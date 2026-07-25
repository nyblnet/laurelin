"""Tests for laurelin.catalog.DatasetCatalog."""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore


@pytest.fixture()
def workspace(tmp_path: Path) -> Workspace:
    return Workspace.init(tmp_path / "ws", name="test-ws")


@pytest.fixture()
def catalog(workspace: Workspace) -> DatasetCatalog:
    return DatasetCatalog(workspace, MetadataStore(workspace.metadata_path))


def simple_table(n: int = 3, start: int = 0) -> pa.Table:
    return pa.table(
        {
            "id": list(range(start, start + n)),
            "label": [f"row-{i}" for i in range(start, start + n)],
        }
    )


# -- create_dataset ------------------------------------------------------------


def test_create_dataset(catalog: DatasetCatalog):
    info = catalog.create_dataset("flights", "flight data")
    assert info.name == "flights"
    assert info.description == "flight data"
    assert info.latest_version is None


@pytest.mark.parametrize(
    "bad", ["Flights", "1abc", "with-dash", "with space", "", "_leading", "café"]
)
def test_create_dataset_bad_name(catalog: DatasetCatalog, bad: str):
    with pytest.raises(ValueError):
        catalog.create_dataset(bad)


def test_write_bad_name(catalog: DatasetCatalog):
    with pytest.raises(ValueError):
        catalog.write("Bad-Name", simple_table())


# -- write / versioning ----------------------------------------------------------


def test_write_autocreates_dataset_and_increments_versions(
    catalog: DatasetCatalog, workspace: Workspace
):
    v1 = catalog.write("ds", simple_table(3))
    assert v1.version == 1
    assert v1.row_count == 3
    assert v1.source == "upload"
    assert catalog.store.get_dataset("ds") is not None

    v2 = catalog.write("ds", simple_table(5), source="transform", build_id="b1")
    assert v2.version == 2
    assert v2.build_id == "b1"
    assert v2.source == "transform"

    assert catalog.store.get_dataset("ds").latest_version == 2
    assert (workspace.data_dir / "ds" / "v0001" / "data.parquet").exists()
    assert (workspace.data_dir / "ds" / "v0002" / "data.parquet").exists()


def test_write_path_is_workspace_relative(catalog: DatasetCatalog, workspace: Workspace):
    v = catalog.write("ds", simple_table())
    assert not Path(v.path).is_absolute()
    assert v.path == str(Path("data") / "ds" / "v0001")
    assert (workspace.root / v.path / "data.parquet").exists()


def test_write_leaves_no_temp_dirs(catalog: DatasetCatalog, workspace: Workspace):
    catalog.write("ds", simple_table())
    leftovers = [p for p in workspace.data_dir.iterdir() if p.name.startswith(".tmp")]
    assert leftovers == []


def test_versions_are_immutable(catalog: DatasetCatalog):
    catalog.write("ds", simple_table(3))
    catalog.write("ds", simple_table(10))
    t1 = catalog.read("ds", version=1)
    assert t1.num_rows == 3  # v1 untouched by later writes


def test_schema_capture(catalog: DatasetCatalog):
    table = pa.table(
        {
            "a": pa.array([1, 2], type=pa.int64()),
            "b": pa.array(["x", "y"], type=pa.string()),
            "c": pa.array([1.5, 2.5], type=pa.float64()),
        }
    )
    v = catalog.write("ds", table)
    schema = {s.name: s.type for s in v.schema_}
    assert schema == {"a": "int64", "b": "string", "c": "double"}


# -- read --------------------------------------------------------------------


def test_read_latest_and_specific(catalog: DatasetCatalog):
    catalog.write("ds", simple_table(3))
    catalog.write("ds", simple_table(7))
    assert catalog.read("ds").num_rows == 7
    assert catalog.read("ds", version=1).num_rows == 3
    assert catalog.read("ds", version=2).num_rows == 7


def test_read_roundtrip_preserves_data(catalog: DatasetCatalog):
    table = simple_table(4)
    catalog.write("ds", table)
    assert catalog.read("ds").equals(table)


def test_read_missing_dataset(catalog: DatasetCatalog):
    with pytest.raises(KeyError):
        catalog.read("nope")


def test_read_missing_version(catalog: DatasetCatalog):
    catalog.write("ds", simple_table())
    with pytest.raises(KeyError):
        catalog.read("ds", version=99)


def test_read_dataset_with_no_versions(catalog: DatasetCatalog):
    catalog.create_dataset("empty")
    with pytest.raises(KeyError):
        catalog.read("empty")


# -- parquet_glob --------------------------------------------------------------


def test_parquet_glob_absolute(catalog: DatasetCatalog, workspace: Workspace):
    catalog.write("ds", simple_table())
    listing = catalog.parquet_glob("ds")
    # A SQL list literal of absolute paths, so a multi-part (appended) version
    # works the same as a single file.
    assert listing.startswith("[") and listing.endswith("]")
    assert str(workspace.root) in listing
    for path in catalog.version_files("ds"):
        assert Path(path).is_absolute()
    import duckdb

    n = duckdb.connect().execute(
        f"SELECT count(*) FROM read_parquet({listing})"
    ).fetchone()[0]
    assert n == 3


def test_parquet_glob_missing(catalog: DatasetCatalog):
    with pytest.raises(KeyError):
        catalog.parquet_glob("nope")


# -- rows -----------------------------------------------------------------------


def test_rows_paging(catalog: DatasetCatalog):
    catalog.write("ds", simple_table(10))
    page = catalog.rows("ds", limit=3, offset=4)
    assert len(page) == 3
    assert [r["id"] for r in page] == [4, 5, 6]
    assert catalog.rows("ds", limit=100, offset=8) == [
        {"id": 8, "label": "row-8"},
        {"id": 9, "label": "row-9"},
    ]


def test_rows_specific_version(catalog: DatasetCatalog):
    catalog.write("ds", simple_table(2))
    catalog.write("ds", simple_table(2, start=100))
    assert [r["id"] for r in catalog.rows("ds", version=1)] == [0, 1]
    assert [r["id"] for r in catalog.rows("ds")] == [100, 101]


def test_rows_json_safety(catalog: DatasetCatalog):
    import json
    from decimal import Decimal

    table = pa.table(
        {
            "ts": pa.array(
                [dt.datetime(2026, 7, 7, 12, 30, 0), None],
                type=pa.timestamp("us"),
            ),
            "d": pa.array([dt.date(2026, 1, 2), None], type=pa.date32()),
            "f": pa.array([float("nan"), 1.5], type=pa.float64()),
            "dec": pa.array([Decimal("1.23"), None], type=pa.decimal128(10, 2)),
            "b": pa.array([b"\x00\x01", None], type=pa.binary()),
        }
    )
    catalog.write("ds", table)
    rows = catalog.rows("ds")
    json.dumps(rows)  # everything must be JSON-serializable
    assert rows[0]["ts"] == "2026-07-07T12:30:00"
    assert rows[0]["d"] == "2026-01-02"
    assert rows[0]["f"] is None  # NaN -> None
    assert rows[1]["f"] == 1.5
    assert isinstance(rows[0]["dec"], str)
    assert rows[1]["ts"] is None


def test_rows_missing_dataset(catalog: DatasetCatalog):
    with pytest.raises(KeyError):
        catalog.rows("nope")


# -- upload_file -------------------------------------------------------------------


def test_upload_csv(catalog: DatasetCatalog, tmp_path: Path):
    csv = tmp_path / "in.csv"
    csv.write_text("id,name,score\n1,alice,9.5\n2,bob,7.0\n")
    v = catalog.upload_file("people", csv, description="people csv")
    assert v.version == 1
    assert v.row_count == 2
    assert v.source == "upload"
    rows = catalog.rows("people")
    assert rows[0]["name"] == "alice"
    assert rows[1]["score"] == 7.0
    types = {s.name: s.type for s in v.schema_}
    assert types["id"] == "int64"


def test_upload_parquet(catalog: DatasetCatalog, tmp_path: Path):
    p = tmp_path / "in.parquet"
    pq.write_table(simple_table(4), p)
    v = catalog.upload_file("ds", p)
    assert v.row_count == 4
    assert catalog.read("ds").num_rows == 4


def test_upload_unsupported_extension(catalog: DatasetCatalog, tmp_path: Path):
    f = tmp_path / "in.json"
    f.write_text("{}")
    with pytest.raises(ValueError):
        catalog.upload_file("ds", f)


def test_upload_missing_file(catalog: DatasetCatalog, tmp_path: Path):
    with pytest.raises(ValueError):
        catalog.upload_file("ds", tmp_path / "absent.csv")


def test_upload_bad_name(catalog: DatasetCatalog, tmp_path: Path):
    csv = tmp_path / "in.csv"
    csv.write_text("a\n1\n")
    with pytest.raises(ValueError):
        catalog.upload_file("Bad Name", csv)
