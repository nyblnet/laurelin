"""Tests for fine-grained ontology permissions and groups."""

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Grant, Role, SubjectKind, User
from laurelin.core.permissions import PermissionService

ONTOLOGY = """
object_types:
  - api_name: aircraft
    backing_dataset: aircraft_ds
    primary_key: id
    properties:
      id: {type: string}
      status: {type: string}
  - api_name: flight
    backing_dataset: flight_ds
    primary_key: fid
    properties:
      fid: {type: string}
actions:
  - api_name: set_status
    object_type: aircraft
    kind: update
    parameters:
      status: {type: string, required: true}
"""


@pytest.fixture()
def ws(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="perms")
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    cat.write("aircraft_ds", pa.table({"id": ["a1", "a2"], "status": ["ok", "ok"]}))
    cat.write("flight_ds", pa.table({"fid": ["f1"]}))
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    return ws


@pytest.fixture()
def store(ws):
    return MetadataStore(ws.metadata_path)


@pytest.fixture()
def perms(store):
    return PermissionService(store)


VIEWER = User(id="1", username="vic", role=Role.viewer)
EDITOR = User(id="2", username="ed", role=Role.editor)
ADMIN = User(id="3", username="ada", role=Role.admin)


# -- unit: PermissionService -------------------------------------------------

def test_default_open_inherits_global_rbac(perms):
    assert perms.permission(VIEWER, "aircraft") == (True, False)
    assert perms.permission(EDITOR, "aircraft") == (True, True)
    assert perms.permission(ADMIN, "aircraft") == (True, True)


def test_grants_lock_type_to_allowlist(perms, store):
    # Grant view to user 'vic' only; now everyone else (incl. global editor) is out.
    store.set_grants_for_type(
        "aircraft",
        [Grant(subject_kind=SubjectKind.user, subject="vic", can_view=True).model_dump(mode="json")],
    )
    assert perms.permission(VIEWER, "aircraft") == (True, False)
    assert perms.permission(EDITOR, "aircraft") == (False, False)  # locked out
    assert perms.permission(ADMIN, "aircraft") == (True, True)  # admin bypass
    # flight has no grants -> still default open
    assert perms.permission(VIEWER, "flight") == (True, False)


def test_edit_grant_elevates_a_viewer(perms, store):
    store.set_grants_for_type(
        "aircraft",
        [Grant(subject_kind=SubjectKind.user, subject="vic", can_edit=True).model_dump(mode="json")],
    )
    # edit implies view; a viewer-role user can now edit this one type
    assert perms.permission(VIEWER, "aircraft") == (True, True)


def test_role_grant(perms, store):
    store.set_grants_for_type(
        "aircraft",
        [Grant(subject_kind=SubjectKind.role, subject="editor", can_edit=True).model_dump(mode="json")],
    )
    assert perms.permission(EDITOR, "aircraft") == (True, True)
    assert perms.permission(VIEWER, "aircraft") == (False, False)


def test_everyone_grant(perms, store):
    store.set_grants_for_type(
        "aircraft",
        [Grant(subject_kind=SubjectKind.everyone, can_view=True).model_dump(mode="json")],
    )
    assert perms.permission(VIEWER, "aircraft") == (True, False)
    assert perms.permission(EDITOR, "aircraft") == (True, False)  # editors lose edit


def test_group_grant(perms, store):
    store.create_group("ops", "2026-01-01T00:00:00+00:00")
    store.set_group_members("ops", ["vic"])
    store.set_grants_for_type(
        "aircraft",
        [Grant(subject_kind=SubjectKind.group, subject="ops", can_edit=True).model_dump(mode="json")],
    )
    assert perms.permission(VIEWER, "aircraft") == (True, True)  # vic is in ops
    assert perms.permission(EDITOR, "aircraft") == (False, False)  # ed is not


def test_validate_grants(perms, store):
    with pytest.raises(ValueError, match="Unknown role"):
        perms.validate_grants([Grant(subject_kind=SubjectKind.role, subject="wizard", can_view=True)])
    with pytest.raises(ValueError, match="Unknown group"):
        perms.validate_grants([Grant(subject_kind=SubjectKind.group, subject="nope", can_view=True)])
    with pytest.raises(ValueError, match="grants nothing"):
        perms.validate_grants([Grant(subject_kind=SubjectKind.everyone)])


# -- HTTP: enforcement with real auth ----------------------------------------

ADMIN_CREDS = {"username": "root", "password": "trustno1!"}


@pytest.fixture()
def app(ws):
    return create_app(ws)


def _admin(app) -> TestClient:
    c = TestClient(app)
    assert c.post("/api/v1/auth/setup", json=ADMIN_CREDS).status_code == 200
    assert c.post("/api/v1/auth/login", json=ADMIN_CREDS).status_code == 200
    return c


def _make_and_login(app, admin_client, username, role):
    assert admin_client.post(
        "/api/v1/users",
        json={"username": username, "password": "password123", "role": role},
    ).status_code == 200
    c = TestClient(app)
    assert c.post("/api/v1/auth/login", json={"username": username, "password": "password123"}).status_code == 200
    return c


def test_http_default_and_locked_enforcement(app):
    admin = _admin(app)
    viewer = _make_and_login(app, admin, "vic", "viewer")
    editor = _make_and_login(app, admin, "ed", "editor")

    # Default: both roles can view aircraft
    assert viewer.get("/api/v1/ontology/objects/aircraft").status_code == 200
    assert editor.get("/api/v1/ontology/objects/aircraft").status_code == 200
    # Viewer cannot apply the action (default requires edit); editor can
    assert viewer.post("/api/v1/ontology/actions/set_status/apply",
                       json={"pk": "a1", "parameters": {"status": "x"}}).status_code == 403
    assert editor.post("/api/v1/ontology/actions/set_status/apply",
                       json={"pk": "a1", "parameters": {"status": "x"}}).status_code == 200

    # Lock aircraft to user 'vic' view-only.
    r = admin.put("/api/v1/ontology/permissions/aircraft",
                  json={"grants": [{"subject_kind": "user", "subject": "vic", "can_view": True}]})
    assert r.status_code == 200

    # Now the editor is locked out of aircraft entirely (403), viewer still views.
    assert editor.get("/api/v1/ontology/objects/aircraft").status_code == 403
    assert viewer.get("/api/v1/ontology/objects/aircraft").status_code == 200
    # Editor can no longer apply the aircraft action either.
    assert editor.post("/api/v1/ontology/actions/set_status/apply",
                       json={"pk": "a1", "parameters": {"status": "y"}}).status_code == 403
    # object-types listing hides aircraft from the editor, keeps flight
    editor_types = {t["api_name"] for t in editor.get("/api/v1/ontology/object-types").json()}
    assert editor_types == {"flight"}
    viewer_types = {t["api_name"] for t in viewer.get("/api/v1/ontology/object-types").json()}
    assert viewer_types == {"aircraft", "flight"}


def test_http_edit_grant_lets_viewer_apply_action(app):
    admin = _admin(app)
    viewer = _make_and_login(app, admin, "vic", "viewer")
    admin.put("/api/v1/ontology/permissions/aircraft",
              json={"grants": [{"subject_kind": "user", "subject": "vic", "can_edit": True}]})
    # The viewer, elevated on aircraft, can now apply the action.
    r = viewer.post("/api/v1/ontology/actions/set_status/apply",
                    json={"pk": "a1", "parameters": {"status": "grounded"}})
    assert r.status_code == 200


def test_permission_and_group_endpoints_are_admin_only(app):
    admin = _admin(app)
    editor = _make_and_login(app, admin, "ed", "editor")
    assert editor.get("/api/v1/ontology/permissions").status_code == 403
    assert editor.put("/api/v1/ontology/permissions/aircraft", json={"grants": []}).status_code == 403
    assert editor.get("/api/v1/groups").status_code == 403
    assert editor.post("/api/v1/groups", json={"name": "x"}).status_code == 403
    # admin can
    assert admin.get("/api/v1/ontology/permissions").status_code == 200


def test_group_lifecycle_via_api(app):
    admin = _admin(app)
    _make_and_login(app, admin, "vic", "viewer")
    assert admin.post("/api/v1/groups", json={"name": "ops"}).status_code == 200
    assert admin.post("/api/v1/groups", json={"name": "ops"}).status_code == 409  # dup
    assert admin.put("/api/v1/groups/ops/members", json={"members": ["vic"]}).status_code == 200
    assert admin.put("/api/v1/groups/ops/members", json={"members": ["ghost"]}).status_code == 400
    groups = admin.get("/api/v1/groups").json()
    assert groups[0]["name"] == "ops" and groups[0]["members"] == ["vic"]
    assert admin.delete("/api/v1/groups/ops").status_code == 200
    assert admin.get("/api/v1/groups").json() == []


def test_setting_grants_validates_subjects(app):
    admin = _admin(app)
    bad = admin.put("/api/v1/ontology/permissions/aircraft",
                    json={"grants": [{"subject_kind": "user", "subject": "ghost", "can_view": True}]})
    assert bad.status_code == 400
    missing_type = admin.put("/api/v1/ontology/permissions/nonexistent",
                             json={"grants": []})
    assert missing_type.status_code == 404
