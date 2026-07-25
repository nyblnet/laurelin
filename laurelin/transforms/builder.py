"""Builder: plans a topological order over the transform DAG and executes it.

Each executed transform writes a new dataset version (source="transform"),
records a build task, and replaces its lineage edges. A failing task marks the
build failed but only blocks tasks that (transitively) depend on its output.
"""

from __future__ import annotations

import duckdb
import pyarrow as pa

from laurelin.catalog import DatasetCatalog
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
    ):
        self.workspace = workspace
        self.catalog = catalog
        self.store = store
        self.registry = registry

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

    def execute(self, build_id: str, targets: list[str] | None = None) -> BuildInfo:
        """Execute an already-created build record (the async path: the API
        creates the record, returns it, and hands execution to a worker)."""
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
            try:
                result = self._execute(spec)
                version = self.catalog.write(
                    spec.output.dataset,
                    result,
                    source="transform",
                    build_id=build.id,
                    description=spec.output.description,
                )
                task.status = BuildStatus.succeeded
                task.rows_written = version.row_count
                task.output_version = version.version
                self.store.replace_lineage_for_transform(
                    spec.name,
                    [
                        LineageEdge(
                            upstream_dataset=inp.dataset,
                            downstream_dataset=spec.output.dataset,
                            transform_name=spec.name,
                        )
                        for inp in spec.inputs.values()
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
        self.store.update_build(
            build.id,
            status=final_status,
            finished_at=utcnow_iso(),
            error="One or more tasks failed" if any_failed else None,
        )
        self.store.log_audit(
            "build_finished", {"build_id": build.id, "status": final_status.value}
        )
        result_build = self.store.get_build(build.id)
        assert result_build is not None
        return result_build

    def _execute(self, spec: TransformSpec) -> pa.Table:
        if spec.kind == "python":
            return self._execute_python(spec)
        if spec.kind == "sql":
            return self._execute_sql(spec)
        raise ValueError(f"Unknown transform kind {spec.kind!r} for {spec.name!r}")

    def _read_input(self, spec: TransformSpec, param: str, dataset: str):
        try:
            return self.catalog.read(dataset)
        except KeyError as exc:
            raise RuntimeError(
                f"Input {param}={dataset!r} of transform {spec.name!r} is not "
                f"available and no transform produces it: {exc.args[0]}"
            ) from exc

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
                    glob = self.catalog.parquet_glob(inp.dataset)
                except KeyError as exc:
                    raise RuntimeError(
                        f"Input {alias}={inp.dataset!r} of transform "
                        f"{spec.name!r} is not available and no transform "
                        f"produces it: {exc.args[0]}"
                    ) from exc
                # parquet_glob returns a SQL list literal of the version's
                # parts (a version may be multi-part after an append).
                con.execute(
                    f'CREATE VIEW "{alias}" AS SELECT * FROM read_parquet({glob})'
                )
            result = con.execute(spec.query).arrow()
        finally:
            con.close()
        if isinstance(result, pa.RecordBatchReader):
            result = result.read_all()
        return result
