"""Tests for multi-workspace mode: control plane, membership roles, isolation."""

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_server_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore

ROOT_CREDS = {"username": "root", "password": "trustno1!"}


@pytest.fixture()
def app(tmp_path):
    return create_server_app(tmp_path / "srv")


@pytest.fixture()
def superadmin(app):
    c = TestClient(app)
    assert c.post("/api/v1/auth/setup", json=ROOT_CREDS).status_code == 200
    assert c.post("/api/v1/auth/login", json=ROOT_CREDS).status_code == 200
    return c


def _login(app, username, password):
    c = TestClient(app)
    assert c.post("/api/v1/auth/login", json={"username": username, "password": password}).status_code == 200
    return c


def _member(app, superadmin, username, slug, role, password="password123"):
    superadmin.post("/api/v1/users", json={"username": username, "password": password, "role": "viewer"})
    superadmin.put(f"/api/v1/workspaces/{slug}/members", json={"username": username, "role": role})


def test_setup_creates_superadmin(app):
    c = TestClient(app)
    status = c.get("/api/v1/auth/status").json()
    assert status["auth_required"] is True
    assert status["setup_required"] is True
    assert status["multi"] is True
    assert status["user"] is None
    c.post("/api/v1/auth/setup", json=ROOT_CREDS)
    c.post("/api/v1/auth/login", json=ROOT_CREDS)
    me = c.get("/api/v1/auth/me").json()
    assert me["superadmin"] is True
    assert me["workspaces"] == []  # superadmin sees all workspaces (none yet)


def test_workspace_crud(superadmin):
    assert superadmin.post("/api/v1/workspaces", json={"slug": "alpha", "name": "Alpha"}).status_code == 200
    assert superadmin.post("/api/v1/workspaces", json={"slug": "alpha"}).status_code == 409  # dup
    assert superadmin.post("/api/v1/workspaces", json={"slug": "Bad Slug"}).status_code == 400
    slugs = {w["slug"] for w in superadmin.get("/api/v1/workspaces").json()}
    assert slugs == {"alpha"}
    # rename
    superadmin.patch("/api/v1/workspaces/alpha", json={"name": "Alpha Renamed"})
    assert superadmin.get("/api/v1/workspaces").json()[0]["name"] == "Alpha Renamed"
    # delete (unregister)
    assert superadmin.delete("/api/v1/workspaces/alpha").status_code == 200
    assert superadmin.get("/api/v1/workspaces").json() == []


def test_workspace_scoped_routes_need_a_selected_workspace(superadmin):
    superadmin.post("/api/v1/workspaces", json={"slug": "alpha"})
    assert superadmin.get("/api/v1/datasets").status_code == 400  # no workspace header
    assert superadmin.get("/api/v1/datasets", headers={"X-Laurelin-Workspace": "alpha"}).status_code == 200
    assert superadmin.get("/api/v1/datasets", headers={"X-Laurelin-Workspace": "ghost"}).status_code == 404


def test_membership_roles_and_isolation(app, superadmin):
    superadmin.post("/api/v1/workspaces", json={"slug": "alpha"})
    superadmin.post("/api/v1/workspaces", json={"slug": "beta"})
    _member(app, superadmin, "ed", "alpha", "editor")
    ed = _login(app, "ed", "password123")

    # ed sees only alpha, as editor
    me = ed.get("/api/v1/auth/me").json()
    assert [(w["slug"], w["role"]) for w in me["workspaces"]] == [("alpha", "editor")]

    A = {"X-Laurelin-Workspace": "alpha"}
    B = {"X-Laurelin-Workspace": "beta"}
    # editor in alpha: can read and create datasets
    assert ed.get("/api/v1/datasets", headers=A).status_code == 200
    assert ed.post("/api/v1/datasets", json={"name": "d1"}, headers=A).status_code == 200
    # not a member of beta: 403 everywhere in beta
    assert ed.get("/api/v1/datasets", headers=B).status_code == 403
    assert ed.post("/api/v1/datasets", json={"name": "d2"}, headers=B).status_code == 403


def test_data_isolation_between_workspaces(app, superadmin):
    superadmin.post("/api/v1/workspaces", json={"slug": "alpha"})
    superadmin.post("/api/v1/workspaces", json={"slug": "beta"})
    superadmin.post("/api/v1/datasets", json={"name": "only_in_alpha"}, headers={"X-Laurelin-Workspace": "alpha"})
    alpha = {d["name"] for d in superadmin.get("/api/v1/datasets", headers={"X-Laurelin-Workspace": "alpha"}).json()}
    beta = {d["name"] for d in superadmin.get("/api/v1/datasets", headers={"X-Laurelin-Workspace": "beta"}).json()}
    assert "only_in_alpha" in alpha
    assert "only_in_alpha" not in beta


def test_only_superadmin_manages_workspaces_and_users(app, superadmin):
    superadmin.post("/api/v1/workspaces", json={"slug": "alpha"})
    _member(app, superadmin, "adm", "alpha", "admin")  # workspace admin, not superadmin
    adm = _login(app, "adm", "password123")
    A = {"X-Laurelin-Workspace": "alpha"}
    # workspace-admin can manage that workspace's groups/permissions...
    assert adm.get("/api/v1/groups", headers=A).status_code == 200
    assert adm.get("/api/v1/ontology/permissions", headers=A).status_code == 200
    # ...but NOT server-level workspaces or global users
    assert adm.get("/api/v1/workspaces").status_code == 403
    assert adm.post("/api/v1/workspaces", json={"slug": "x"}).status_code == 403
    assert adm.get("/api/v1/users").status_code == 403


def test_removing_membership_revokes_access(app, superadmin):
    superadmin.post("/api/v1/workspaces", json={"slug": "alpha"})
    _member(app, superadmin, "vic", "alpha", "viewer")
    vic = _login(app, "vic", "password123")
    A = {"X-Laurelin-Workspace": "alpha"}
    assert vic.get("/api/v1/datasets", headers=A).status_code == 200
    superadmin.request("DELETE", "/api/v1/workspaces/alpha/members/vic")
    assert vic.get("/api/v1/datasets", headers=A).status_code == 403


def test_per_workspace_acls_are_independent(app, superadmin):
    # Groups/grants live per workspace; the same group name is separate per ws.
    superadmin.post("/api/v1/workspaces", json={"slug": "alpha"})
    superadmin.post("/api/v1/workspaces", json={"slug": "beta"})
    A = {"X-Laurelin-Workspace": "alpha"}
    B = {"X-Laurelin-Workspace": "beta"}
    assert superadmin.post("/api/v1/groups", json={"name": "ops"}, headers=A).status_code == 200
    assert {g["name"] for g in superadmin.get("/api/v1/groups", headers=A).json()} == {"ops"}
    assert superadmin.get("/api/v1/groups", headers=B).json() == []  # beta has no groups


def test_anonymous_cannot_enumerate_workspace_slugs(app, superadmin):
    # An existing vs absent slug must look identical to an unauthenticated caller
    # (both 401) — no slug-enumeration oracle.
    superadmin.post("/api/v1/workspaces", json={"slug": "alpha"})
    anon = TestClient(app)
    exists = anon.get("/api/v1/datasets", headers={"X-Laurelin-Workspace": "alpha"})
    absent = anon.get("/api/v1/datasets", headers={"X-Laurelin-Workspace": "ghost"})
    assert exists.status_code == 401
    assert absent.status_code == 401


def test_workspace_management_absent_in_single_mode(tmp_path):
    from laurelin.api import create_app

    ws = Workspace.init(tmp_path / "ws", name="single")
    c = TestClient(create_app(ws, no_auth=True))
    # the /workspaces routes exist but report 404 (single-workspace server)
    assert c.get("/api/v1/workspaces").status_code == 404
