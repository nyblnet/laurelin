"""Iceberg-backed datasets.

Laurelin's own format is already open — a version is a manifest of plain
Parquet, and leaving costs `cp -r`. What Iceberg adds is that *other engines
read the table without asking Laurelin*, with its history intact. So the test
that matters most here is the one where DuckDB opens the table directly,
knowing nothing about Laurelin.

Everything else follows from one decision: an Iceberg dataset is written like
a managed one and read like a federated one, so the SQL policy renderer covers
it with no new code.
"""

import warnings

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core import iceberg
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore

pytestmark = pytest.mark.skipif(
    not iceberg.available(), reason="needs pyiceberg: pip install 'laurelin[iceberg]'"
)

# pyiceberg is noisy about deprecations in its own dependencies.
warnings.filterwarnings("ignore")

FIRST = pa.table({
    "id": pa.array([1, 2, 3], type=pa.int64()),
    "region": ["us", "eu", "us"],
    "amount": pa.array([10.0, 20.0, 30.0], type=pa.float64()),
})
MORE = pa.table({
    "id": pa.array([4], type=pa.int64()),
    "region": ["apac"],
    "amount": pa.array([40.0], type=pa.float64()),
})


@pytest.fixture()
def cat(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="ice")
    store = MetadataStore(ws.metadata_path)
    return DatasetCatalog(ws, store)


# -- the point of the exercise ------------------------------------------------

def test_another_engine_can_read_the_table(cat, tmp_path):
    """No lock-in, demonstrated rather than asserted: a DuckDB that has never
    heard of Laurelin opens the table from its Iceberg metadata."""
    import duckdb

    cat.write_iceberg("orders", FIRST)
    cat.write_iceberg("orders", MORE, mode="append")

    location = cat._iceberg().metadata_location("orders").removeprefix("file://")
    con = duckdb.connect()
    try:
        con.execute("INSTALL iceberg; LOAD iceberg;")
        rows = con.execute(
            "SELECT count(*) FROM iceberg_scan(?)", [location]
        ).fetchone()[0]
    finally:
        con.close()
    assert rows == 4


# -- versions are snapshots ---------------------------------------------------

def test_each_write_is_a_version_and_a_snapshot(cat):
    v1 = cat.write_iceberg("orders", FIRST)
    v2 = cat.write_iceberg("orders", MORE, mode="append")

    assert (v1.version, v1.row_count) == (1, 3)
    assert (v2.version, v2.row_count) == (2, 4)
    assert v1.snapshot_id and v2.snapshot_id
    assert v1.snapshot_id != v2.snapshot_id
    assert len(cat.iceberg_snapshots("orders")) == 2


def test_time_travel_by_version(cat):
    cat.write_iceberg("orders", FIRST)
    cat.write_iceberg("orders", MORE, mode="append")

    assert cat.read("orders").num_rows == 4
    assert cat.read("orders", version=1).num_rows == 3, "history must stay readable"
    assert cat.read("orders", version=2).num_rows == 4


def test_replace_is_the_default_and_keeps_history(cat):
    cat.write_iceberg("orders", FIRST)
    cat.write_iceberg("orders", MORE)  # replace, not append

    assert cat.read("orders").num_rows == 1
    assert cat.read("orders", version=1).num_rows == 3, (
        "replacing the contents must not erase the snapshot before it"
    )


def test_an_unknown_version_is_an_error(cat):
    cat.write_iceberg("orders", FIRST)
    with pytest.raises(KeyError):
        cat.read("orders", version=99)


def test_an_unknown_mode_is_rejected(cat):
    with pytest.raises(ValueError, match="replace' or 'append'"):
        cat.write_iceberg("orders", FIRST, mode="merge")


# -- it behaves like any other dataset ----------------------------------------

def test_it_is_a_first_class_dataset(cat):
    cat.write_iceberg("orders", FIRST)
    info = cat.store.get_dataset("orders")

    assert info.kind == "iceberg"
    assert info.is_iceberg and info.scans_at_source
    assert not info.is_federated, "Laurelin owns this one"
    assert info.latest_version == 1


def test_reads_page_and_query_like_anything_else(cat):
    cat.write_iceberg("orders", FIRST)
    cat.write_iceberg("orders", MORE, mode="append")

    assert len(cat.rows("orders", limit=2)) == 2
    result = cat.query(
        "SELECT region, count(*) AS n FROM orders GROUP BY region ORDER BY n DESC"
    )
    assert result["rows"][0] == {"region": "us", "n": 2}


def test_the_workbench_gate_does_not_hide_it(cat, monkeypatch):
    """That gate stops ad-hoc SQL reaching *foreign* systems. An Iceberg table
    Laurelin owns is not one, and must stay visible with federation off."""
    monkeypatch.delenv("LAURELIN_FEDERATION_WORKBENCH", raising=False)
    cat.write_iceberg("orders", FIRST)
    assert cat.query("SELECT count(*) AS n FROM orders")["rows"] == [{"n": 3}]


def test_a_row_policy_still_applies(cat):
    """Reading at source must not become a way around enforcement."""
    cat.write_iceberg("orders", FIRST)

    class OnlyUs:
        lazy = False
        filter = projection = None

        def apply(self, table):
            import pyarrow.compute as pc
            return table.filter(pc.equal(table["region"], "us"))

    scanned = cat.scan_for("orders", plan_for=lambda name, schema: OnlyUs())
    assert scanned.num_rows == 2


# -- configuration ------------------------------------------------------------

def test_the_warehouse_defaults_into_the_workspace(tmp_path, monkeypatch):
    """Embedded mode must work with no configuration at all."""
    monkeypatch.delenv("LAURELIN_ICEBERG_WAREHOUSE", raising=False)
    ws = Workspace.init(tmp_path / "ws", name="ice")
    assert iceberg.warehouse_uri(ws).startswith("file://")
    assert str(ws.root) in iceberg.warehouse_uri(ws)


def test_the_warehouse_can_point_at_object_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURELIN_ICEBERG_WAREHOUSE", "s3://bucket/warehouse")
    ws = Workspace.init(tmp_path / "ws", name="ice")
    assert iceberg.warehouse_uri(ws) == "s3://bucket/warehouse"


def test_a_postgres_metadata_url_becomes_the_iceberg_catalog(tmp_path, monkeypatch):
    """The catalog is the database Laurelin already runs — no REST catalog to
    operate, and replicas share one catalog for free."""
    monkeypatch.delenv("LAURELIN_ICEBERG_CATALOG", raising=False)
    monkeypatch.setenv("LAURELIN_DATABASE_URL", "postgresql://u:p@db:5432/laurelin")
    ws = Workspace.init(tmp_path / "ws", name="ice")
    assert iceberg.catalog_uri(ws) == "postgresql+psycopg://u:p@db:5432/laurelin"


def test_an_invalid_namespace_is_rejected(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="ice")
    with pytest.raises(ValueError, match="namespace"):
        iceberg.IcebergTables(ws, namespace="not a namespace")
