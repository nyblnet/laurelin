"""Tests for SCIM 2.0 provisioning (users + groups + deprovisioning)."""

import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.core.config import Workspace

SCIM_TOKEN = "scim-secret-token"
H = {"Authorization": f"Bearer {SCIM_TOKEN}", "Content-Type": "application/scim+json"}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURELIN_SCIM_TOKEN", SCIM_TOKEN)
    ws = Workspace.init(tmp_path / "ws", name="scim")
    app = create_app(ws)
    app.state.auth.create_first_admin("root", "trustno1!")  # out of setup mode
    return app


@pytest.fixture()
def client(app):
    return TestClient(app)


def test_disabled_without_token(tmp_path):
    ws = Workspace.init(tmp_path / "ws2", name="noscim")
    c = TestClient(create_app(ws, no_auth=True))
    assert c.get("/api/v1/scim/v2/Users").status_code == 404


def test_requires_bearer_token(client):
    assert client.get("/api/v1/scim/v2/Users").status_code == 401
    assert client.get("/api/v1/scim/v2/Users", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_service_provider_config(client):
    r = client.get("/api/v1/scim/v2/ServiceProviderConfig", headers=H)
    assert r.status_code == 200
    assert r.json()["patch"]["supported"] is True


def test_provision_and_deprovision_user(app, client):
    # create
    r = client.post("/api/v1/scim/v2/Users", headers=H,
                    json={"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                          "userName": "alice", "active": True})
    assert r.status_code == 201
    scim_id = r.json()["id"]
    assert r.json()["userName"] == "alice"
    assert app.state.auth.get_user("alice") is not None

    # the provisioned user can hold a live session; deprovision must kill it
    from laurelin.core.approvals import local_ticket
    app.state.auth.update_user("alice", role="editor", ticket=local_ticket("test"))
    token, _ = app.state.auth.login(app.state.auth.get_user("alice"))
    assert app.state.auth.resolve_session(token) is not None

    # list with filter
    r = client.get('/api/v1/scim/v2/Users?filter=userName eq "alice"', headers=H)
    assert r.json()["totalResults"] == 1

    # deprovision via PATCH active=false
    r = client.patch(f"/api/v1/scim/v2/Users/{scim_id}", headers=H,
                     json={"Operations": [{"op": "replace", "value": {"active": False}}]})
    assert r.status_code == 200
    assert r.json()["active"] is False
    # session is now dead (disabled user)
    assert app.state.auth.resolve_session(token) is None

    # re-activate via PUT
    r = client.put(f"/api/v1/scim/v2/Users/{scim_id}", headers=H,
                   json={"userName": "alice", "active": True})
    assert r.json()["active"] is True

    # delete
    assert client.delete(f"/api/v1/scim/v2/Users/{scim_id}", headers=H).status_code == 204
    assert app.state.auth.get_user("alice") is None


def test_duplicate_user_is_409(client):
    body = {"userName": "bob", "active": True}
    assert client.post("/api/v1/scim/v2/Users", headers=H, json=body).status_code == 201
    assert client.post("/api/v1/scim/v2/Users", headers=H, json=body).status_code == 409


def test_provision_group_and_membership(app, client):
    # two users first
    a = client.post("/api/v1/scim/v2/Users", headers=H, json={"userName": "u1"}).json()["id"]
    b = client.post("/api/v1/scim/v2/Users", headers=H, json={"userName": "u2"}).json()["id"]

    # create group with one member
    r = client.post("/api/v1/scim/v2/Groups", headers=H,
                    json={"displayName": "Engineers", "members": [{"value": a}]})
    assert r.status_code == 201
    assert {m["value"] for m in r.json()["members"]} == {"u1"}

    # add the second member via PATCH
    r = client.patch("/api/v1/scim/v2/Groups/engineers", headers=H,
                     json={"Operations": [{"op": "add", "path": "members", "value": [{"value": b}]}]})
    assert {m["value"] for m in r.json()["members"]} == {"u1", "u2"}

    # remove one
    r = client.patch("/api/v1/scim/v2/Groups/engineers", headers=H,
                     json={"Operations": [{"op": "remove", "path": "members", "value": [{"value": a}]}]})
    assert {m["value"] for m in r.json()["members"]} == {"u2"}

    # the Laurelin group reflects it
    assert app.state.store.groups_for_user("u2") == {"engineers"}

    # delete group
    assert client.delete("/api/v1/scim/v2/Groups/engineers", headers=H).status_code == 204
