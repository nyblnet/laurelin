"""Tests for data connectors (sources): validation, redaction, file/http sync,
permission gating, and the streaming write path."""

import http.server
import os
import threading

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.connectors import redacted_config, validate_source
from laurelin.core import redaction
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore

# -- unit: validation ---------------------------------------------------------

@pytest.mark.parametrize(
    "type_, config, msg",
    [
        ("bigquery", {}, "Unknown source type"),
        ("postgres", {"url": "mysql://x"}, "postgresql:// url"),
        ("postgres", {"url": "postgresql://h/db"}, "exactly one of"),
        ("postgres", {"url": "postgresql://h/db", "table": "a", "query": "SELECT 1"}, "exactly one of"),
        ("postgres", {"url": "postgresql://h/db", "table": "a; DROP TABLE x"}, "Invalid table"),
        ("postgres", {"url": "postgresql://h/db", "table": "a", "batch_size": 0}, "batch_size"),
        ("http", {"url": "ftp://host/f.csv"}, "http:// or https://"),
        ("http", {"url": "https://host/export"}, "format"),
        ("http", {"url": "https://host/f.csv", "headers": {"a": 1}}, "headers"),
        ("file", {}, "needs a 'path'"),
        ("file", {"path": "/data/dump.xml"}, "format"),
    ],
)
def test_validate_source_rejects(type_, config, msg):
    with pytest.raises(ValueError, match=msg):
        validate_source(type_, config)


def test_validate_source_accepts_good_configs():
    validate_source("postgres", {"url": "postgresql://u:p@h/db", "table": "public.orders"})
    validate_source("postgres", {"url": "postgres://h/db", "query": "SELECT 1"})
    validate_source("http", {"url": "https://host/x.parquet", "headers": {"Authorization": "Bearer t"}})
    validate_source("http", {"url": "https://host/export", "format": "csv"})
    validate_source("file", {"path": "/land/*.csv"})


def test_redacted_config_hides_secrets():
    red = redacted_config(
        {
            "url": "postgresql://alice:hunter2@db.internal:5432/prod",
            "api_key": "xyz",
            "headers": {"Authorization": "Bearer tok", "Accept": "text/csv"},
            "table": "orders",
        }
    )
    assert "hunter2" not in red["url"]
    assert "alice" in red["url"] and "db.internal" in red["url"]
    assert red["api_key"] == "*****"
    assert red["table"] == "orders"
    # Header *values* are withheld wholesale now, including this innocent
    # `Accept: text/csv`, which this test used to require be shown. The name
    # denylist that allowed that also allowed `{"X-Api-Key": "SEKRET"}` through
    # verbatim — `api_?key` does not match `Api-Key` — and it would equally
    # allow `Cookie` and `Proxy-Authorization`. Names survive so the operator
    # can still see which headers are set. See tests/test_redaction.py.
    assert red["headers"]["Authorization"] == redaction.WITHHELD
    assert red["headers"]["Accept"] == redaction.WITHHELD


# -- unit: streaming write ----------------------------------------------------

def test_write_batches_streams_and_unifies_schema(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="t")
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    chunks = [
        pa.Table.from_pylist([{"id": 1, "note": None}]),   # note: null type
        pa.Table.from_pylist([{"id": 2, "note": "hi"}]),
    ]
    info = cat.write_batches("streamed", iter(chunks), source="sync:test")
    assert info.row_count == 2
    table = cat.read("streamed")
    assert table.column("note").to_pylist() == [None, "hi"]
    assert str(table.schema.field("note").type) == "string"


def test_write_batches_empty_iterator_fails(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="t")
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    with pytest.raises(ValueError, match="no data"):
        cat.write_batches("empty", iter([]))
    assert not list((ws.data_dir).glob(".tmp-*"))  # temp dir cleaned up


# -- API: file + http sync ----------------------------------------------------

CREDS = {"username": "root", "password": "trustno1!"}


@pytest.fixture()
def clients(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="conn")
    app = create_app(ws)
    admin = TestClient(app)
    assert admin.post("/api/v1/auth/setup", json=CREDS).status_code == 200
    assert admin.post("/api/v1/auth/login", json=CREDS).status_code == 200
    for username, role in (("ed", "editor"), ("vic", "viewer")):
        admin.post(
            "/api/v1/users",
            json={"username": username, "password": "password123", "role": role},
        )
    editor, viewer = TestClient(app), TestClient(app)
    editor.post("/api/v1/auth/login", json={"username": "ed", "password": "password123"})
    viewer.post("/api/v1/auth/login", json={"username": "vic", "password": "password123"})
    return admin, editor, viewer


def test_file_source_lifecycle(clients, tmp_path):
    admin, editor, _ = clients
    csv = tmp_path / "cities.csv"
    csv.write_text("city,pop\nvalmar,120\ntirion,340\n")

    r = admin.put(
        "/api/v1/sources/city_load",
        json={"type": "file", "dataset": "cities", "config": {"path": str(csv)}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["last_sync_status"] is None

    r = editor.post("/api/v1/sources/city_load/sync")
    assert r.status_code == 200, r.text
    assert r.json()["row_count"] == 2
    assert r.json()["source"] == "sync:file"

    rows = admin.get("/api/v1/datasets/cities/rows").json()["rows"]
    assert {row["city"] for row in rows} == {"valmar", "tirion"}

    listed = admin.get("/api/v1/sources").json()
    assert listed[0]["last_sync_status"] == "succeeded"
    assert listed[0]["last_sync_rows"] == 2

    r = admin.delete("/api/v1/sources/city_load")
    assert r.status_code == 200
    assert admin.get("/api/v1/sources/city_load").status_code == 404


@pytest.fixture()
def http_dir(tmp_path):
    serve_dir = tmp_path / "www"
    serve_dir.mkdir()
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(serve_dir), **kw
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield serve_dir, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_http_source_sync_and_failure(clients, http_dir):
    admin, _, _ = clients
    serve_dir, base = http_dir
    (serve_dir / "orders.csv").write_text("id,total\n1,9.5\n2,3.25\n3,7.0\n")

    r = admin.put(
        "/api/v1/sources/orders_pull",
        json={"type": "http", "dataset": "orders", "config": {"url": f"{base}/orders.csv"}},
    )
    assert r.status_code == 200, r.text
    r = admin.post("/api/v1/sources/orders_pull/sync")
    assert r.status_code == 200, r.text
    assert r.json()["row_count"] == 3

    # A failing pull is a 502 and is recorded on the source.
    r = admin.put(
        "/api/v1/sources/broken",
        json={"type": "http", "dataset": "nope", "config": {"url": f"{base}/missing.csv"}},
    )
    assert r.status_code == 200
    r = admin.post("/api/v1/sources/broken/sync")
    assert r.status_code == 502
    src = admin.get("/api/v1/sources/broken").json()
    assert src["last_sync_status"] == "failed"
    # R1: a structured failure, not the driver's sentence. The old column held
    # `redact_driver_text(f"{type(exc).__name__}: {exc}", …)` and round 3 walked
    # around that redactor twice.
    assert src["last_sync_failure"]["subject"] == "source:broken"
    assert src["last_sync_failure"]["detail_ref"].startswith("err-")


def test_source_permissions_and_redaction(clients):
    admin, editor, viewer = clients
    body = {
        "type": "postgres",
        "dataset": "crm",
        "config": {"url": "postgresql://svc:s3cret@db/prod", "table": "public.accounts"},
    }
    assert editor.put("/api/v1/sources/crm_pull", json=body).status_code == 403
    assert admin.put("/api/v1/sources/crm_pull", json=body).status_code == 200

    # R2: a source is ADMIN-authored and EDITOR-read, which is a real privilege
    # crossing. The editor gets the Laurelin-owned facts — which source, which
    # kind, which dataset, whether it last worked — and no `config` at all. The
    # old answer was a denylist over key names in the *connector's* vocabulary,
    # and round 3 read an ODBC keyword string out of a `path` key.
    seen = editor.get("/api/v1/sources/crm_pull").json()
    assert seen["name"] == "crm_pull" and seen["type"] == "postgres"
    assert "config" not in seen
    assert "s3cret" not in editor.get("/api/v1/sources").text

    assert viewer.get("/api/v1/sources").status_code == 403
    # The ADMIN who wrote the config still reads it back, still redacted: "the
    # person who typed it" and "the admin reading this screen" need not be the
    # same admin.
    listed = admin.get("/api/v1/sources").json()
    assert "s3cret" not in listed[0]["config"]["url"]
    assert "db" in listed[0]["config"]["url"]

    # Sync needs edit access on the target dataset: viewers are refused.
    assert viewer.post("/api/v1/sources/crm_pull/sync").status_code == 403

    assert editor.delete("/api/v1/sources/crm_pull").status_code == 403
    assert admin.put(
        "/api/v1/sources/bad", json={"type": "file", "dataset": "x", "config": {}}
    ).status_code == 400


# -- postgres connector (gated: needs a live database) -------------------------

@pytest.mark.skipif(
    not os.environ.get("LAURELIN_TEST_POSTGRES"),
    reason="set LAURELIN_TEST_POSTGRES=<url> to run postgres connector tests",
)
def test_postgres_source_sync(clients):
    import psycopg

    pg_url = os.environ["LAURELIN_TEST_POSTGRES"]
    admin, _, _ = clients
    with psycopg.connect(pg_url) as conn:
        conn.execute("DROP TABLE IF EXISTS laurelin_conn_test")
        conn.execute("CREATE TABLE laurelin_conn_test (id int, name text)")
        conn.execute(
            "INSERT INTO laurelin_conn_test SELECT g, 'row-' || g FROM generate_series(1, 250) g"
        )

    r = admin.put(
        "/api/v1/sources/pg_pull",
        json={
            "type": "postgres",
            "dataset": "pg_rows",
            "config": {"url": pg_url, "table": "laurelin_conn_test", "batch_size": 100},
        },
    )
    assert r.status_code == 200, r.text
    r = admin.post("/api/v1/sources/pg_pull/sync")
    assert r.status_code == 200, r.text
    assert r.json()["row_count"] == 250

    rows = admin.get("/api/v1/datasets/pg_rows/rows?limit=5").json()["rows"]
    assert rows[0]["name"].startswith("row-")


# -- incremental sync ----------------------------------------------------------

def test_append_mode_validation(clients):
    admin, _, _ = clients
    # cursor_column requires append mode and a table (not a raw query).
    bad = {"type": "postgres", "dataset": "x",
           "config": {"url": "postgresql://h/db", "table": "t", "cursor_column": "id"}}
    assert admin.put("/api/v1/sources/s1", json=bad).status_code == 400
    bad["config"]["mode"] = "append"
    bad["config"]["query"] = "SELECT 1"
    del bad["config"]["table"]
    assert admin.put("/api/v1/sources/s1", json=bad).status_code == 400
    # Injection attempt in the cursor identifier is rejected.
    assert admin.put("/api/v1/sources/s1", json={
        "type": "postgres", "dataset": "x",
        "config": {"url": "postgresql://h/db", "table": "t", "mode": "append",
                   "cursor_column": "id > 0; DROP TABLE t --"}}).status_code == 400
    # The good shape is accepted.
    assert admin.put("/api/v1/sources/s1", json={
        "type": "postgres", "dataset": "x",
        "config": {"url": "postgresql://h/db", "table": "t", "mode": "append",
                   "cursor_column": "id"}}).status_code == 200


def test_file_source_append_mode(clients, tmp_path):
    admin, _, _ = clients
    csv = tmp_path / "batch.csv"
    csv.write_text("id,city\n1,valmar\n")
    r = admin.put("/api/v1/sources/city_feed", json={
        "type": "file", "dataset": "cities",
        "config": {"path": str(csv), "mode": "append"}})
    assert r.status_code == 200, r.text
    assert admin.post("/api/v1/sources/city_feed/sync").json()["row_count"] == 1

    csv.write_text("id,city\n2,tirion\n")
    assert admin.post("/api/v1/sources/city_feed/sync").json()["row_count"] == 2
    rows = admin.get("/api/v1/datasets/cities/rows").json()["rows"]
    assert {r["city"] for r in rows} == {"valmar", "tirion"}


@pytest.mark.skipif(
    not os.environ.get("LAURELIN_TEST_POSTGRES"),
    reason="set LAURELIN_TEST_POSTGRES=<url> to run postgres connector tests",
)
def test_postgres_incremental_sync_moves_only_the_delta(clients):
    """A cursor-based sync pulls only new rows and appends them, so a repeated
    sync is O(delta) end to end — not a full refresh."""
    import psycopg

    pg_url = os.environ["LAURELIN_TEST_POSTGRES"]
    admin, _, _ = clients
    with psycopg.connect(pg_url) as conn:
        conn.execute("DROP TABLE IF EXISTS laurelin_incr_test")
        conn.execute("CREATE TABLE laurelin_incr_test (id int, payload text)")
        conn.execute("INSERT INTO laurelin_incr_test "
                     "SELECT g, 'row-' || g FROM generate_series(1, 100) g")

    assert admin.put("/api/v1/sources/incr", json={
        "type": "postgres", "dataset": "incr_rows",
        "config": {"url": pg_url, "table": "laurelin_incr_test",
                   "mode": "append", "cursor_column": "id"}}).status_code == 200

    first = admin.post("/api/v1/sources/incr/sync").json()
    assert first["row_count"] == 100
    assert admin.get("/api/v1/sources/incr").json()["cursor_value"] == "100"

    # Re-syncing with no new upstream rows must be a no-op, not a duplicate.
    same = admin.post("/api/v1/sources/incr/sync").json()
    assert same["row_count"] == 100
    assert same["version"] == first["version"]

    with psycopg.connect(pg_url) as conn:
        conn.execute("INSERT INTO laurelin_incr_test "
                     "SELECT g, 'row-' || g FROM generate_series(101, 130) g")

    after = admin.post("/api/v1/sources/incr/sync").json()
    assert after["row_count"] == 130
    assert after["version"] == first["version"] + 1
    assert admin.get("/api/v1/sources/incr").json()["cursor_value"] == "130"
    # The new version references the original part plus one small delta part.
    assert len(after["files"]) == 2


def test_a_sync_failure_does_not_hand_an_editor_the_admins_endpoint(tmp_path):
    """`POST /sources/{name}/sync` has **no** `dependencies=[...]`. Its only gate
    is `_require_dataset_edit`, and `permissions._evaluate` grants edit from an
    explicit dataset grant regardless of role, or from `role.covers(editor)`
    when a dataset has no grants at all — so a plain editor always reaches it,
    and a viewer holding a `can_edit` grant does too.

    Measured before this fix, as a plain EDITOR with no dataset grants:

        502 {"detail": "The host for source:crm could not be resolved at
             secret-db.internal.corp:55999. (psycopg/OperationalError/ref …)"}

    while the same editor's `GET /sources/crm` correctly carries no `config` at
    all. `HTTPException(detail=...)` never passes through `serialize.dump`, so
    R2 had no jurisdiction over the error path until it was asked to.
    """
    import pyarrow as pa
    from fastapi.testclient import TestClient

    from laurelin.api import create_app
    from laurelin.catalog import DatasetCatalog
    from laurelin.core.config import Workspace
    from laurelin.core.db import MetadataStore

    creds = {"username": "root", "password": "trustno1!"}
    ws = Workspace.init(tmp_path / "ws", name="sync")
    DatasetCatalog(ws, MetadataStore(ws.metadata_path)).write(
        "sales", pa.table({"a": [1]})
    )
    app = create_app(ws)
    admin = TestClient(app)
    assert admin.post("/api/v1/auth/setup", json=creds).status_code == 200
    assert admin.post("/api/v1/auth/login", json=creds).status_code == 200
    for name, role in (("ed", "editor"), ("vic", "viewer")):
        assert admin.post("/api/v1/users", json={
            "username": name, "password": "password123", "role": role,
        }).status_code in (200, 201)

    host = "secret-db.internal.corp"
    assert admin.put("/api/v1/sources/crm", json={
        "type": "postgres", "dataset": "sales",
        "config": {"url": f"postgresql://svc:hunter2@{host}:55999/crm",
                   "table": "public.crm"},
    }).status_code == 200

    editor = TestClient(app)
    assert editor.post("/api/v1/auth/login", json={
        "username": "ed", "password": "password123"}).status_code == 200

    r = editor.post("/api/v1/sources/crm/sync")
    assert r.status_code == 502
    assert host not in r.text, r.text
    # They still learn which source failed, why in one word, and where the
    # operator can read the rest.
    assert "source:crm" in r.text and "err-" in r.text

    # A viewer with an explicit dataset edit grant reaches the same route.
    assert admin.put("/api/v1/datasets/sales/permissions", json={"grants": [
        {"subject_kind": "user", "subject": "vic", "can_view": True,
         "can_edit": True}]}).status_code == 200
    viewer = TestClient(app)
    assert viewer.post("/api/v1/auth/login", json={
        "username": "vic", "password": "password123"}).status_code == 200
    assert host not in viewer.post("/api/v1/sources/crm/sync").text

    # ...and an admin, who wrote the config, still gets the operator sentence.
    assert host in admin.post("/api/v1/sources/crm/sync").text
