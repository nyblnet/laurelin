"""Integration test: the multi-workspace control plane on a real PostgreSQL.

Runs only when LAURELIN_TEST_POSTGRES is set to a postgresql:// URL (e.g. a
docker/podman Postgres). Everything else runs on SQLite.
"""

import os

import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_server_app
from laurelin.core.db import MetadataStore
from laurelin.core.models import EditKind, ObjectEdit

PG_URL = os.environ.get("LAURELIN_TEST_POSTGRES")


def _reset_pg():
    import psycopg

    with psycopg.connect(PG_URL) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.commit()

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="set LAURELIN_TEST_POSTGRES to a postgresql:// URL to run"
)


@pytest.fixture()
def app(tmp_path):
    _reset_pg()
    return create_server_app(tmp_path / "root", control_url=PG_URL)


@pytest.fixture()
def store():
    _reset_pg()
    return MetadataStore(PG_URL)


def test_store_paths_that_differ_by_dialect_on_postgres(store):
    from laurelin.core.models import DatasetVersionInfo

    # upsert_dataset — was broken on PG (ambiguous 'description' in ON CONFLICT)
    store.upsert_dataset("ds1", "first")
    store.upsert_dataset("ds1", "")  # empty must not clobber
    assert store.get_dataset("ds1").description == "first"
    store.upsert_dataset("ds1", "second")
    assert store.get_dataset("ds1").description == "second"

    # builds ORDER BY (was ORDER BY rowid — no rowid on PG)
    b1 = store.create_build(["a"])
    b2 = store.create_build(["b"])
    builds = store.list_builds()
    assert [b.id for b in builds] == [b2.id, b1.id]  # newest first

    # object_edits insertion order (was ORDER BY rowid)
    for i, kind in enumerate([EditKind.create, EditKind.update, EditKind.delete]):
        store.add_object_edit(
            ObjectEdit(id=f"e{i}", object_type="widget", pk_value="1",
                       kind=kind, payload={"id": "1"}, created_at="2026-01-01T00:00:00+00:00")
        )
    kinds = [e.kind for e in store.list_object_edits("widget")]
    assert kinds == [EditKind.create, EditKind.update, EditKind.delete]

    # audit ordering + IDENTITY id
    store.log_audit("first_action")
    store.log_audit("second_action")
    assert store.list_audit(1)[0].action == "second_action"


def test_group_case_insensitivity_on_postgres(store):
    # groups were stored verbatim but read lowercased -> broken on PG
    store.create_group("Eng", "2026-01-01T00:00:00+00:00")
    assert store.group_exists("eng") is True
    assert store.group_exists("ENG") is True
    store.create_user(_user("alice"), "hash")
    store.set_group_members("ENG", ["Alice"])
    assert store.groups_for_user("ALICE") == {"eng"}


def _user(username):
    from laurelin.core.models import Role, User

    return User(id=username, username=username, role=Role.viewer)


ROOT = {"username": "root", "password": "trustno1!"}


def test_multiworkspace_flow_on_postgres(app):
    admin = TestClient(app)
    # setup superadmin + login
    assert admin.post("/api/v1/auth/setup", json=ROOT).status_code == 200
    assert admin.post("/api/v1/auth/login", json=ROOT).status_code == 200
    assert admin.get("/api/v1/auth/me").json()["superadmin"] is True

    # workspace CRUD (registry in Postgres)
    assert admin.post("/api/v1/workspaces", json={"slug": "alpha", "name": "Alpha"}).status_code == 200
    assert admin.post("/api/v1/workspaces", json={"slug": "beta"}).status_code == 200
    assert admin.post("/api/v1/workspaces", json={"slug": "alpha"}).status_code == 409

    # global user + per-workspace membership
    assert admin.post(
        "/api/v1/users", json={"username": "ed", "password": "password123", "role": "viewer"}
    ).status_code == 200
    assert admin.put("/api/v1/workspaces/alpha/members", json={"username": "ed", "role": "editor"}).status_code == 200

    ed = TestClient(app)
    assert ed.post("/api/v1/auth/login", json={"username": "ed", "password": "password123"}).status_code == 200
    assert [(w["slug"], w["role"]) for w in ed.get("/api/v1/auth/me").json()["workspaces"]] == [("alpha", "editor")]

    # isolation: ed is editor in alpha, denied beta
    A, B = {"X-Laurelin-Workspace": "alpha"}, {"X-Laurelin-Workspace": "beta"}
    assert ed.get("/api/v1/datasets", headers=A).status_code == 200
    assert ed.post("/api/v1/datasets", json={"name": "d1"}, headers=A).status_code == 200
    assert ed.get("/api/v1/datasets", headers=B).status_code == 403

    # server-level ops remain superadmin-only
    assert ed.get("/api/v1/workspaces").status_code == 403
    assert ed.get("/api/v1/users").status_code == 403

    # case-insensitive identity (Postgres has no COLLATE NOCASE)
    assert admin.post("/api/v1/auth/login", json={"username": "ED", "password": "password123"}).status_code == 200
