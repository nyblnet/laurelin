"""ClickHouse-backed datasets: the plumbing around the governance.

Same trick as ``tests/test_federation.py`` — a local Parquet file *is* the
"remote" source, because chdb reads it through ``file(path, Parquet)``. No
ClickHouse server, no container, no service in CI.

The read paths covered here are mostly ones that needed **no code change**:
``rows``, ``scan_for`` and ``iter_batches`` already branched on
``scans_at_source`` rather than on the dataset kind. That is the payoff of the
existing abstraction, and the point of testing it is to prove the abstraction
held rather than to test new code.

The correctness of what those paths *return* under a policy is
``tests/test_clickhouse_governance.py``; this file is about wiring.
"""

import io

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core import clickhouse, limits
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Role, User
from laurelin.core.permissions import PermissionService

pytestmark = pytest.mark.skipif(
    not clickhouse.available(), reason="needs chdb: pip install 'laurelin[clickhouse]'"
)

VIEWER = User(id="1", username="vic", role=Role.viewer)
ADMIN = User(id="2", username="ada", role=Role.admin)
CREDS = {"username": "root", "password": "trustno1!"}


def events(n: int = 30) -> pa.Table:
    return pa.table({
        "id": pa.array(range(n), type=pa.int64()),
        "region": [["us", "eu", "apac"][i % 3] for i in range(n)],
        "ssn": [f"{i:03d}-00-0000" for i in range(n)],
    })


@pytest.fixture()
def env(tmp_path):
    remote = tmp_path / "remote" / "events.parquet"
    remote.parent.mkdir(parents=True)
    pq.write_table(events(), remote)

    ws = Workspace.init(tmp_path / "ws", name="ch")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    return catalog, store, PermissionService(store), {"type": "parquet",
                                                      "path": str(remote)}


# -- validation & redaction ----------------------------------------------------

@pytest.mark.parametrize("source, msg", [
    ({"type": "server"}, "Unknown ClickHouse source type"),
    ({"type": "mergetree"}, "server-backed sources are not implemented"),
    ({"type": "parquet"}, "needs a 'path'"),
    ({"type": "parquet", "path": "   "}, "needs a 'path'"),
    ({"type": "parquet", "path": "file:///etc/passwd"}, "file:// paths are not allowed"),
])
def test_validate_rejects(source, msg):
    with pytest.raises(ValueError, match=msg):
        clickhouse.validate_source(source)


def test_validate_accepts_and_normalises():
    assert clickhouse.validate_source(
        {"type": "parquet", "path": " /data/x.parquet ", "junk": 1}
    ) == {"type": "parquet", "path": "/data/x.parquet"}


def test_the_scan_expression_escapes_its_path():
    """The path goes through the same escaper as a policy value, because
    ClickHouse gives us nowhere else to put it."""
    expr = clickhouse.scan_expression({"type": "parquet", "path": "/a'b\\c.parquet"})
    assert expr == "file('/a\\'b\\\\c.parquet', Parquet)"


def test_source_secrets_are_redacted():
    red = clickhouse.redacted_source(
        {"type": "parquet", "path": "/x.parquet", "access_key": "AKIA", "secret": "s"}
    )
    assert red["access_key"] == "*****" and red["secret"] == "*****"
    assert red["path"] == "/x.parquet"


# -- registration --------------------------------------------------------------

def test_register_and_read(env):
    catalog, store, perms, source = env
    info = catalog.register_clickhouse("events", source, "clickhouse events")

    assert info.is_clickhouse and info.kind == "clickhouse"
    assert info.scans_at_source and info.sql_dialect == "clickhouse"
    assert info.latest_version is None, "a ClickHouse table has no versions"
    assert catalog.read("events").num_rows == 30


def test_register_probes_and_fails_loudly(env, tmp_path):
    catalog, _, _, _ = env
    with pytest.raises(clickhouse.ClickHouseError):
        catalog.register_clickhouse(
            "nope", {"type": "parquet", "path": str(tmp_path / "ghost.parquet")}
        )
    assert catalog.store.get_dataset("nope") is None, "a failed probe stores nothing"


def test_an_empty_column_list_is_a_refusal(env, monkeypatch):
    """The other half of "column discovery failure is a refusal": if DESCRIBE
    comes back with nothing, there is no column set to mask against."""
    catalog, _, _, source = env
    monkeypatch.setattr(clickhouse, "run", lambda sql: pa.table({}))
    with pytest.raises(clickhouse.ClickHouseError, match="no columns"):
        clickhouse.schema_of(source)


# -- the read paths that needed no change --------------------------------------

def test_rows_pages_at_the_source(env):
    catalog, store, perms, source = env
    catalog.register_clickhouse("events", source)
    assert len(catalog.rows("events", limit=5)) == 5
    assert catalog.rows("events", limit=2, offset=1)[0]["id"] == 1


def test_scan_for_reaches_the_source(env):
    catalog, store, perms, source = env
    catalog.register_clickhouse("events", source)
    store.set_dataset_policy("events", {
        "dataset": "events",
        "row_policy": {"column": "region", "rules": [
            {"subject_kind": "user", "subject": "vic", "values": ["eu"]}]},
        "column_masks": [],
    })
    scan = catalog.scan_for("events", plan_for=perms.arrow_policy_fn(VIEWER))
    table = scan if isinstance(scan, pa.Table) else scan.to_table()
    assert set(table.column("region").to_pylist()) == {"eu"}


def test_iter_batches_yields_the_table_whole(env):
    """Honest rather than pretending to stream something already collected."""
    catalog, store, perms, source = env
    catalog.register_clickhouse("events", source)
    batches = list(catalog.iter_batches("events"))
    assert len(batches) == 1 and batches[0].num_rows == 30


# -- the workbench gate --------------------------------------------------------

def test_workbench_excludes_clickhouse_by_default(env, monkeypatch):
    catalog, store, perms, source = env
    catalog.register_clickhouse("events", source)
    monkeypatch.delenv("LAURELIN_FEDERATION_WORKBENCH", raising=False)

    with pytest.raises(Exception):
        catalog.query(
            "SELECT count(*) AS n FROM events",
            plan_for=perms.arrow_policy_fn(ADMIN),
            sql_policy_for=perms.sql_policy_fn(ADMIN),
        )


def test_workbench_includes_clickhouse_when_enabled(env, monkeypatch):
    catalog, store, perms, source = env
    catalog.register_clickhouse("events", source)
    monkeypatch.setenv("LAURELIN_FEDERATION_WORKBENCH", "1")

    result = catalog.query(
        "SELECT count(*) AS n FROM events",
        plan_for=perms.arrow_policy_fn(ADMIN),
        sql_policy_for=perms.sql_policy_fn(ADMIN),
    )
    assert result["rows"][0]["n"] == 30


def test_the_workbench_never_registers_an_unpolicied_clickhouse_table(env, monkeypatch):
    """Both conditions must hold, not either — the gate being on must not be
    enough to expose a table with no policy renderer attached."""
    catalog, store, perms, source = env
    catalog.register_clickhouse("events", source)
    monkeypatch.setenv("LAURELIN_FEDERATION_WORKBENCH", "1")

    with pytest.raises(Exception):
        catalog.query("SELECT count(*) FROM events", sql_policy_for=None)


# -- writes are refused --------------------------------------------------------

def test_write_append_and_upload_all_refuse(env, tmp_path):
    """A write would land local Parquet parts and mint a version row while
    read() kept returning the remote table — half managed, half remote, and
    reporting only the half you did not write."""
    catalog, store, perms, source = env
    catalog.register_clickhouse("events", source)

    for call in (
        lambda: catalog.write("events", events(1)),
        lambda: catalog.append("events", events(1)),
        lambda: catalog.write_batches("events", [events(1)]),
        lambda: catalog.append_batches("events", [events(1)]),
    ):
        with pytest.raises(ValueError, match="scanned at the source"):
            call()

    csv = tmp_path / "more.csv"
    csv.write_text("id,region,ssn\n99,us,x\n")
    with pytest.raises(ValueError, match="scanned at the source"):
        catalog.upload_file("events", csv)

    assert store.get_dataset("events").latest_version is None


# -- transforms: reduce at the boundary ----------------------------------------

PIPELINE = """
from laurelin.transforms import sql_transform, Input, Output

@sql_transform(
    output=Output("events_by_region"),
    inputs={"e": Input("events")},
    query="SELECT region, count(*) AS n FROM e GROUP BY region ORDER BY region",
)
def rollup(): ...
"""


def test_a_transform_reduces_a_clickhouse_table_into_a_managed_one(env):
    from laurelin.transforms import Builder, collect_transforms

    catalog, store, perms, source = env
    catalog.register_clickhouse("events", source)
    (catalog.workspace.pipelines_dir / "p.py").write_text(PIPELINE)

    registry = collect_transforms(catalog.workspace.pipelines_dir)
    build = Builder(catalog.workspace, catalog, store, registry).build()
    assert build.status.value == "succeeded", build.tasks

    rollup = store.get_dataset("events_by_region")
    assert rollup.kind == "managed" and rollup.latest_version == 1
    rows = {r["region"]: r["n"] for r in catalog.rows("events_by_region", limit=10)}
    assert rows == {"apac": 10, "eu": 10, "us": 10}


# -- the ontology guard --------------------------------------------------------

ONTOLOGY = """
object_types:
  - api_name: event
    backing_dataset: events
    primary_key: id
    properties:
      id: {type: integer}
      region: {type: string}
"""


def test_ontology_refuses_a_clickhouse_backing_dataset(env):
    from laurelin.ontology import OntologyService, load_ontology

    catalog, store, perms, source = env
    catalog.register_clickhouse("events", source)
    (catalog.workspace.ontology_dir / "o.yml").write_text(ONTOLOGY)

    svc = OntologyService(
        catalog.workspace, catalog, store, load_ontology(catalog.workspace.ontology_dir)
    )
    with pytest.raises(ValueError, match="scanned at the source"):
        svc.query("event", limit=10)


# -- limits --------------------------------------------------------------------

def test_settings_carry_the_budget():
    budget = limits.QueryLimits(memory_limit="512MB", timeout_s=7.5)
    settings = limits.clickhouse_settings(budget)
    assert "max_execution_time=7.5" in settings
    assert f"max_memory_usage={512 * 1024 * 1024}" in settings
    assert "transform_null_in=0" in settings

    # A disabled timeout emits no clause rather than "0", which ClickHouse
    # would read as "no limit" anyway but which reads as a bug.
    assert "max_execution_time" not in limits.clickhouse_settings(
        limits.QueryLimits(timeout_s=0)
    )


def test_a_timeout_is_a_408_and_an_over_large_query_is_a_413():
    """Without the translator both flatten into a generic 400 and the API loses
    the distinction the route deliberately preserves. Codes 159/241 are what
    chdb 4.2.1 actually raises."""
    budget = limits.QueryLimits(memory_limit="1GB", timeout_s=1)

    with pytest.raises(limits.QueryTimeout):
        with limits.clickhouse_guard(budget):
            clickhouse.run(
                "SELECT count() FROM numbers(100000000000) SETTINGS max_execution_time=1"
            )

    with pytest.raises(limits.QueryTooLarge):
        with limits.clickhouse_guard(budget):
            clickhouse.run(
                "SELECT groupArray(number) FROM numbers(100000000) "
                "SETTINGS max_memory_usage=100000000"
            )

    # Anything else keeps its own identity rather than being mislabelled.
    with pytest.raises(clickhouse.ClickHouseError):
        with limits.clickhouse_guard(budget):
            clickhouse.run("SELECT nonexistent_function(1)")


# -- HTTP ----------------------------------------------------------------------

def _admin(tmp_path, name="chapi"):
    ws = Workspace.init(tmp_path / name, name=name)
    app = create_app(ws)
    admin = TestClient(app)
    admin.post("/api/v1/auth/setup", json=CREDS)
    admin.post("/api/v1/auth/login", json=CREDS)
    return app, admin


def test_registration_is_admin_only(tmp_path):
    remote = tmp_path / "r.parquet"
    pq.write_table(events(5), remote)
    app, admin = _admin(tmp_path)
    admin.post("/api/v1/users",
               json={"username": "ed", "password": "password123", "role": "editor"})
    editor = TestClient(app)
    editor.post("/api/v1/auth/login",
                json={"username": "ed", "password": "password123"})

    body = {"source": {"type": "parquet", "path": str(remote)}}
    assert editor.put("/api/v1/datasets/ch_events/clickhouse",
                      json=body).status_code == 403

    r = admin.put("/api/v1/datasets/ch_events/clickhouse", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "clickhouse"

    # A bad source is a 400; an unreachable one a 502.
    assert admin.put("/api/v1/datasets/bad/clickhouse",
                     json={"source": {"type": "nope"}}).status_code == 400
    assert admin.put("/api/v1/datasets/gone/clickhouse",
                     json={"source": {"type": "parquet",
                                      "path": str(tmp_path / "missing.parquet")}}
                     ).status_code == 502


def test_cannot_shadow_a_managed_dataset(tmp_path):
    remote = tmp_path / "r.parquet"
    pq.write_table(events(5), remote)
    _, admin = _admin(tmp_path, "shadow")
    admin.post("/api/v1/datasets", json={"name": "orders"})
    r = admin.put("/api/v1/datasets/orders/clickhouse",
                  json={"source": {"type": "parquet", "path": str(remote)}})
    assert r.status_code == 409


def test_rows_endpoint_previews_a_clickhouse_dataset(tmp_path):
    remote = tmp_path / "r.parquet"
    pq.write_table(events(7), remote)
    _, admin = _admin(tmp_path, "rows")
    admin.put("/api/v1/datasets/remote/clickhouse",
              json={"source": {"type": "parquet", "path": str(remote)}})

    body = admin.get("/api/v1/datasets/remote/rows?limit=3").json()
    assert len(body["rows"]) == 3
    assert body["row_count"] is None, "unknown total, not a fabricated one"


def test_uploading_to_a_clickhouse_dataset_is_refused_over_http(tmp_path):
    remote = tmp_path / "r.parquet"
    pq.write_table(events(5), remote)
    _, admin = _admin(tmp_path, "noup")
    admin.put("/api/v1/datasets/remote/clickhouse",
              json={"source": {"type": "parquet", "path": str(remote)}})

    files = {"file": ("d.csv", io.BytesIO(b"id,region,ssn\n1,us,x\n"), "text/csv")}
    r = admin.post("/api/v1/datasets/remote/upload", files=files)
    assert r.status_code == 400, r.text
    assert "scanned at the source" in r.text


def test_the_dataset_list_no_longer_leaks_the_source(tmp_path):
    """Pre-existing hole, made acute by a second kind of source: GET /datasets
    returned DatasetInfo.source verbatim to any viewer, credentials included."""
    _, admin = _admin(tmp_path, "leak")
    # Registering a postgres source would need a live server to probe, so the
    # row is written directly — the hole is in serialization, not registration.
    ws = Workspace(tmp_path / "leak")
    store = MetadataStore(ws.metadata_path)
    store.upsert_dataset("fed", "")
    store.set_dataset_source("fed", "federated", {
        "type": "postgres", "url": "postgresql://alice:hunter2@db/prod",
        "table": "public.events", "password": "hunter2",
    })

    listed = admin.get("/api/v1/datasets").json()
    fed = next(d for d in listed if d["name"] == "fed")
    assert "hunter2" not in str(fed)
    assert fed["source"]["password"] == "*****"

    detail = admin.get("/api/v1/datasets/fed").json()
    assert "hunter2" not in str(detail)
