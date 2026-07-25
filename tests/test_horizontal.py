"""Multi-node readiness: per-workspace metadata in PostgreSQL.

The blocker to running more than one replica was per-workspace metadata living
in a SQLite file on a shared volume — SQLite's WAL cannot be shared across
hosts. Each workspace now gets its own Postgres *schema* in the shared control
database, so N replicas can serve the same workspaces safely.

These tests need a live PostgreSQL:
    LAURELIN_TEST_POSTGRES=postgresql://user:pw@host:port/db pytest tests/test_horizontal.py
"""

import os
import uuid

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_server_app
from laurelin.api.context import open_workspace_store, workspace_schema
from laurelin.catalog import DatasetCatalog
from laurelin.core.backend import PostgresBackend
from laurelin.core.config import Workspace
from laurelin.core.control import ControlStore
from laurelin.core.db import MetadataStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("LAURELIN_TEST_POSTGRES"),
    reason="set LAURELIN_TEST_POSTGRES=<url> to run multi-node tests",
)

PG = os.environ.get("LAURELIN_TEST_POSTGRES", "")
CREDS = {"username": "root", "password": "trustno1!"}


@pytest.fixture()
def control_url():
    """A throwaway schema for the control plane, dropped afterwards."""
    name = f"ctl_{uuid.uuid4().hex[:10]}"
    store = ControlStore(PG, schema=name)
    yield PG, name, store
    with store._conn() as c:
        c.execute(f"DROP SCHEMA IF EXISTS {PostgresBackend.quote_ident(name)} CASCADE")


def drop_ws_schema(slug: str):
    store = MetadataStore(PG)
    with store._conn() as c:
        c.execute(
            f"DROP SCHEMA IF EXISTS "
            f"{PostgresBackend.quote_ident(workspace_schema(slug))} CASCADE"
        )


# -- schema scoping -----------------------------------------------------------

def test_schema_scoped_stores_are_isolated(tmp_path):
    a, b = f"wsa{uuid.uuid4().hex[:6]}", f"wsb{uuid.uuid4().hex[:6]}"
    try:
        sa = MetadataStore(PG, schema=a)
        sb = MetadataStore(PG, schema=b)
        sa.upsert_dataset("shared_name", "from A")
        sb.upsert_dataset("shared_name", "from B")

        # Same table name, same database, different contents.
        assert sa.get_dataset("shared_name").description == "from A"
        assert sb.get_dataset("shared_name").description == "from B"
        assert [d.name for d in sa.list_datasets()] == ["shared_name"]

        # A dataset in one is invisible to the other.
        sa.upsert_dataset("only_in_a", "")
        assert sb.get_dataset("only_in_a") is None
    finally:
        for name in (a, b):
            with MetadataStore(PG)._conn() as c:
                c.execute(f"DROP SCHEMA IF EXISTS {PostgresBackend.quote_ident(name)} CASCADE")


def test_reopening_a_schema_sees_existing_data():
    name = f"wsr{uuid.uuid4().hex[:6]}"
    try:
        MetadataStore(PG, schema=name).upsert_dataset("persisted", "hello")
        # A *different process/replica* opening the same schema sees the data.
        assert MetadataStore(PG, schema=name).get_dataset("persisted").description == "hello"
    finally:
        with MetadataStore(PG)._conn() as c:
            c.execute(f"DROP SCHEMA IF EXISTS {PostgresBackend.quote_ident(name)} CASCADE")


def test_sqlite_rejects_a_schema(tmp_path):
    with pytest.raises(ValueError, match="postgresql:// URL"):
        MetadataStore(tmp_path / "x.db", schema="nope")


# -- the multi-replica property ------------------------------------------------

def test_two_replicas_share_workspace_state(tmp_path, control_url):
    """Two independently constructed apps — the moral equivalent of two pods —
    must see each other's writes without sharing a filesystem database."""
    url, _, control = control_url
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    slug = f"shared{uuid.uuid4().hex[:6]}"
    try:
        app_a = create_server_app(root_a, control_url=url)
        app_b = create_server_app(root_b, control_url=url)
        # Point both at the same control schema as the fixture.
        app_a.state.control = control
        app_b.state.control = control
        from laurelin.core.auth import AuthService
        app_a.state.control_auth = AuthService(control)
        app_b.state.control_auth = AuthService(control)

        ca, cb = TestClient(app_a), TestClient(app_b)
        assert ca.post("/api/v1/auth/setup", json=CREDS).status_code == 200
        # Identity is global: the account created on replica A logs in on B.
        assert cb.post("/api/v1/auth/login", json=CREDS).status_code == 200
        assert ca.post("/api/v1/auth/login", json=CREDS).status_code == 200

        assert ca.post("/api/v1/workspaces", json={"slug": slug, "name": "Shared"}).status_code == 200
        for client in (ca, cb):
            client.headers["X-Laurelin-Workspace"] = slug

        # Replica A writes a dataset; replica B must see it.
        assert ca.post("/api/v1/datasets", json={"name": "orders"}).status_code == 200
        assert "orders" in {d["name"] for d in cb.get("/api/v1/datasets").json()}

        # And a policy set on B applies on A — shared metadata, not a cache.
        assert cb.post("/api/v1/markings", json={"name": "pii"}).status_code == 200
        assert "pii" in {m["name"] for m in ca.get("/api/v1/markings").json()}
    finally:
        drop_ws_schema(slug)


def test_workspace_store_resolution(tmp_path, control_url):
    url, _, control = control_url
    slug = f"res{uuid.uuid4().hex[:6]}"
    try:
        store = open_workspace_store(tmp_path, control, slug)
        assert store.schema == workspace_schema(slug)
        assert store.dialect == "postgres"
    finally:
        drop_ws_schema(slug)


def test_sqlite_control_plane_keeps_file_stores(tmp_path):
    """Embedded mode is unchanged: a SQLite control plane still gives each
    workspace its own file."""
    control = ControlStore(tmp_path / "control.db")
    Workspace.init(tmp_path / "solo", name="solo")
    store = open_workspace_store(tmp_path, control, "solo")
    assert store.dialect == "sqlite"
    assert store.schema is None


def test_deleting_a_workspace_drops_its_schema(tmp_path, control_url):
    url, _, control = control_url
    slug = f"del{uuid.uuid4().hex[:6]}"
    control.create_workspace(slug, name="Doomed")
    store = open_workspace_store(tmp_path, control, slug)
    store.upsert_dataset("leftover", "")

    control.delete_workspace(slug)

    # The schema is gone, so reusing the slug cannot resurrect stale metadata.
    probe = MetadataStore(PG)
    with probe._conn() as c:
        row = c.execute(
            "SELECT 1 AS x FROM information_schema.schemata WHERE schema_name = ?",
            (workspace_schema(slug),),
        ).fetchone()
    assert row is None
    assert open_workspace_store(tmp_path, control, slug).get_dataset("leftover") is None
    drop_ws_schema(slug)


def test_data_written_via_one_store_is_readable_via_another(tmp_path, control_url):
    """Parquet on shared storage + metadata in Postgres = a replica that never
    touched the write still reads it."""
    url, _, control = control_url
    slug = f"dat{uuid.uuid4().hex[:6]}"
    try:
        ws = Workspace.init(tmp_path / slug, name=slug)
        writer = open_workspace_store(tmp_path, control, slug)
        DatasetCatalog(ws, writer).write("orders", pa.table({"id": [1, 2, 3]}))

        reader = open_workspace_store(tmp_path, control, slug)
        assert DatasetCatalog(ws, reader).read("orders").num_rows == 3
    finally:
        drop_ws_schema(slug)
