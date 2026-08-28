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


# -- object_store: validation + redaction (ungated) ----------------------------

@pytest.mark.parametrize(
    "config, msg",
    [
        ({}, "s3:// uri"),
        ({"uri": "http://bucket/k.csv"}, "s3:// uri"),
        ({"provider": "gcs", "uri": "s3://b/k.csv"}, "gs:// uri"),
        ({"uri": "s3:///k.csv"}, "names no bucket"),
        ({"uri": "s3://b/k.csv", "access_key_id": "AK"}, "together"),
        ({"uri": "s3://b/k.csv", "secret_access_key": "SK"}, "together"),
        ({"uri": "s3://b/export"}, "format"),
        ({"uri": "s3://b/k.xml"}, "format"),
        ({"uri": "s3://b/k.csv", "endpoint_url": "ftp://minio:9000"}, "http:// or https://"),
        ({"provider": "gcs", "uri": "gs://b/k.csv", "endpoint_url": "http://x"},
         "provider 's3'"),
        ({"provider": "azure", "uri": "az://c/k.csv"}, "not supported yet"),
        ({"provider": "r2", "uri": "s3://b/k.csv"}, "'s3' or 'gcs'"),
        # The object cursor is implicit; a cursor_column here is dead config.
        ({"uri": "s3://b/k.csv", "mode": "append", "cursor_column": "id"},
         "postgres"),
    ],
)
def test_object_store_source_validation_rejects_bad_config(config, msg):
    with pytest.raises(ValueError, match=msg):
        validate_source("object_store", config)


def test_object_store_source_validation_accepts_good_configs():
    validate_source("object_store", {"uri": "s3://landing/exports/*.parquet"})
    validate_source("object_store", {"uri": "s3://landing/x", "format": "jsonl"})
    validate_source("object_store", {  # suffix inference across the new formats
        "uri": "s3://landing/a.ndjson"})
    validate_source("object_store", {"uri": "s3://landing/a.avro", "mode": "append"})
    validate_source("object_store", {
        "provider": "gcs", "uri": "gs://landing/a.json",
        "access_key_id": "AK", "secret_access_key": "SK"})
    validate_source("object_store", {
        "uri": "s3://landing/a.csv", "endpoint_url": "http://minio.internal:9000",
        "region": "us-east-1", "access_key_id": "AK", "secret_access_key": "SK"})


def test_object_store_source_permissions_and_redaction(clients):
    """The invariant with its three enforcement points: a viewer gets 403, an
    editor gets no `config` key at all, and an admin gets the config with the
    key pair masked — while `uri`/`endpoint_url` (the only way to tell two
    bucket registrations apart) and `provider`/`region` stay readable."""
    admin, editor, viewer = clients
    secret = "wJalrXUtnFEMI-K7MDENG-bPxRfiCY"
    body = {
        "type": "object_store",
        "dataset": "landing",
        "config": {
            "provider": "s3",
            "uri": "s3://landing/exports/*.parquet",
            "endpoint_url": "http://minio.internal:9000",
            "region": "eu-central-1",
            "mode": "append",
            "access_key_id": "AKIAFAKEFAKEFAKEFAKE",
            "secret_access_key": secret,
        },
    }
    assert editor.put("/api/v1/sources/bucket_pull", json=body).status_code == 403
    assert admin.put("/api/v1/sources/bucket_pull", json=body).status_code == 200

    config = admin.get("/api/v1/sources/bucket_pull").json()["config"]
    assert config["access_key_id"] == "*****"
    assert config["secret_access_key"] == "*****"
    assert config["uri"] == "s3://landing/exports/*.parquet"
    assert config["endpoint_url"] == "http://minio.internal:9000"
    # Pins the allowlist additions: shape fields must not render as withheld,
    # or the Sources screen cannot tell two bucket registrations apart.
    assert config["provider"] == "s3"
    assert config["region"] == "eu-central-1"
    assert config["mode"] == "append"

    assert secret not in admin.get("/api/v1/sources").text
    assert "AKIAFAKEFAKEFAKEFAKE" not in admin.get("/api/v1/sources").text

    seen = editor.get("/api/v1/sources/bucket_pull").json()
    assert "config" not in seen
    assert secret not in editor.get("/api/v1/sources").text
    assert viewer.get("/api/v1/sources").status_code == 403


# -- JSON/JSONL/Avro on the existing file + http connectors --------------------

def _avro_ocf(rows):
    """A minimal Avro Object Container File for [(id, name), ...] — DuckDB has
    no Avro writer and the venv has no avro package, so the fixture is built
    by hand (verified read back correctly by read_avro)."""
    import json as _json

    def zigzag(n):
        n = (n << 1) ^ (n >> 63)
        out = b""
        while True:
            b7 = n & 0x7F
            n >>= 7
            if n:
                out += bytes([b7 | 0x80])
            else:
                return out + bytes([b7])

    def avro_str(s):
        b = s.encode()
        return zigzag(len(b)) + b

    schema = _json.dumps({"type": "record", "name": "row", "fields": [
        {"name": "id", "type": "long"}, {"name": "name", "type": "string"}]})
    meta = zigzag(2) + avro_str("avro.schema") + avro_str(schema) \
        + avro_str("avro.codec") + avro_str("null") + b"\x00"
    sync = b"0123456789abcdef"
    records = b"".join(zigzag(i) + avro_str(n) for i, n in rows)
    block = zigzag(len(rows)) + zigzag(len(records)) + records + sync
    return b"Obj\x01" + meta + sync + block


ROWS = [(1, "alpha"), (2, "beta"), (3, "gamma")]
JSONL = b'{"id": 1, "name": "alpha"}\n{"id": 2, "name": "beta"}\n{"id": 3, "name": "gamma"}\n'
JSON_ARRAY = (b'[{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"},'
              b' {"id": 3, "name": "gamma"}]')
CSV = b"id,name\n1,alpha\n2,beta\n3,gamma\n"


def test_file_source_reads_jsonl_json_and_avro(clients, tmp_path):
    admin, _, _ = clients
    fixtures = [
        ("data.json", JSON_ARRAY, {}),           # suffix-inferred json
        ("data.jsonl", JSONL, {}),               # suffix-inferred jsonl
        ("data.ndjson", JSONL, {}),              # .ndjson maps to jsonl
        ("data.avro", _avro_ocf(ROWS), {}),      # suffix-inferred avro
        ("data.txt", JSONL, {"format": "jsonl"}),  # explicit format wins
    ]
    for index, (filename, payload, extra) in enumerate(fixtures):
        (tmp_path / filename).write_bytes(payload)
        r = admin.put(f"/api/v1/sources/fmt_{index}", json={
            "type": "file", "dataset": f"fmt_rows_{index}",
            "config": {"path": str(tmp_path / filename), **extra}})
        assert r.status_code == 200, r.text
        r = admin.post(f"/api/v1/sources/fmt_{index}/sync")
        assert r.status_code == 200, (filename, r.text)
        assert r.json()["row_count"] == 3
        rows = admin.get(f"/api/v1/datasets/fmt_rows_{index}/rows").json()["rows"]
        assert {row["name"] for row in rows} == {"alpha", "beta", "gamma"}, filename


def test_http_source_reads_json(clients, http_dir):
    admin, _, _ = clients
    serve_dir, base = http_dir
    (serve_dir / "rows.json").write_bytes(JSON_ARRAY)
    (serve_dir / "rows.jsonl").write_bytes(JSONL)
    for name, url in (("hj", f"{base}/rows.json"), ("hl", f"{base}/rows.jsonl")):
        assert admin.put(f"/api/v1/sources/{name}", json={
            "type": "http", "dataset": f"{name}_rows", "config": {"url": url},
        }).status_code == 200
        r = admin.post(f"/api/v1/sources/{name}/sync")
        assert r.status_code == 200, r.text
        assert r.json()["row_count"] == 3


# -- export posture (ungated) --------------------------------------------------

def test_export_withholds_object_store_credentials_and_import_refuses_sync(tmp_path):
    """The archive is a file that leaves the building: keys, uri and endpoint
    are withheld whole; provider/region (shape) survive; and the re-imported
    source refuses its first sync with the re-supply message instead of
    failing inside DuckDB over `uri=None`."""
    import io
    import json
    import tarfile

    from laurelin.core.models import SourceInfo
    from laurelin.export import (
        ExportOptions,
        ImportOptions,
        NeedsCredentials,
        export_workspace,
        import_workspace,
    )

    ws = Workspace.init(tmp_path / "src", name="src")
    store = MetadataStore(ws.metadata_path)
    store.upsert_source(SourceInfo(
        name="bucket_pull", type="object_store", dataset="landing",
        config={
            "provider": "s3",
            "uri": "s3://verysecret-bucket/exports/*.parquet",
            "endpoint_url": "http://secret-minio.internal.corp:9000",
            "region": "eu-central-1",
            "access_key_id": "AKIAFAKEFAKEFAKEFAKE",
            "secret_access_key": "wJalrXUtnFEMI-K7MDENG-bPxRfiCY",
        },
        created_by="andy",
    ))
    archive = tmp_path / "export.tar"
    export_workspace(ws, store, archive, ExportOptions())
    raw = archive.read_bytes()
    for needle in (b"AKIAFAKEFAKEFAKEFAKE", b"wJalrXUtnFEMI-K7MDENG-bPxRfiCY",
                   b"verysecret-bucket", b"secret-minio.internal.corp"):
        assert needle not in raw, needle

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|*") as tar:
        for member in tar:
            if member.name == "tables/sources.jsonl":
                row = json.loads(tar.extractfile(member).read().decode())
                break
    config = json.loads(row["config_json"])
    assert config["provider"] == "s3" and config["region"] == "eu-central-1"
    assert not config.get("uri") and not config.get("access_key_id")

    target = Workspace.init(tmp_path / "dst", name="dst")
    target_store = MetadataStore(target.metadata_path)
    import_workspace(io.BytesIO(raw), target, target_store, ImportOptions())
    imported = target_store.get_source("bucket_pull")
    assert imported is not None
    from laurelin.connectors import sync_source as run_sync
    with pytest.raises(NeedsCredentials, match="imported without its endpoint"):
        run_sync(DatasetCatalog(target, target_store), target_store, imported)


# -- object_store connector (gated: needs live S3-compatible storage) ----------

S3_TEST_URL = os.environ.get("LAURELIN_TEST_S3", "")
BUCKET = "laurelin-ingest-test"

s3_gate = pytest.mark.skipif(
    not S3_TEST_URL,
    reason="set LAURELIN_TEST_S3=http://<key>:<secret>@host:port (MinIO or any "
           "S3-compatible endpoint) to run object-store connector tests",
)


@pytest.fixture()
def s3():
    """A live S3-compatible endpoint plus a unique per-test prefix, or a SKIP.

    Unreachable is a SKIP with a reason, never a failure — the same posture as
    LAURELIN_TEST_POSTGRES. Fixtures go through pyarrow's S3FileSystem, never
    by shelling into a container.
    """
    import urllib.parse
    import uuid

    from pyarrow import fs as pafs

    if not S3_TEST_URL:
        pytest.skip("LAURELIN_TEST_S3 is not set")
    parts = urllib.parse.urlsplit(S3_TEST_URL)
    endpoint = f"{parts.scheme}://{parts.hostname}:{parts.port}"
    access_key = urllib.parse.unquote(parts.username or "")
    secret_key = urllib.parse.unquote(parts.password or "")
    filesystem = pafs.S3FileSystem(
        access_key=access_key, secret_key=secret_key,
        endpoint_override=endpoint, scheme=parts.scheme,
        allow_bucket_creation=True,
    )
    try:
        filesystem.create_dir(BUCKET, recursive=True)
    except OSError as exc:
        pytest.skip(f"S3 endpoint at LAURELIN_TEST_S3 is unreachable: {type(exc).__name__}")
    return {
        "fs": filesystem,
        "endpoint": endpoint,
        "access_key": access_key,
        "secret_key": secret_key,
        "prefix": f"{BUCKET}/t66-{uuid.uuid4().hex[:10]}",
    }


def _put_object(s3_env, key, data: bytes):
    with s3_env["fs"].open_output_stream(key) as out:
        out.write(data)


def _bucket_config(s3_env, uri, **extra):
    return {
        "uri": uri,
        "endpoint_url": s3_env["endpoint"],
        "access_key_id": s3_env["access_key"],
        "secret_access_key": s3_env["secret_key"],
        "region": "us-east-1",
        **extra,
    }


def _parquet_bytes():
    import io as _io

    import pyarrow.parquet as pq

    sink = _io.BytesIO()
    pq.write_table(
        pa.table({"id": [1, 2, 3], "name": ["alpha", "beta", "gamma"]}), sink
    )
    return sink.getvalue()


@s3_gate
@pytest.mark.parametrize("fmt, payload", [
    ("parquet", _parquet_bytes),
    ("csv", lambda: CSV),
    ("json", lambda: JSON_ARRAY),
    ("jsonl", lambda: JSONL),
    ("avro", lambda: _avro_ocf(ROWS)),
])
def test_object_store_sync_ingests_each_format_from_bucket(clients, s3, fmt, payload):
    admin, _, _ = clients
    key = f"{s3['prefix']}/{fmt}/data.{fmt}"
    _put_object(s3, key, payload())
    r = admin.put(f"/api/v1/sources/os_{fmt}", json={
        "type": "object_store", "dataset": f"os_rows_{fmt}",
        "config": _bucket_config(s3, f"s3://{key}")})
    assert r.status_code == 200, r.text
    r = admin.post(f"/api/v1/sources/os_{fmt}/sync")
    assert r.status_code == 200, r.text
    assert r.json()["row_count"] == 3
    assert r.json()["source"] == "sync:object_store"
    rows = admin.get(f"/api/v1/datasets/os_rows_{fmt}/rows").json()["rows"]
    assert {row["name"] for row in rows} == {"alpha", "beta", "gamma"}


@s3_gate
def test_object_store_sync_uses_glob_selection(clients, s3):
    admin, _, _ = clients
    _put_object(s3, f"{s3['prefix']}/glob/a.csv", b"id,name\n1,alpha\n2,beta\n")
    _put_object(s3, f"{s3['prefix']}/glob/b.csv", b"id,name\n3,gamma\n")
    assert admin.put("/api/v1/sources/os_glob", json={
        "type": "object_store", "dataset": "os_glob_rows",
        "config": _bucket_config(s3, f"s3://{s3['prefix']}/glob/*.csv"),
    }).status_code == 200
    r = admin.post("/api/v1/sources/os_glob/sync")
    assert r.status_code == 200, r.text
    assert r.json()["row_count"] == 3
    rows = admin.get("/api/v1/datasets/os_glob_rows/rows").json()["rows"]
    assert {row["name"] for row in rows} == {"alpha", "beta", "gamma"}


@s3_gate
def test_object_store_sync_with_no_matching_objects_is_an_error_not_an_empty_version(
    clients, s3
):
    admin, _, _ = clients
    assert admin.put("/api/v1/sources/os_none", json={
        "type": "object_store", "dataset": "os_none_rows",
        "config": _bucket_config(s3, f"s3://{s3['prefix']}/void/*.csv"),
    }).status_code == 200
    r = admin.post("/api/v1/sources/os_none/sync")
    assert r.status_code == 400, r.text
    assert "matched no objects" in r.text


@s3_gate
def test_ingested_dataset_is_governed_like_any_managed_dataset(clients, s3):
    """The bytes are copied in and owned: versions with source
    "sync:object_store", a new version per re-sync, and markings + ACLs bind
    exactly as they do for a postgres sync — same shape as the postgres
    governance assertions."""
    admin, _, viewer = clients
    key = f"{s3['prefix']}/gov/data.csv"
    _put_object(s3, key, CSV)
    assert admin.put("/api/v1/sources/os_gov", json={
        "type": "object_store", "dataset": "gov_rows",
        "config": _bucket_config(s3, f"s3://{key}")}).status_code == 200

    first = admin.post("/api/v1/sources/os_gov/sync").json()
    assert first["source"] == "sync:object_store"
    versions = admin.get("/api/v1/datasets/gov_rows").json()["versions"]
    assert [v["source"] for v in versions] == ["sync:object_store"]

    # A re-sync mints a new immutable version, like any managed write.
    second = admin.post("/api/v1/sources/os_gov/sync").json()
    assert second["version"] == first["version"] + 1

    # Before governance is applied, the viewer reads the rows.
    assert viewer.get("/api/v1/datasets/gov_rows/rows").status_code == 200

    # A marking refuses the uncleared viewer.
    assert admin.post("/api/v1/markings", json={
        "name": "pii", "description": "personal"}).status_code == 200
    assert admin.put("/api/v1/datasets/gov_rows/markings", json={
        "markings": ["pii"]}).status_code == 200
    assert viewer.get("/api/v1/datasets/gov_rows/rows").status_code == 403

    # And a restrictive ACL refuses them independently of the marking.
    assert admin.put("/api/v1/datasets/gov_rows/markings", json={
        "markings": []}).status_code == 200
    assert admin.put("/api/v1/datasets/gov_rows/permissions", json={"grants": [
        {"subject_kind": "user", "subject": "ed", "can_view": True,
         "can_edit": True}]}).status_code == 200
    assert viewer.get("/api/v1/datasets/gov_rows/rows").status_code == 403


@s3_gate
def test_object_store_append_syncs_only_new_objects(clients, s3):
    """The implicit object cursor: an append sync pulls only objects newer than
    the stored high-water mark, and a sync that finds nothing new mints no
    version."""
    import time

    admin, _, _ = clients
    prefix = f"{s3['prefix']}/append"
    _put_object(s3, f"{prefix}/a.csv", b"id,name\n1,alpha\n2,beta\n3,gamma\n")
    assert admin.put("/api/v1/sources/os_append", json={
        "type": "object_store", "dataset": "append_rows",
        "config": _bucket_config(
            s3, f"s3://{prefix}/*.csv", mode="append")}).status_code == 200

    first = admin.post("/api/v1/sources/os_append/sync").json()
    assert first["row_count"] == 3
    cursor = admin.get("/api/v1/sources/os_append").json()["cursor_value"]
    assert cursor  # the max last_modified was recorded

    time.sleep(0.05)  # MinIO lists millisecond mtimes; keep b strictly newer
    _put_object(s3, f"{prefix}/b.csv", b"id,name\n4,delta\n5,epsilon\n")
    second = admin.post("/api/v1/sources/os_append/sync").json()
    assert second["row_count"] == 5  # 3 kept + 2 appended, not 3 + 5
    assert second["version"] == first["version"] + 1
    advanced = admin.get("/api/v1/sources/os_append").json()["cursor_value"]
    assert advanced > cursor
    rows = admin.get("/api/v1/datasets/append_rows/rows").json()["rows"]
    assert {row["name"] for row in rows} == {"alpha", "beta", "gamma", "delta", "epsilon"}

    # Nothing new upstream: no new version, cursor unchanged.
    third = admin.post("/api/v1/sources/os_append/sync").json()
    assert third["version"] == second["version"]
    assert admin.get("/api/v1/sources/os_append").json()["cursor_value"] == advanced


@s3_gate
def test_object_store_sync_failure_names_no_endpoint_to_editor(clients):
    """A failing bucket sync must not hand the sync-triggering editor the
    admin's endpoint host, bucket name, or keys — the live_redaction_check
    pattern applied to the new type."""
    admin, editor, _ = clients
    host = "secret-minio.internal.corp"
    bucket = "verysecret-bucket"
    secret = "wJalrXUtnFEMI-K7MDENG-bPxRfiCY"
    assert admin.put("/api/v1/sources/os_broken", json={
        "type": "object_store", "dataset": "broken_rows",
        "config": {
            "uri": f"s3://{bucket}/exports/*.parquet",
            "endpoint_url": f"http://{host}:9000",
            "access_key_id": "AKIAFAKEFAKEFAKEFAKE",
            "secret_access_key": secret,
        }}).status_code == 200

    r = editor.post("/api/v1/sources/os_broken/sync")
    assert r.status_code == 502, r.text
    for needle in (host, bucket, secret, "AKIAFAKE"):
        assert needle not in r.text, needle
    # They still learn which source failed and where the operator reads more.
    assert "source:os_broken" in r.text

    seen = editor.get("/api/v1/sources/os_broken").json()
    assert seen["last_sync_status"] == "failed"
    failure_text = str(seen)
    for needle in (host, bucket, secret, "AKIAFAKE"):
        assert needle not in failure_text, needle


# -- regression: sync connection cannot reach arbitrary http(s) hosts ---------

def test_object_store_sync_connection_blocks_http_and_local_filesystems():
    """The hardened sync connection is network-enabled only for s3://. It must
    disable BOTH LocalFileSystem and HTTPFileSystem, so no reader on it can
    reach an arbitrary http(s) host (incl. 169.254.169.254 cloud metadata) or
    the local disk — defense-in-depth behind validate_source forcing s3://.
    This needs no network: the reads are refused before any request leaves."""
    from laurelin.connectors import connectors as C

    con = C._hardened_sync_connection({
        "uri": "s3://laurelin-ingest-test/x.csv",
        "endpoint_url": "http://127.0.0.1:4705",
        "access_key_id": "AKIAFAKEFAKEFAKEFAKE",
        "secret_access_key": "wJalrXUtnFEMI-fake",
        "region": "us-east-1",
    })
    try:
        for target in (
            "http://127.0.0.1:4706/",             # an internal service
            "http://169.254.169.254/latest/meta", # cloud metadata
            "https://example.com/x.csv",          # any external host
        ):
            with pytest.raises(Exception) as exc:
                con.execute(f"SELECT * FROM read_csv_auto('{target}')").fetchall()
            assert "HTTPFileSystem has been disabled" in str(exc.value), target
        with pytest.raises(Exception) as exc:
            con.execute("SELECT * FROM read_csv_auto('/etc/hostname')").fetchall()
        assert "LocalFileSystem has been disabled" in str(exc.value)
    finally:
        con.close()


@s3_gate
def test_object_store_sync_enforces_the_ingest_size_cap(clients, s3, monkeypatch):
    """An object-store sync must honour the same size ceiling the http puller
    enforces (LAURELIN_MAX_UPLOAD_MB), so an editor-triggered sync of a huge
    object cannot mint an unbounded managed dataset. With the cap at 0 MB, any
    non-empty object trips it and the sync fails cleanly at 400."""
    monkeypatch.setenv("LAURELIN_MAX_UPLOAD_MB", "0")
    key = f"{s3['prefix']}/cap/data.csv"
    _put_object(s3, key, CSV)
    admin, _, _ = clients
    assert admin.put("/api/v1/sources/os_cap", json={
        "type": "object_store", "dataset": "cap_rows",
        "config": _bucket_config(s3, f"s3://{key}")}).status_code == 200
    r = admin.post("/api/v1/sources/os_cap/sync")
    assert r.status_code == 400, r.text
    assert "exceeds" in r.text and "limit" in r.text
