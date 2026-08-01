"""Federated datasets: governed tables whose bytes Laurelin doesn't hold.

Uses local Parquet as the "remote" source so the mechanism is testable without
a warehouse — the scan path, policy compilation and sandboxing are identical
for s3:// or Iceberg, only the extension differs.
"""


import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core import federation
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Role, User
from laurelin.core.permissions import PermissionService

VIEWER = User(id="1", username="vic", role=Role.viewer)
ADMIN = User(id="2", username="ada", role=Role.admin)


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

    ws = Workspace.init(tmp_path / "ws", name="fed")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    return catalog, store, PermissionService(store), {"type": "parquet", "path": str(remote)}


# -- validation & redaction ----------------------------------------------------

@pytest.mark.parametrize("source, msg", [
    ({"type": "warehouse"}, "Unknown federated source type"),
    ({"type": "parquet"}, "needs a 'path'"),
    ({"type": "iceberg", "path": "  "}, "needs a 'path'"),
    ({"type": "parquet", "path": "file:///etc/passwd"}, "file:// paths are not allowed"),
    ({"type": "postgres", "url": "mysql://h/db", "table": "t"}, "postgresql:// url"),
    ({"type": "postgres", "url": "postgresql://h/db", "table": "t; DROP TABLE x"}, "Invalid table"),
])
def test_validate_rejects(source, msg):
    with pytest.raises(ValueError, match=msg):
        federation.validate_source(source)


def test_validate_accepts():
    federation.validate_source({"type": "parquet", "path": "s3://b/p/*.parquet"})
    federation.validate_source({"type": "iceberg", "path": "s3://b/warehouse/db/t"})
    federation.validate_source({"type": "delta", "path": "s3://b/t"})
    federation.validate_source({"type": "postgres", "url": "postgresql://u:p@h/db",
                                "table": "public.events"})


def test_source_secrets_are_redacted():
    red = federation.redacted_source(
        {"type": "postgres", "url": "postgresql://alice:hunter2@db/prod",
         "table": "events", "secret_key": "xyz"}
    )
    assert "hunter2" not in red["url"] and "alice" in red["url"]
    assert red["secret_key"] == "*****"
    assert red["table"] == "events"


def test_scan_expression_binds_values_and_declares_extensions():
    expr, params, exts = federation.scan_expression({"type": "iceberg", "path": "s3://b/t"})
    assert expr == "iceberg_scan(?)" and params == ["s3://b/t"]
    assert set(exts) == {"iceberg", "httpfs"}

    # A local path needs no httpfs; the path is still a bound parameter.
    expr, params, exts = federation.scan_expression({"type": "parquet", "path": "/tmp/x.parquet"})
    assert expr == "read_parquet(?)" and params == ["/tmp/x.parquet"] and exts == []


# -- registration & reading ----------------------------------------------------

def test_register_and_read(env):
    catalog, store, perms, source = env
    info = catalog.register_federated("events", source, "remote events")

    assert info.is_federated and info.kind == "federated"
    assert info.latest_version is None, "a federated table has no versions"
    assert catalog.read("events").num_rows == 30
    assert len(catalog.rows("events", limit=5)) == 5


def test_register_rejects_unreachable_source(env, tmp_path):
    catalog, _, _, _ = env
    with pytest.raises(federation.FederationError):
        catalog.register_federated("nope", {"type": "parquet", "path": str(tmp_path / "ghost.parquet")})


def test_policy_is_applied_at_the_source(env):
    """The row filter and mask are compiled into the remote scan, not applied
    after the data arrives."""
    catalog, store, perms, source = env
    catalog.register_federated("events", source)
    store.set_dataset_policy("events", {
        "dataset": "events",
        "row_policy": {"column": "region", "rules": [
            {"subject_kind": "user", "subject": "vic", "values": ["eu"]}]},
        "column_masks": [{"column": "ssn", "mode": "hash", "exempt": []}],
    })

    table = catalog.federated_table("events", sql_policy_for=perms.sql_policy_fn(VIEWER))
    assert set(table.column("region").to_pylist()) == {"eu"}
    assert all(len(v) == 16 for v in table.column("ssn").to_pylist())  # sha256 prefix

    # …and it matches what the exact table path would have produced.
    unfiltered = catalog.federated_table("events")
    expected = perms.apply_table_policy(VIEWER, "events", unfiltered)
    assert table.to_pylist() == expected.to_pylist()

    # An admin sees everything.
    admin_table = catalog.federated_table("events", sql_policy_for=perms.sql_policy_fn(ADMIN))
    assert admin_table.num_rows == 30


def test_anonymous_reads_nothing(env):
    catalog, store, perms, source = env
    catalog.register_federated("events", source)
    store.set_dataset_policy("events", {
        "dataset": "events",
        "row_policy": {"column": "region", "rules": [
            {"subject_kind": "user", "subject": "vic", "values": ["eu"]}]},
        "column_masks": [],
    })
    assert catalog.federated_table(
        "events", sql_policy_for=perms.sql_policy_fn(None)
    ).num_rows == 0


# -- sandboxing ----------------------------------------------------------------

def test_remote_source_gets_no_local_filesystem_access():
    """Least privilege per source: a source in object storage must not gain
    the ability to read local files along with its network access."""
    remote = {"type": "parquet", "path": "s3://bucket/events/*.parquet"}
    assert not federation.is_local_source(remote)
    con = federation.connect(remote)
    try:
        with pytest.raises(Exception) as err:
            con.execute("SELECT * FROM read_csv_auto('/etc/passwd')").fetchall()
        assert "disabled" in str(err.value).lower()
        with pytest.raises(Exception):  # and the restriction cannot be lifted
            con.execute("SET disabled_filesystems=''")
    finally:
        con.close()


def test_local_source_keeps_the_access_it_needs(env):
    """A federated path on a mounted volume is a legitimate source, so it
    keeps local access — but its config is still locked."""
    catalog, _, _, source = env
    assert federation.is_local_source(source)
    con = federation.connect(source)
    try:
        expr, params, _ = federation.scan_expression(source)
        assert con.execute(f"SELECT count(*) FROM {expr}", params).fetchone()[0] == 30
        with pytest.raises(Exception):
            con.execute("SET lock_configuration=false")
    finally:
        con.close()


# -- workbench exposure is opt-in ----------------------------------------------

def test_workbench_excludes_federated_by_default(env, monkeypatch):
    catalog, store, perms, source = env
    catalog.register_federated("events", source)
    monkeypatch.delenv("LAURELIN_FEDERATION_WORKBENCH", raising=False)

    # Unregistered means "unknown table", exactly like a dataset you can't see.
    with pytest.raises(Exception):
        catalog.query(
            "SELECT count(*) AS n FROM events",
            plan_for=perms.arrow_policy_fn(ADMIN),
            sql_policy_for=perms.sql_policy_fn(ADMIN),
        )


def test_workbench_includes_federated_when_enabled(env, monkeypatch):
    catalog, store, perms, source = env
    catalog.register_federated("events", source)
    monkeypatch.setenv("LAURELIN_FEDERATION_WORKBENCH", "1")

    result = catalog.query(
        "SELECT count(*) AS n FROM events",
        plan_for=perms.arrow_policy_fn(ADMIN),
        sql_policy_for=perms.sql_policy_fn(ADMIN),
    )
    assert result["rows"][0]["n"] == 30


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


def test_transform_reduces_a_federated_table_into_a_managed_one(env):
    """The intended large-data path: scan remotely, land a medium-sized
    managed dataset that the ontology and dashboards can use."""
    from laurelin.transforms import Builder, collect_transforms

    catalog, store, perms, source = env
    catalog.register_federated("events", source)
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


def test_ontology_refuses_a_federated_backing_dataset(env):
    from laurelin.ontology import OntologyService, load_ontology

    catalog, store, perms, source = env
    catalog.register_federated("events", source)
    (catalog.workspace.ontology_dir / "o.yml").write_text(ONTOLOGY)

    svc = OntologyService(
        catalog.workspace, catalog, store, load_ontology(catalog.workspace.ontology_dir)
    )
    with pytest.raises(ValueError, match="federated dataset"):
        svc.query("event", limit=10)


# -- API -----------------------------------------------------------------------

CREDS = {"username": "root", "password": "trustno1!"}


def test_registration_is_admin_only_and_redacts(tmp_path):
    remote = tmp_path / "r.parquet"
    pq.write_table(events(5), remote)
    ws = Workspace.init(tmp_path / "ws2", name="api")
    app = create_app(ws)
    admin = TestClient(app)
    admin.post("/api/v1/auth/setup", json=CREDS)
    admin.post("/api/v1/auth/login", json=CREDS)
    admin.post("/api/v1/users", json={"username": "ed", "password": "password123", "role": "editor"})
    editor = TestClient(app)
    editor.post("/api/v1/auth/login", json={"username": "ed", "password": "password123"})

    body = {"source": {"type": "parquet", "path": str(remote)}}
    assert editor.put("/api/v1/datasets/remote_events/federated", json=body).status_code == 403

    r = admin.put("/api/v1/datasets/remote_events/federated", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "federated"

    # A bad source is a 400; an unreachable one a 502.
    assert admin.put("/api/v1/datasets/bad/federated",
                     json={"source": {"type": "nope"}}).status_code == 400
    assert admin.put("/api/v1/datasets/gone/federated",
                     json={"source": {"type": "parquet",
                                      "path": str(tmp_path / "missing.parquet")}}
                     ).status_code == 502


def test_rows_endpoint_previews_a_federated_dataset(tmp_path):
    """Regression: the rows route required a version, but a federated dataset
    has none — so its row preview 404'd with "has no versions". Nobody hit it
    until the UI could create a federated dataset. It must page at source, with
    a null row_count (counting would be a full remote scan)."""
    remote = tmp_path / "r.parquet"
    pq.write_table(events(7), remote)
    ws = Workspace.init(tmp_path / "wsr", name="rows")
    app = create_app(ws)
    admin = TestClient(app)
    admin.post("/api/v1/auth/setup", json=CREDS)
    admin.post("/api/v1/auth/login", json=CREDS)
    admin.put("/api/v1/datasets/remote/federated",
              json={"source": {"type": "parquet", "path": str(remote)}})

    r = admin.get("/api/v1/datasets/remote/rows?limit=3")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["rows"]) == 3
    assert body["row_count"] is None, "unknown total, not a fabricated one"


def test_cannot_shadow_a_managed_dataset(tmp_path):
    remote = tmp_path / "r.parquet"
    pq.write_table(events(5), remote)
    ws = Workspace.init(tmp_path / "ws3", name="api2")
    app = create_app(ws)
    client = TestClient(app)
    client.post("/api/v1/auth/setup", json=CREDS)
    client.post("/api/v1/auth/login", json=CREDS)
    DatasetCatalog(ws, MetadataStore(ws.metadata_path)).write("owned", events(3))

    r = client.put("/api/v1/datasets/owned/federated",
                   json={"source": {"type": "parquet", "path": str(remote)}})
    assert r.status_code == 409
