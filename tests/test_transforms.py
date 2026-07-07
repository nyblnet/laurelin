"""Tests for laurelin.transforms: decorators, collection, planning, building."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import BuildStatus
from laurelin.transforms import (
    Builder,
    Input,
    Output,
    PipelineError,
    TransformRegistry,
    TransformSpec,
    collect_transforms,
    sql_transform,
    transform,
    use_registry,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace.init(tmp_path / "ws", name="test-ws")


@pytest.fixture
def store(workspace: Workspace) -> MetadataStore:
    return MetadataStore(workspace.metadata_path)


@pytest.fixture
def catalog(workspace: Workspace, store: MetadataStore) -> DatasetCatalog:
    return DatasetCatalog(workspace, store)


def make_spec(name: str, output: str, *inputs: str) -> TransformSpec:
    return TransformSpec(
        name=name,
        output=Output(dataset=output),
        inputs={f"in{i}": Input(dataset=d) for i, d in enumerate(inputs)},
        kind="python",
        fn=lambda **kw: pa.table({"x": [1]}),
    )


# ---------------------------------------------------------------------------
# Decorators & registry
# ---------------------------------------------------------------------------

def test_transform_decorator_registers():
    registry = TransformRegistry()
    with use_registry(registry):

        @transform(Output(dataset="clean", description="cleaned"),
                   raw=Input(dataset="raw"))
        def clean(raw):
            return raw

    spec = registry.get("clean")
    assert spec.kind == "python"
    assert spec.output.dataset == "clean"
    assert spec.output.description == "cleaned"
    assert spec.inputs == {"raw": Input(dataset="raw")}
    assert spec.fn is clean
    assert spec.query is None
    assert registry.by_output("clean") is spec
    assert registry.all() == [spec]


def test_sql_transform_decorator_registers():
    registry = TransformRegistry()
    with use_registry(registry):

        @sql_transform(
            Output(dataset="stats"),
            inputs={"clean": Input(dataset="clean")},
            query="SELECT count(*) AS n FROM clean",
        )
        def stats():
            ...

    spec = registry.get("stats")
    assert spec.kind == "sql"
    assert spec.fn is None
    assert spec.query == "SELECT count(*) AS n FROM clean"


def test_decorator_outside_collection_does_not_register_globally():
    @transform(Output(dataset="orphan"))
    def orphan():
        return pa.table({"x": [1]})

    assert orphan.__transform_spec__.name == "orphan"


def test_registry_rejects_duplicates():
    registry = TransformRegistry()
    registry.register(make_spec("t1", "out"))
    with pytest.raises(ValueError, match="Duplicate transform name"):
        registry.register(make_spec("t1", "other"))
    with pytest.raises(ValueError, match="produced by both"):
        registry.register(make_spec("t2", "out"))
    with pytest.raises(KeyError, match="No transform named"):
        registry.get("missing")
    assert registry.by_output("missing") is None


# ---------------------------------------------------------------------------
# collect_transforms
# ---------------------------------------------------------------------------

def test_collect_transforms_from_files(tmp_path: Path):
    pipelines = tmp_path / "pipelines"
    pipelines.mkdir()
    (pipelines / "b_second.py").write_text(textwrap.dedent("""
        from laurelin.transforms import transform, sql_transform, Input, Output

        @sql_transform(Output(dataset="agg"),
                       inputs={"clean": Input(dataset="clean")},
                       query="SELECT * FROM clean")
        def aggregate():
            ...
    """))
    (pipelines / "a_first.py").write_text(textwrap.dedent("""
        from laurelin.transforms import transform, Input, Output

        @transform(Output(dataset="clean"), raw=Input(dataset="raw"))
        def clean(raw):
            return raw
    """))
    registry = collect_transforms(pipelines)
    names = [s.name for s in registry.all()]
    assert names == ["clean", "aggregate"]  # sorted file order
    assert registry.by_output("agg").kind == "sql"


def test_collect_transforms_empty_or_missing_dir(tmp_path: Path):
    assert collect_transforms(tmp_path / "nope").all() == []
    empty = tmp_path / "empty"
    empty.mkdir()
    assert collect_transforms(empty).all() == []


def test_collect_transforms_error_names_file(tmp_path: Path):
    pipelines = tmp_path / "pipelines"
    pipelines.mkdir()
    (pipelines / "broken.py").write_text("def nope(:\n")
    with pytest.raises(PipelineError, match="broken.py"):
        collect_transforms(pipelines)

    (pipelines / "broken.py").write_text("raise RuntimeError('boom')\n")
    with pytest.raises(PipelineError, match="broken.py.*boom"):
        collect_transforms(pipelines)


def test_collect_transforms_files_isolated_namespaces(tmp_path: Path):
    pipelines = tmp_path / "pipelines"
    pipelines.mkdir()
    (pipelines / "a.py").write_text("SHARED = 1\n")
    (pipelines / "b.py").write_text("assert 'SHARED' not in dir()\n")
    collect_transforms(pipelines)  # must not raise


# ---------------------------------------------------------------------------
# Builder.plan
# ---------------------------------------------------------------------------

def build_diamond_registry() -> TransformRegistry:
    # raw -> t1 -> d1 ; d1 -> t2 -> d2 ; d1 -> t3 -> d3 ; d2,d3 -> t4 -> d4
    registry = TransformRegistry()
    registry.register(make_spec("t1", "d1", "raw"))
    registry.register(make_spec("t2", "d2", "d1"))
    registry.register(make_spec("t3", "d3", "d1"))
    registry.register(make_spec("t4", "d4", "d2", "d3"))
    return registry


def planner(registry: TransformRegistry) -> Builder:
    return Builder(None, None, None, registry)


def test_plan_diamond_ordering():
    registry = build_diamond_registry()
    order = [s.name for s in planner(registry).plan(["d4"])]
    assert set(order) == {"t1", "t2", "t3", "t4"}
    assert order.index("t1") < order.index("t2")
    assert order.index("t1") < order.index("t3")
    assert order.index("t2") < order.index("t4")
    assert order.index("t3") < order.index("t4")


def test_plan_subset_and_all():
    registry = build_diamond_registry()
    b = planner(registry)
    assert [s.name for s in b.plan(["d2"])] == ["t1", "t2"]
    assert [s.name for s in b.plan(["d1"])] == ["t1"]
    all_order = [s.name for s in b.plan(None)]
    assert set(all_order) == {"t1", "t2", "t3", "t4"}
    assert all_order.index("t1") < all_order.index("t4")
    # no duplicates even when targets share upstreams
    assert [s.name for s in b.plan(["d2", "d3"])].count("t1") == 1


def test_plan_unknown_target():
    with pytest.raises(ValueError, match="Unknown build target 'nope'"):
        planner(build_diamond_registry()).plan(["nope"])


def test_plan_cycle_detection():
    registry = TransformRegistry()
    registry.register(make_spec("ta", "a", "b"))
    registry.register(make_spec("tb", "b", "a"))
    with pytest.raises(ValueError, match="Cycle detected.*ta.*tb|Cycle detected.*tb.*ta"):
        planner(registry).plan(None)


# ---------------------------------------------------------------------------
# Builder.build (real catalog integration)
# ---------------------------------------------------------------------------

def seed_raw_flights(catalog: DatasetCatalog) -> None:
    catalog.write(
        "raw_flights",
        pa.table({
            "flight_id": ["f1", "f2", "f3", "f4"],
            "tail": ["n1", "n1", "n2", None],
            "delay": [5, 15, 0, 99],
        }),
    )


def flights_registry() -> TransformRegistry:
    registry = TransformRegistry()
    with use_registry(registry):

        @transform(Output(dataset="clean_flights", description="drop null tails"),
                   raw=Input(dataset="raw_flights"))
        def clean_flights(raw: pa.Table) -> pa.Table:
            import pyarrow.compute as pc
            return raw.filter(pc.is_valid(raw["tail"]))

        @sql_transform(
            Output(dataset="flight_stats"),
            inputs={"flights": Input(dataset="clean_flights")},
            query="""
                SELECT tail, count(*) AS n_flights, avg(delay) AS avg_delay
                FROM flights GROUP BY tail ORDER BY tail
            """,
        )
        def flight_stats():
            ...

    return registry


def test_build_python_and_sql(workspace, catalog, store):
    seed_raw_flights(catalog)
    builder = Builder(workspace, catalog, store, flights_registry())
    build = builder.build()

    assert build.status == BuildStatus.succeeded
    assert build.started_at and build.finished_at
    assert {t.transform_name: t.status for t in build.tasks} == {
        "clean_flights": BuildStatus.succeeded,
        "flight_stats": BuildStatus.succeeded,
    }

    clean = catalog.read("clean_flights")
    assert clean.num_rows == 3
    stats = catalog.read("flight_stats")
    assert stats.num_rows == 2
    row = stats.to_pylist()[0]
    assert row["tail"] == "n1" and row["n_flights"] == 2 and row["avg_delay"] == 10.0

    version = store.get_version("clean_flights")
    assert version.source == "transform"
    assert version.build_id == build.id

    task = next(t for t in build.tasks if t.transform_name == "clean_flights")
    assert task.rows_written == 3
    assert task.output_version == version.version

    # lineage
    edges = {(e.upstream_dataset, e.downstream_dataset, e.transform_name)
             for e in store.list_lineage()}
    assert edges == {
        ("raw_flights", "clean_flights", "clean_flights"),
        ("clean_flights", "flight_stats", "flight_stats"),
    }

    # audit
    actions = [a.action for a in store.list_audit()]
    assert "build_started" in actions and "build_finished" in actions

    # persisted build matches
    persisted = store.get_build(build.id)
    assert persisted.status == BuildStatus.succeeded
    assert len(persisted.tasks) == 2


def test_build_targets_subset(workspace, catalog, store):
    seed_raw_flights(catalog)
    builder = Builder(workspace, catalog, store, flights_registry())
    build = builder.build(["clean_flights"])
    assert build.targets == ["clean_flights"]
    assert [t.transform_name for t in build.tasks] == ["clean_flights"]
    with pytest.raises(KeyError):
        catalog.read("flight_stats")


def test_build_missing_input_fails_cleanly(workspace, catalog, store):
    registry = TransformRegistry()
    with use_registry(registry):

        @transform(Output(dataset="out"), src=Input(dataset="never_created"))
        def needs_missing(src):
            return src

    build = Builder(workspace, catalog, store, registry).build()
    assert build.status == BuildStatus.failed
    task = build.tasks[0]
    assert task.status == BuildStatus.failed
    assert "never_created" in task.error


def test_build_non_table_return_fails(workspace, catalog, store):
    seed_raw_flights(catalog)
    registry = TransformRegistry()
    with use_registry(registry):

        @transform(Output(dataset="bad_out"), raw=Input(dataset="raw_flights"))
        def bad(raw):
            return [1, 2, 3]

    build = Builder(workspace, catalog, store, registry).build()
    task = build.tasks[0]
    assert task.status == BuildStatus.failed
    assert "must return a pyarrow.Table" in task.error


def test_build_failure_isolation(workspace, catalog, store):
    seed_raw_flights(catalog)
    registry = TransformRegistry()
    with use_registry(registry):

        @transform(Output(dataset="broken"), raw=Input(dataset="raw_flights"))
        def breaks(raw):
            raise RuntimeError("kaboom")

        @transform(Output(dataset="downstream"), b=Input(dataset="broken"))
        def dependent(b):
            return b

        @transform(Output(dataset="further"), d=Input(dataset="downstream"))
        def transitively_dependent(d):
            return d

        @transform(Output(dataset="independent"), raw=Input(dataset="raw_flights"))
        def independent(raw):
            return raw

    build = Builder(workspace, catalog, store, registry).build()
    assert build.status == BuildStatus.failed
    by_name = {t.transform_name: t for t in build.tasks}

    assert by_name["breaks"].status == BuildStatus.failed
    assert "kaboom" in by_name["breaks"].error

    assert by_name["dependent"].status == BuildStatus.failed
    assert "Skipped" in by_name["dependent"].error
    assert by_name["transitively_dependent"].status == BuildStatus.failed
    assert "Skipped" in by_name["transitively_dependent"].error

    assert by_name["independent"].status == BuildStatus.succeeded
    assert catalog.read("independent").num_rows == 4
    with pytest.raises(KeyError):
        catalog.read("broken")
