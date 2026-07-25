"""Incremental (append) writes and compaction.

The property that matters: appending N rows to a dataset of M rows costs
O(N) of I/O, not O(M+N). Versions stay immutable and independently readable.
"""

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore


@pytest.fixture()
def catalog(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="inc")
    return DatasetCatalog(ws, MetadataStore(ws.metadata_path))


def rows(start: int, n: int) -> pa.Table:
    return pa.table({
        "id": pa.array(range(start, start + n), type=pa.int64()),
        "name": pa.array([f"row-{i}" for i in range(start, start + n)]),
    })


def test_append_adds_rows_and_keeps_history(catalog):
    v1 = catalog.write("events", rows(0, 100))
    v2 = catalog.append("events", rows(100, 50))

    assert v2.version == v1.version + 1
    assert v2.row_count == 150
    assert catalog.read("events").num_rows == 150
    # Earlier versions are untouched and still readable.
    assert catalog.read("events", version=v1.version).num_rows == 100

    ids = catalog.read("events").column("id").to_pylist()
    assert ids == list(range(150))


def test_append_writes_only_the_delta(catalog):
    """The whole point: the new version's *own* directory holds just the new
    rows; the bulk of the data is referenced, not copied."""
    catalog.write("events", rows(0, 50_000))
    v2 = catalog.append("events", rows(50_000, 100))

    v1_dir = catalog.workspace.data_dir / "events" / "v0001"
    v2_dir = catalog.workspace.data_dir / "events" / "v0002"
    v1_bytes = sum(f.stat().st_size for f in v1_dir.glob("*.parquet"))
    v2_bytes = sum(f.stat().st_size for f in v2_dir.glob("*.parquet"))

    assert v2_bytes < v1_bytes / 10, "append must not rewrite the dataset"
    # The manifest references the previous part plus the new one.
    assert len(v2.files) == 2
    assert any("v0001" in f for f in v2.files)
    assert catalog.read("events").num_rows == 50_100


def test_repeated_appends_accumulate_parts(catalog):
    catalog.write("events", rows(0, 10))
    for i in range(1, 5):
        catalog.append("events", rows(i * 10, 10))
    info = catalog.store.get_version("events", None)
    assert len(info.files) == 5
    assert info.row_count == 50
    assert catalog.read("events").num_rows == 50
    # Multi-part versions must work through every read path.
    assert len(catalog.rows("events", limit=100)) == 50
    assert catalog.arrow_dataset("events").count_rows() == 50
    result = catalog.query("SELECT count(*) AS n FROM events")
    assert result["rows"][0]["n"] == 50


def test_compact_merges_parts(catalog):
    catalog.write("events", rows(0, 10))
    for i in range(1, 4):
        catalog.append("events", rows(i * 10, 10))
    assert len(catalog.store.get_version("events", None).files) == 4

    compacted = catalog.compact("events")
    assert len(compacted.files) == 1
    assert compacted.row_count == 40
    assert catalog.read("events").num_rows == 40
    assert sorted(catalog.read("events").column("id").to_pylist()) == list(range(40))


def test_append_to_empty_dataset_is_a_write(catalog):
    info = catalog.append("fresh", rows(0, 5))
    assert info.version == 1
    assert catalog.read("fresh").num_rows == 5


def test_append_rejects_incompatible_schema(catalog):
    catalog.write("events", rows(0, 10))
    with pytest.raises(ValueError, match="incompatible schema"):
        catalog.append("events", pa.table({"totally": ["different"]}))
    # The failed append leaves no trace.
    assert catalog.store.get_version("events", None).version == 1
    assert not list(catalog.workspace.data_dir.glob(".tmp-*"))


def test_legacy_single_file_versions_still_read(catalog):
    """Versions written before manifests have an empty file list and are read
    by globbing their directory — including as an append base."""
    info = catalog.write("events", rows(0, 10))
    # Simulate a pre-manifest row.
    with catalog.store._conn() as c:
        c.execute(
            "UPDATE dataset_versions SET files_json = '[]' WHERE dataset = ? AND version = ?",
            ("events", info.version),
        )
    assert catalog.store.get_version("events", None).files == []
    assert catalog.read("events").num_rows == 10

    v2 = catalog.append("events", rows(10, 5))
    assert v2.row_count == 15
    assert len(v2.files) == 2
    assert catalog.read("events").num_rows == 15


# -- streaming append (the connector sync path) --------------------------------

def test_append_batches_streams_delta(catalog):
    catalog.write("events", rows(0, 20))
    info = catalog.append_batches("events", iter([rows(20, 5), rows(25, 5)]))
    assert info.row_count == 30
    assert catalog.read("events").num_rows == 30
    # One new part regardless of how many chunks streamed in.
    assert len(info.files) == 2


def test_append_batches_no_new_rows_is_a_noop(catalog):
    first = catalog.write("events", rows(0, 10))
    info = catalog.append_batches("events", iter([]))
    assert info.version == first.version, "empty delta must not mint a version"
    assert not list(catalog.workspace.data_dir.glob(".tmp-*"))


def test_upload_file_append_mode(catalog, tmp_path):
    csv = tmp_path / "more.csv"
    csv.write_text("id,name\n1,one\n2,two\n")
    catalog.upload_file("uploaded", csv)
    assert catalog.read("uploaded").num_rows == 2

    more = tmp_path / "more2.csv"
    more.write_text("id,name\n3,three\n")
    info = catalog.upload_file("uploaded", more, mode="append")
    assert info.row_count == 3
    assert sorted(catalog.read("uploaded").column("id").to_pylist()) == [1, 2, 3]
