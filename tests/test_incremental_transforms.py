"""Incremental transforms — process only what's new.

Reprocessing yesterday's rows to produce yesterday's answers is the most
common waste in a pipeline. An incremental transform sees only the rows its
input gained since the last build, and its output is appended.

The delta is derived from the version manifest: appends only ever *extend* it,
so a prefix check says precisely whether the history still lines up.
"""

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.transforms import Builder, Input, Output, collect_transforms, transform

PIPELINE = """
from laurelin.transforms import transform, Input, Output
import pyarrow as pa

@transform(output=Output("doubled"), incremental=True, src=Input("src"))
def doubled(src):
    import pyarrow.compute as pc
    return src.set_column(0, "id", pc.multiply(src.column("id"), 2))
"""


def rows(start: int, n: int) -> pa.Table:
    return pa.table({"id": pa.array(range(start, start + n), type=pa.int64())})


@pytest.fixture()
def env(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="incr")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    (ws.pipelines_dir / "p.py").write_text(PIPELINE)
    return ws, store, catalog


def build(ws, store, catalog):
    return Builder(ws, catalog, store, collect_transforms(ws.pipelines_dir)).build()


def test_first_build_processes_everything(env):
    ws, store, catalog = env
    catalog.write("src", rows(0, 5))
    result = build(ws, store, catalog)
    assert result.status.value == "succeeded", result.tasks
    assert catalog.read("doubled").column("id").to_pylist() == [0, 2, 4, 6, 8]
    assert result.tasks[0].rows_written == 5


def test_append_processes_only_the_delta(env):
    ws, store, catalog = env
    catalog.write("src", rows(0, 3))
    build(ws, store, catalog)

    catalog.append("src", rows(3, 2))
    result = build(ws, store, catalog)
    assert result.status.value == "succeeded", result.tasks

    # Only the two new rows were processed, and the output grew rather than
    # being rebuilt.
    assert catalog.read("doubled").column("id").to_pylist() == [0, 2, 4, 6, 8]
    assert store.get_dataset("doubled").latest_version == 2


def test_unchanged_input_is_a_no_op(env):
    ws, store, catalog = env
    catalog.write("src", rows(0, 3))
    build(ws, store, catalog)
    before = store.get_dataset("doubled").latest_version

    result = build(ws, store, catalog)
    assert result.status.value == "succeeded"
    assert result.tasks[0].rows_written == 0
    assert store.get_dataset("doubled").latest_version == before, "no new version"


def test_a_rewritten_input_forces_a_full_rebuild(env):
    """If the input was replaced rather than appended to, its history no longer
    lines up — appending would double-count."""
    ws, store, catalog = env
    catalog.write("src", rows(0, 3))
    build(ws, store, catalog)

    catalog.write("src", rows(100, 2))  # full rewrite, not an append
    result = build(ws, store, catalog)
    assert result.status.value == "succeeded", result.tasks
    assert catalog.read("doubled").column("id").to_pylist() == [200, 202]


def test_state_tracks_the_processed_version(env):
    ws, store, catalog = env
    catalog.write("src", rows(0, 3))
    build(ws, store, catalog)
    assert store.get_transform_state("doubled", "src")["last_version"] == 1

    catalog.append("src", rows(3, 1))
    build(ws, store, catalog)
    assert store.get_transform_state("doubled", "src")["last_version"] == 2


def test_clearing_state_forces_a_rebuild(env):
    ws, store, catalog = env
    catalog.write("src", rows(0, 3))
    build(ws, store, catalog)
    store.clear_transform_state("doubled")

    result = build(ws, store, catalog)
    assert result.tasks[0].rows_written == 3, "no state -> reprocess everything"


def test_incremental_requires_exactly_one_input():
    with pytest.raises(ValueError, match="exactly one input"):
        @transform(output=Output("out"), incremental=True,
                   a=Input("x"), b=Input("y"))
        def two(a, b):  # pragma: no cover
            return a
