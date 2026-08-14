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

import io
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


# -- compaction ---------------------------------------------------------------
#
# `compact()` on an Iceberg dataset did not fail, which is why nobody noticed.
# `write()` is exempted from the scanned-at-source refusal for Iceberg (Laurelin
# really does write that table, via `write_iceberg`), so compaction wrote a
# local Parquet part nothing reads and registered a version row with no
# snapshot id. Three consequences, measured on a two-snapshot table: the Iceberg
# table was untouched, the audit reported `parts_before: 0`, and reading that
# version fell through to "no snapshot pinned, read the table as it is now".


def test_compacting_an_iceberg_dataset_merges_its_data_files(cat):
    cat.write_iceberg("orders", FIRST)
    cat.write_iceberg("orders", MORE, mode="append")
    ice = cat._iceberg()
    assert ice.data_files("orders") == 2, "two appends, two data files"

    result = cat.compact("orders")

    assert ice.data_files("orders") == 1, "compaction has to compact something"
    assert result.row_count == 4 and cat.read("orders").num_rows == 4
    assert result.source == "compact"


def test_a_compacted_iceberg_version_pins_the_snapshot_it_made(cat):
    """The bug that cannot be seen by looking: a version row with
    `snapshot_id = NULL` reads as "now" forever, so time travel to it returns
    whatever the table holds later — five rows from a version whose own row
    says four."""
    cat.write_iceberg("orders", FIRST)
    compacted = cat.compact("orders")
    assert compacted.snapshot_id is not None

    cat.write_iceberg("orders", MORE, mode="append")

    assert cat.read("orders").num_rows == 4
    assert cat.read("orders", version=compacted.version).num_rows == 3, (
        "a compacted version means the rows it compacted, not the rows there are now"
    )
    assert cat.read("orders", version=1).num_rows == 3, "history still readable"


def test_compacting_an_iceberg_dataset_writes_no_laurelin_parts(cat, tmp_path):
    """An Iceberg dataset's bytes live in the warehouse. A Parquet part under
    `data/` would be a dataset half managed and half remote — exactly what
    `_refuse_write_at_source` exists to prevent everywhere else."""
    cat.write_iceberg("orders", FIRST)
    cat.compact("orders")

    parts = list((tmp_path / "ws" / "data" / "orders").rglob("*.parquet"))
    assert parts == [], f"compaction left orphan parts: {parts}"


def test_the_iceberg_compaction_audit_counts_real_files(cat):
    cat.write_iceberg("orders", FIRST)
    cat.write_iceberg("orders", MORE, mode="append")
    cat.compact("orders")

    entry = next(e for e in cat.store.list_audit(limit=20)
                 if e.action == "dataset_compacted")
    assert entry.details["parts_before"] == 2, "0 was the old lie"
    assert entry.details["parts_after"] == 1
    assert entry.details["format"] == "iceberg"


def test_compacting_a_dataset_with_no_versions_is_an_error(cat):
    cat.create_iceberg_dataset("orders")
    with pytest.raises(KeyError, match="no versions"):
        cat.compact("orders")


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


# -- branches -----------------------------------------------------------------
#
# A branch is a named pointer into the snapshot history, so cutting one copies
# no data. What it buys is the thing "build against staging" needs: work that
# readers of main cannot see until it's merged.

def test_a_branch_is_isolated_from_main(cat):
    cat.write_iceberg("orders", FIRST)
    cat.iceberg_branch("orders", "staging")
    cat._iceberg().write("orders", MORE, mode="append", branch="staging")

    assert cat.read("orders").num_rows == 3, "main must not see branch writes"
    assert cat._iceberg().read("orders", branch="staging").num_rows == 4


def test_branches_are_listed_with_main(cat):
    cat.write_iceberg("orders", FIRST)
    cat.iceberg_branch("orders", "staging")
    assert [b["branch"] for b in cat.iceberg_branches("orders")] == ["main", "staging"]


def test_merging_fast_forwards_main_and_records_a_version(cat):
    cat.write_iceberg("orders", FIRST)
    cat.iceberg_branch("orders", "staging")
    cat._iceberg().write("orders", MORE, mode="append", branch="staging")

    merged = cat.merge_iceberg_branch("orders", "staging")
    assert cat.read("orders").num_rows == 4
    # Without the version row the merge would be invisible to lineage, builds
    # and time travel, which all speak in Laurelin versions.
    assert merged.source == "merge:staging"
    assert cat.store.get_dataset("orders").latest_version == merged.version


def test_history_survives_a_merge(cat):
    cat.write_iceberg("orders", FIRST)
    cat.iceberg_branch("orders", "staging")
    cat._iceberg().write("orders", MORE, mode="append", branch="staging")
    cat.merge_iceberg_branch("orders", "staging")
    assert cat.read("orders", version=1).num_rows == 3


def test_a_diverged_branch_refuses_to_merge(cat):
    """Fast-forward only. A three-way merge needs a row-level conflict policy,
    and guessing one silently picks a winner between two people's writes."""
    cat.write_iceberg("orders", FIRST)
    cat.iceberg_branch("orders", "staging")
    cat._iceberg().write("orders", MORE, mode="append", branch="staging")
    cat.write_iceberg("orders", MORE, mode="append")  # main moves on

    with pytest.raises(ValueError, match="diverged"):
        cat.merge_iceberg_branch("orders", "staging")


def test_branching_from_an_older_version(cat):
    cat.write_iceberg("orders", FIRST)
    cat.write_iceberg("orders", MORE, mode="append")
    cat.iceberg_branch("orders", "rewind", from_version=1)
    assert cat._iceberg().read("orders", branch="rewind").num_rows == 3


@pytest.mark.parametrize("branch, message", [
    ("main", "trunk"),
    ("Not A Branch", "Invalid branch name"),
])
def test_bad_branch_names_are_rejected(cat, branch, message):
    cat.write_iceberg("orders", FIRST)
    with pytest.raises(ValueError, match=message):
        cat.iceberg_branch("orders", branch)


def test_main_cannot_be_deleted(cat):
    cat.write_iceberg("orders", FIRST)
    with pytest.raises(ValueError, match="Refusing to delete"):
        cat.delete_iceberg_branch("orders", "main")


# -- schema evolution ---------------------------------------------------------

def test_adding_a_column_is_additive_and_keeps_history_readable(cat):
    cat.write_iceberg("orders", FIRST)
    columns = cat.evolve_iceberg_schema("orders", add={"priority": "string"})
    assert columns == ["id", "region", "amount", "priority"]
    # Iceberg tracks columns by id, so the old snapshot still reads.
    assert cat.read("orders", version=1).num_rows == 3


def test_an_unknown_column_type_is_rejected(cat):
    cat.write_iceberg("orders", FIRST)
    with pytest.raises(ValueError, match="Unknown column type"):
        cat.evolve_iceberg_schema("orders", add={"x": "blob"})


def test_dropping_a_column_needs_explicit_consent(cat):
    """The point isn't to forbid the change — it's to stop it being made
    without seeing what breaks."""
    cat.write_iceberg("orders", FIRST)
    with pytest.raises(ValueError, match="breaking change"):
        cat.evolve_iceberg_schema("orders", drop=["amount"])

    columns = cat.evolve_iceberg_schema("orders", drop=["amount"], allow_breaking=True)
    assert columns == ["id", "region"]


def test_a_breaking_change_names_what_is_downstream(cat):
    from laurelin.core.models import LineageEdge

    cat.write_iceberg("orders", FIRST)
    cat.store.replace_lineage_for_transform("roll_up", [
        LineageEdge(upstream_dataset="orders", downstream_dataset="daily",
                    transform_name="roll_up"),
    ])
    cat.store.replace_lineage_for_transform("summarize", [
        LineageEdge(upstream_dataset="daily", downstream_dataset="monthly",
                    transform_name="summarize"),
    ])
    # Transitive: breaking `orders` breaks `monthly` too, via `daily`.
    assert cat.downstream_of("orders") == ["daily", "monthly"]

    with pytest.raises(ValueError, match="daily, monthly"):
        cat.evolve_iceberg_schema("orders", rename={"amount": "value"})


def test_renaming_a_column_works_once_allowed(cat):
    cat.write_iceberg("orders", FIRST)
    columns = cat.evolve_iceberg_schema(
        "orders", rename={"amount": "value"}, allow_breaking=True
    )
    assert columns == ["id", "region", "value"]


# -- HTTP routes --------------------------------------------------------------
#
# Every Iceberg capability the catalog implements had no REST surface, so the
# feature the CHANGELOG advertises could only be driven from Python. These
# cover the routes that make it reachable.


def _client(tmp_path):
    from fastapi.testclient import TestClient

    from laurelin.api import create_app

    ws = Workspace.init(tmp_path / "ws", name="ibhttp")
    return TestClient(create_app(ws, no_auth=True))


def _csv(rows="id,region\n1,us\n2,eu\n3,us\n"):
    return {"file": ("d.csv", io.BytesIO(rows.encode()), "text/csv")}


def test_create_and_snapshot_over_http(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/v1/datasets/orders/iceberg", files=_csv())
    assert r.status_code == 200, r.text
    assert r.json()["row_count"] == 3

    r = c.post("/api/v1/datasets/orders/iceberg?mode=append",
               files=_csv("id,region\n4,apac\n"))
    assert r.json()["version"] == 2

    snaps = c.get("/api/v1/datasets/orders/iceberg/snapshots").json()
    assert len(snaps) == 2


def test_branch_lifecycle_over_http(tmp_path):
    c = _client(tmp_path)
    c.post("/api/v1/datasets/orders/iceberg", files=_csv())

    assert c.post("/api/v1/datasets/orders/iceberg/branches",
                  json={"branch": "staging"}).status_code == 200
    branches = c.get("/api/v1/datasets/orders/iceberg/branches").json()
    assert {b["branch"] for b in branches} == {"main", "staging"}

    merged = c.post("/api/v1/datasets/orders/iceberg/branches/staging/merge")
    assert merged.status_code == 200

    assert c.delete("/api/v1/datasets/orders/iceberg/branches/staging").status_code == 200


def test_a_diverged_merge_is_a_409(tmp_path):
    c = _client(tmp_path)
    c.post("/api/v1/datasets/orders/iceberg", files=_csv())
    c.post("/api/v1/datasets/orders/iceberg/branches", json={"branch": "staging"})
    # move main on so staging is behind and diverged
    c.post("/api/v1/datasets/orders/iceberg?mode=append", files=_csv("id,region\n9,us\n"))

    r = c.post("/api/v1/datasets/orders/iceberg/branches/staging/merge")
    assert r.status_code == 409
    assert "diverged" in r.json()["detail"]


def test_schema_evolution_over_http(tmp_path):
    c = _client(tmp_path)
    c.post("/api/v1/datasets/orders/iceberg", files=_csv())

    # additive is fine
    r = c.post("/api/v1/datasets/orders/iceberg/schema",
               json={"add": {"priority": "string"}})
    assert r.status_code == 200
    assert "priority" in r.json()["columns"]

    # a breaking change is refused without consent, and the impact is queryable
    impact = c.get("/api/v1/datasets/orders/iceberg/schema/impact").json()
    assert "downstream" in impact
    r = c.post("/api/v1/datasets/orders/iceberg/schema", json={"drop": ["region"]})
    assert r.status_code == 400
    assert "breaking" in r.json()["detail"]
    # …and allowed with it
    r = c.post("/api/v1/datasets/orders/iceberg/schema",
               json={"drop": ["region"], "allow_breaking": True})
    assert r.status_code == 200
    assert "region" not in r.json()["columns"]


def test_compaction_over_http_reaches_the_iceberg_path(tmp_path):
    c = _client(tmp_path)
    c.post("/api/v1/datasets/orders/iceberg", files=_csv())
    c.post("/api/v1/datasets/orders/iceberg?mode=append",
           files=_csv("id,region\n4,apac\n"))

    r = c.post("/api/v1/datasets/orders/compact")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 3 and body["row_count"] == 4
    snaps = c.get("/api/v1/datasets/orders/iceberg/snapshots").json()
    assert len(snaps) > 2, "compaction is a snapshot on the table, not a local part"
