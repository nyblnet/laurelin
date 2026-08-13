"""Builder: plans a topological order over the transform DAG and executes it.

Each executed transform writes a new dataset version (source="transform"),
records a build task, and replaces its lineage edges. A failing task marks the
build failed but only blocks tasks that (transitively) depend on its output.
"""

from __future__ import annotations

import logging
import os
import time

import duckdb
import pyarrow as pa

from laurelin.catalog import DatasetCatalog
from laurelin.core import engines, limits, metrics
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.failure import Failure, FailureCode, Phase
from laurelin.core.models import (
    BuildInfo,
    BuildStatus,
    BuildTaskInfo,
    LineageEdge,
    utcnow_iso,
)
from laurelin.transforms.api import TransformRegistry, TransformSpec
from laurelin.transforms.expectations import ExpectationError
from laurelin.transforms.expectations import check as check_expectations

log = logging.getLogger("laurelin.builder")


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
            # R1: `error=str(exc)` here put a ValueError's text on a
            # VIEWER-gated route (GET /builds). The message is ours — the
            # planner raised it — but the rule is "convert at the catch site"
            # with no exceptions, because the next raise inside `plan` will come
            # from somewhere else.
            self.store.update_build(
                build_id, status=BuildStatus.failed,
                started_at=utcnow_iso(), finished_at=utcnow_iso(),
                failure=Failure.from_exception(
                    exc, code=FailureCode.TRANSFORM_FAILED, phase=Phase.plan,
                    subject=f"build:{build_id}", driver="python",
                ),
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
                        # A first-party fact with a first-party shape: this
                        # task never ran because a named upstream failed. The
                        # names are dataset names, which the reader of a build
                        # already sees.
                        failure=Failure(
                            code=FailureCode.TRANSFORM_FAILED,
                            phase=Phase.plan,
                            subject=f"transform:{spec.name}",
                            counters={"blocked_by": len(blocked_by)},
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
            validate = self._expectation_validator(spec, task)
            try:
                if spec.incremental:
                    (param, only_input), = spec.inputs.items()
                    mode, delta, input_version = self._incremental_input(spec)
                    if mode == "unchanged":
                        # Nothing new upstream: succeed without minting a
                        # version. A no-op build should leave no trace.
                        task.status = BuildStatus.succeeded
                        task.rows_written = 0
                        task.finished_at = utcnow_iso()
                        self.store.upsert_build_task(build.id, task)
                        continue
                    result = spec.fn(**{param: delta})
                    if not isinstance(result, pa.Table):
                        raise TypeError(
                            f"Transform {spec.name!r} must return a pyarrow.Table, "
                            f"got {type(result).__name__}"
                        )
                    writer = (
                        self.catalog.append if mode == "delta" else self.catalog.write
                    )
                    version = writer(
                        spec.output.dataset,
                        result,
                        source="transform",
                        build_id=build.id,
                        description=spec.output.description,
                        validate=validate,
                    )
                    self.store.set_transform_state(
                        spec.name, only_input.dataset,
                        input_version.version, input_version.row_count,
                    )
                elif spec.streaming:
                    # Batches flow input -> fn -> parquet writer, so neither
                    # side is ever held whole.
                    version = self.catalog.write_batches(
                        spec.output.dataset,
                        self._execute_streaming(spec),
                        source="transform",
                        build_id=build.id,
                        description=spec.output.description,
                        validate=validate,
                    )
                else:
                    version = self.catalog.write(
                        spec.output.dataset,
                        self._execute(spec),
                        source="transform",
                        build_id=build.id,
                        description=spec.output.description,
                        validate=validate,
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
                # R1, and this is the leak reproduced live on this tree: the old
                # line was `task.error = f"{type(exc).__name__}: {exc}"`, and a
                # plain VIEWER read a driver-authored sentinel out of GET
                # /builds/{id}, GET /builds and GET /pipelines/{name}. There was
                # no redactor anywhere on this path — `grep -rn redact
                # laurelin/transforms/` returned nothing.
                task.failure = _task_failure(exc, spec)
            task.finished_at = utcnow_iso()
            self.store.upsert_build_task(build.id, task)

        # Propagate classification markings along the (now-updated) lineage so
        # every derived dataset inherits its inputs' markings.
        self.store.recompute_all_markings()
        self._refresh_object_indexes()

        final_status = BuildStatus.failed if any_failed else BuildStatus.succeeded
        metrics.builds.labels(status=final_status.value).inc()
        metrics.build_duration.observe(time.perf_counter() - started_at)
        self.store.update_build(
            build.id,
            status=final_status,
            finished_at=utcnow_iso(),
            failure=Failure(
                code=FailureCode.TRANSFORM_FAILED, phase=Phase.execute,
                subject=f"build:{build.id}",
                counters={"failed_tasks": len(failed_outputs)},
            ) if any_failed else None,
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

    def _expectation_validator(self, spec: TransformSpec, task):
        """A callback the catalog runs after the output's parts are written and
        before its manifest row is inserted.

        That ordering is the whole design. The row insert *is* the
        publication, so raising here means the failing version never existed
        as far as any reader is concerned — no downstream build consumes it,
        no dashboard shows it, and the orphaned parts are deleted. Checking
        after the commit would mean deciding what to do about data people can
        already see.
        """
        if not spec.expectations:
            return None

        def validate(files: list[str]) -> None:
            con = duckdb.connect()
            try:
                # Register the freshly written parts as `t`. A pyarrow dataset
                # is lazy, so a count over a billion rows is a scan, not a
                # materialization — a streaming transform stays streaming.
                con.register("t", self.catalog.storage.dataset(files))
                with limits.limited(con, limits.QueryLimits.build()):
                    results = check_expectations(
                        con, spec.expectations, spec.output.dataset
                    )
            finally:
                con.close()

            task.expectations = results
            failures = [r for r in results if not r["passed"]
                        and r["severity"] == "error"]
            for r in results:
                if not r["passed"] and r["severity"] == "warn":
                    log.warning("expectation warning on %s: %s",
                                spec.output.dataset, r["message"])
            if failures:
                raise ExpectationError(spec.output.dataset, failures)

        return validate

    def _refresh_object_indexes(self) -> None:
        """Rebuild any object index that a build invalidated.

        Only types already indexed are refreshed — indexing is opt-in, and a
        build should not silently start materializing every object type. A
        failure here must not fail the build: the index is an optimization,
        and queries fall back to scanning without it.
        """
        from laurelin.ontology import OntologyService, load_ontology

        try:
            ontology = load_ontology(self.workspace.ontology_dir)
        except Exception:  # noqa: BLE001 - a bad ontology is not this build's problem
            return
        service = OntologyService(self.workspace, self.catalog, self.store, ontology)
        for ot in ontology.object_types:
            if self.store.object_index_state(ot.api_name) is None:
                continue
            try:
                if not service.index_is_fresh(ot):
                    service.reindex(ot.api_name)
            except Exception:  # noqa: BLE001
                self.store.drop_object_index(ot.api_name)  # stale beats wrong

    def _incremental_input(self, spec: TransformSpec):
        """Work out what an incremental transform actually has to process.

        Returns ``(mode, table, version)`` where mode is:

        * ``"unchanged"`` — the input hasn't advanced; nothing to do.
        * ``"delta"`` — the input grew by appending, so the new rows are
          exactly the parts added since last time. Because a version is a
          manifest and appends only ever *extend* it, a prefix check tells us
          this precisely — and lets us read only the new parts rather than the
          whole input.
        * ``"full"`` — first run, or the input was rewritten rather than
          appended to, so its history no longer lines up and everything must
          be reprocessed.
        """
        (param, inp), = spec.inputs.items()
        current = self.store.get_version(inp.dataset, None)
        if current is None:
            raise RuntimeError(
                f"Input {param}={inp.dataset!r} of transform {spec.name!r} has "
                f"no versions yet"
            )

        state = self.store.get_transform_state(spec.name, inp.dataset)
        if state is None:
            return "full", self.catalog.read(inp.dataset), current
        if state["last_version"] == current.version:
            return "unchanged", None, current

        previous = self.store.get_version(inp.dataset, state["last_version"])
        prev_files = list(previous.files) if previous else []
        if prev_files and current.files[: len(prev_files)] == prev_files:
            delta = current.files[len(prev_files):]
            if not delta:
                return "unchanged", None, current
            return "delta", self.catalog.storage.read_table(delta), current
        # Rewritten (or pre-manifest): the old output no longer corresponds to
        # any prefix of this input, so appending would double-count.
        return "full", self.catalog.read(inp.dataset), current

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
                    if info is not None and info.scans_at_source:
                        # Reduce at the boundary: a transform may read a table
                        # scanned at the source (federated, Iceberg,
                        # ClickHouse) and write a managed one. This is the
                        # intended path for large data. Iceberg reached here
                        # through arrow_dataset() before, which has no local
                        # parts to scan — a latent bug this fixes.
                        scan = self.catalog.source_table(inp.dataset)
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


def _task_failure(exc: BaseException, spec) -> Failure:
    """One transform task's failure, as Laurelin records it.

    A transform can fail three ways and they mean different things to whoever
    reads the build: a declared expectation rejected the output, a remote engine
    refused, or our own Python raised. Only the first is worth a distinct code;
    everything else is TRANSFORM_FAILED (or DuckDB's own classification) plus a
    `detail_ref` that finds the traceback in the log.

    The count of failed expectations is carried as an *integer*, not the
    concatenated `message` strings that `ExpectationError.__str__` builds —
    those messages are prose an editor wrote in a pipeline file, which is
    exactly the class of text R2 withholds from a viewer. `task.expectations`
    already holds the structured per-check results for the editor who may read
    them.
    """
    subject = f"transform:{spec.name}"
    if isinstance(exc, ExpectationError):
        return Failure.from_exception(
            exc, code=FailureCode.EXPECTATION_FAILED, phase=Phase.write,
            subject=subject, driver="python",
            counters={"failed_expectations": len(getattr(exc, "failures", []))},
        )
    driver = "duckdb" if type(exc).__module__.split(".")[0] == "duckdb" else "python"
    return Failure.from_exception(
        exc, phase=Phase.execute, subject=subject, driver=driver,
        code=None if driver == "duckdb" else FailureCode.TRANSFORM_FAILED,
    )
