"""Builder: plans a topological order over the transform DAG and executes it.

Each executed transform writes a new dataset version (source="transform"),
records a build task, and replaces its lineage edges. A failing task marks the
build failed but only blocks tasks that (transitively) depend on its output.
"""

from __future__ import annotations

import os
import time

import duckdb
import pyarrow as pa

from laurelin.catalog import DatasetCatalog
from laurelin.core import engines, limits, metrics
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import (
    BuildInfo,
    BuildStatus,
    BuildTaskInfo,
    LineageEdge,
    utcnow_iso,
)
from laurelin.transforms.api import TransformRegistry, TransformSpec


class Builder:
    def __init__(
        self,
        workspace: Workspace,
        catalog: DatasetCatalog,
        store: MetadataStore,
        registry: TransformRegistry,
        engine_factory=None,
    ):
        self.workspace = workspace
        self.catalog = catalog
        self.store = store
        self.registry = registry
        # Injectable so the orchestration around delegated compute — lineage,
        # policy, limits, error handling — is testable without a cluster.
        self._engine_factory = engine_factory or engines.connect

    # -- planning -------------------------------------------------------------

    def plan(self, targets: list[str] | None = None) -> list[TransformSpec]:
        """Topologically ordered transforms needed to produce `targets`.

        `targets` is a list of output dataset names; None means every
        registered output. Includes upstream transforms recursively. Raises
        ValueError for an unknown target or a dependency cycle.
        """
        if targets is None:
            roots = self.registry.all()
        else:
            roots = []
            for name in targets:
                spec = self.registry.by_output(name)
                if spec is None:
                    raise ValueError(
                        f"Unknown build target {name!r}: no transform produces it"
                    )
                roots.append(spec)

        order: list[TransformSpec] = []
        state: dict[str, str] = {}  # transform name -> "visiting" | "done"
        path: list[str] = []

        def visit(spec: TransformSpec) -> None:
            status = state.get(spec.name)
            if status == "done":
                return
            if status == "visiting":
                cycle = path[path.index(spec.name):] + [spec.name]
                raise ValueError(
                    "Cycle detected in transform DAG: " + " -> ".join(cycle)
                )
            state[spec.name] = "visiting"
            path.append(spec.name)
            for inp in spec.inputs.values():
                upstream = self.registry.by_output(inp.dataset)
                if upstream is not None:
                    visit(upstream)
            path.pop()
            state[spec.name] = "done"
            order.append(spec)

        for spec in roots:
            visit(spec)
        return order

    # -- execution ------------------------------------------------------------

    def build(self, targets: list[str] | None = None) -> BuildInfo:
        """Plan and execute synchronously (CLI and ``wait=true`` API calls).
        Raises ValueError for an unknown target or a cycle *before* creating
        the build record, so a typo doesn't leave a failed-build tombstone."""
        self.plan(targets)
        build = self.store.create_build(list(targets) if targets else [])
        return self.execute(build.id, targets)

    def execute(
        self,
        build_id: str,
        targets: list[str] | None = None,
        worker: str | None = None,
    ) -> BuildInfo:
        """Execute an already-created build record (the async path: the API
        creates the record, returns it, and hands execution to a worker).

        With several replicas serving one workspace, more than one may try to
        run the same build. ``worker`` identifies this process; execution
        proceeds only if it wins the lease, so the build runs exactly once.
        """
        if worker is not None and not self.store.claim_build(build_id, worker):
            metrics.build_claims.labels(outcome="lost").inc()
            existing = self.store.get_build(build_id)
            assert existing is not None
            return existing  # another replica owns it
        if worker is not None:
            metrics.build_claims.labels(outcome="won").inc()
        started_at = time.perf_counter()
        try:
            specs = self.plan(targets)
        except ValueError as exc:
            # Normally caught at request time; guards the worker against a
            # pipeline edit racing the queue.
            self.store.update_build(
                build_id, status=BuildStatus.failed,
                started_at=utcnow_iso(), finished_at=utcnow_iso(), error=str(exc),
            )
            failed = self.store.get_build(build_id)
            assert failed is not None
            return failed
        build = self.store.get_build(build_id)
        assert build is not None
        self.store.update_build(
            build.id, status=BuildStatus.running, started_at=utcnow_iso()
        )
        self.store.log_audit(
            "build_started",
            {"build_id": build.id, "targets": build.targets,
             "transforms": [s.name for s in specs]},
        )

        failed_outputs: set[str] = set()
        any_failed = False

        for spec in specs:
            blocked_by = sorted(
                inp.dataset for inp in spec.inputs.values()
                if inp.dataset in failed_outputs
            )
            if blocked_by:
                any_failed = True
                failed_outputs.add(spec.output.dataset)
                self.store.upsert_build_task(
                    build.id,
                    BuildTaskInfo(
                        transform_name=spec.name,
                        output_dataset=spec.output.dataset,
                        status=BuildStatus.failed,
                        started_at=utcnow_iso(),
                        finished_at=utcnow_iso(),
                        error=(
                            "Skipped: upstream input(s) failed to build: "
                            + ", ".join(blocked_by)
                        ),
                    ),
                )
                continue

            task = BuildTaskInfo(
                transform_name=spec.name,
                output_dataset=spec.output.dataset,
                status=BuildStatus.running,
                started_at=utcnow_iso(),
            )
            self.store.upsert_build_task(build.id, task)
            if worker is not None:
                # Heartbeat between transforms so a long build isn't reaped as
                # abandoned. A lost lease means another replica took over.
                self.store.renew_build_lease(build.id, worker)
            try:
                if spec.streaming:
                    # Batches flow input -> fn -> parquet writer, so neither
                    # side is ever held whole.
                    version = self.catalog.write_batches(
                        spec.output.dataset,
                        self._execute_streaming(spec),
                        source="transform",
                        build_id=build.id,
                        description=spec.output.description,
                    )
                else:
                    version = self.catalog.write(
                        spec.output.dataset,
                        self._execute(spec),
                        source="transform",
                        build_id=build.id,
                        description=spec.output.description,
                    )
                task.status = BuildStatus.succeeded
                task.rows_written = version.row_count
                task.output_version = version.version
                upstreams = [inp.dataset for inp in spec.inputs.values()]
                if spec.kind == "remote" and spec.engine:
                    # A remote transform reads the engine's catalog, not
                    # Laurelin datasets, so record the engine as the upstream —
                    # otherwise the result would appear to come from nowhere.
                    upstreams = [f"engine:{spec.engine}"]
                self.store.replace_lineage_for_transform(
                    spec.name,
                    [
                        LineageEdge(
                            upstream_dataset=upstream,
                            downstream_dataset=spec.output.dataset,
                            transform_name=spec.name,
                        )
                        for upstream in upstreams
                    ],
                )
            except Exception as exc:
                any_failed = True
                failed_outputs.add(spec.output.dataset)
                task.status = BuildStatus.failed
                task.error = f"{type(exc).__name__}: {exc}"
            task.finished_at = utcnow_iso()
            self.store.upsert_build_task(build.id, task)

        # Propagate classification markings along the (now-updated) lineage so
        # every derived dataset inherits its inputs' markings.
        self.store.recompute_all_markings()

        final_status = BuildStatus.failed if any_failed else BuildStatus.succeeded
        metrics.builds.labels(status=final_status.value).inc()
        metrics.build_duration.observe(time.perf_counter() - started_at)
        self.store.update_build(
            build.id,
            status=final_status,
            finished_at=utcnow_iso(),
            error="One or more tasks failed" if any_failed else None,
        )
        self.store.log_audit(
            "build_finished", {"build_id": build.id, "status": final_status.value}
        )
        if worker is not None:
            self.store.release_build(build.id)
        # Builds are the natural periodic hook for housekeeping the audit log,
        # which nothing else bounds.
        keep = int(os.environ.get("LAURELIN_AUDIT_MAX_EVENTS", "0") or 0)
        if keep > 0:
            self.store.prune_audit(keep)
        result_build = self.store.get_build(build.id)
        assert result_build is not None
        return result_build

    def _execute(self, spec: TransformSpec) -> pa.Table:
        if spec.kind == "python":
            return self._execute_python(spec)
        if spec.kind == "sql":
            return self._execute_sql(spec)
        if spec.kind == "remote":
            return self._execute_remote(spec)
        raise ValueError(f"Unknown transform kind {spec.kind!r} for {spec.name!r}")

    def _execute_remote(self, spec: TransformSpec) -> pa.Table:
        """Submit the query to a delegated engine and keep what comes back.

        The cluster does the work; Laurelin stores the reduced result. The size
        cap is the guardrail that keeps "delegate" from becoming "download" —
        a query returning millions of rows hasn't reduced anything.
        """
        assert spec.query is not None and spec.engine is not None
        config = self.store.get_engine(spec.engine)
        if config is None:
            raise RuntimeError(
                f"Transform {spec.name!r} names engine {spec.engine!r}, which is "
                f"not registered in this workspace."
            )
        timeout = float(os.environ.get("LAURELIN_ENGINE_TIMEOUT", "300"))
        max_rows = int(os.environ.get("LAURELIN_ENGINE_MAX_ROWS", "5000000"))

        client = self._engine_factory(
            engines.EngineConfig(
                name=config["name"], type=config["type"],
                uri=config["uri"], options=config["options"],
            ),
            timeout,
        )
        try:
            table = client.query(spec.query)
            metrics.engine_queries.labels(status="succeeded").inc()
        except Exception:
            metrics.engine_queries.labels(status="failed").inc()
            raise
        finally:
            client.close()
        if isinstance(table, pa.RecordBatchReader):
            table = table.read_all()
        return engines.check_result_size(table, max_rows, spec.engine)

    def _read_input(self, spec: TransformSpec, param: str, dataset: str):
        try:
            return self.catalog.read(dataset)
        except KeyError as exc:
            raise RuntimeError(
                f"Input {param}={dataset!r} of transform {spec.name!r} is not "
                f"available and no transform produces it: {exc.args[0]}"
            ) from exc

    def _execute_streaming(self, spec: TransformSpec):
        """Feed the transform a lazy batch iterator and yield what it produces.

        Each yielded value is validated as it passes, so a transform that
        returns the wrong type fails with a clear message mid-stream rather
        than confusing the Parquet writer.
        """
        assert spec.fn is not None
        (param, inp), = spec.inputs.items()
        try:
            batches = self.catalog.iter_batches(inp.dataset)
        except KeyError as exc:
            raise RuntimeError(
                f"Input {param}={inp.dataset!r} of transform {spec.name!r} is "
                f"not available and no transform produces it: {exc.args[0]}"
            ) from exc

        produced = 0
        for chunk in spec.fn(**{param: batches}):
            if isinstance(chunk, pa.RecordBatch):
                chunk = pa.Table.from_batches([chunk])
            if not isinstance(chunk, pa.Table):
                raise TypeError(
                    f"Streaming transform {spec.name!r} must yield pyarrow "
                    f"Tables or RecordBatches, got {type(chunk).__name__}"
                )
            produced += 1
            yield chunk
        if produced == 0:
            raise ValueError(
                f"Streaming transform {spec.name!r} produced no batches; it "
                f"must yield at least one (an empty table is fine)."
            )

    def _execute_python(self, spec: TransformSpec) -> pa.Table:
        assert spec.fn is not None
        kwargs = {
            param: self._read_input(spec, param, inp.dataset)
            for param, inp in spec.inputs.items()
        }
        result = spec.fn(**kwargs)
        if not isinstance(result, pa.Table):
            raise TypeError(
                f"Transform {spec.name!r} must return a pyarrow.Table, "
                f"got {type(result).__name__}"
            )
        return result

    def _execute_sql(self, spec: TransformSpec) -> pa.Table:
        assert spec.query is not None
        con = duckdb.connect()
        try:
            for alias, inp in spec.inputs.items():
                try:
                    info = self.store.get_dataset(inp.dataset)
                    if info is not None and info.is_federated:
                        # Reduce at the boundary: a transform may read a
                        # federated table (scanned remotely) and write a
                        # managed one. This is the intended path for large data.
                        scan = self.catalog.federated_table(inp.dataset)
                    else:
                        scan = self.catalog.arrow_dataset(inp.dataset)
                except KeyError as exc:
                    raise RuntimeError(
                        f"Input {alias}={inp.dataset!r} of transform "
                        f"{spec.name!r} is not available and no transform "
                        f"produces it: {exc.args[0]}"
                    ) from exc
                # Register the lazy Arrow dataset rather than file paths: it
                # keeps scan pushdown, covers multi-part (appended) versions,
                # and works when the parts live in object storage.
                con.register(alias, scan)
            # Builds are allowed to be slow — nobody is waiting on a browser —
            # but must still not exhaust the machine. No admission slot: the
            # worker pool already bounds how many builds run at once.
            limits.apply(con, limits.QueryLimits.build())
            result = con.execute(spec.query).arrow()
        finally:
            con.close()
        if isinstance(result, pa.RecordBatchReader):
            result = result.read_all()
        return result
