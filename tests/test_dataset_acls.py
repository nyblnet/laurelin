"""Tests for per-dataset access control and its composition with ontology grants."""

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.approvals import ChangeTicket as _ChangeTicket
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Grant, Role, SubjectKind, User
from laurelin.core.permissions import PermissionService

_TICKET = _ChangeTicket(kind="local", actor="test")

ONTOLOGY = """
object_types:
  - api_name: secret_obj
    backing_dataset: secret_ds
    primary_key: id
    properties:
      id: {type: string}
  - api_name: public_obj
    backing_dataset: public_ds
    primary_key: id
    properties:
      id: {type: string}
actions:
  - api_name: touch_secret
    object_type: secret_obj
    kind: update
    parameters:
      id: {type: string, required: true}
"""


@pytest.fixture()
def ws(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="acls")
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    cat.write("secret_ds", pa.table({"id": ["s1", "s2"]}))
    cat.write("public_ds", pa.table({"id": ["p1"]}))
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


# -- unit: dataset + composed permission logic -------------------------------

def test_default_dataset_access(perms):
    assert perms.dataset_permission(VIEWER, "secret_ds") == (True, False)
    assert perms.dataset_permission(EDITOR, "secret_ds") == (True, True)
    assert perms.dataset_permission(ADMIN, "secret_ds") == (True, True)


def test_dataset_grant_locks_to_allowlist(perms, store):
    store.set_grants_for_dataset(
        "secret_ds",
        [Grant(subject_kind=SubjectKind.user, subject="vic", can_view=True).model_dump(mode="json")],
    ticket=_TICKET)
    assert perms.dataset_permission(VIEWER, "secret_ds") == (True, False)
    assert perms.dataset_permission(EDITOR, "secret_ds") == (False, False)  # locked out
    assert perms.dataset_permission(ADMIN, "secret_ds") == (True, True)  # bypass
    assert perms.viewable_datasets(EDITOR, ["secret_ds", "public_ds"]) == {"public_ds"}


def test_object_type_view_requires_backing_dataset_view(perms, store):
    # Lock the dataset to admin-only; the object type must become invisible even
    # though it has no ontology grant of its own.
    store.set_grants_for_dataset(
        "secret_ds",
        [Grant(subject_kind=SubjectKind.role, subject="admin", can_view=True).model_dump(mode="json")],
    ticket=_TICKET)
    assert perms.object_type_permission(VIEWER, "secret_obj", "secret_ds") == (False, False)
    assert perms.object_type_permission(EDITOR, "secret_obj", "secret_ds") == (False, False)
    assert perms.object_type_permission(ADMIN, "secret_obj", "secret_ds") == (True, True)
    # public_obj unaffected
    assert perms.object_type_permission(VIEWER, "public_obj", "public_ds") == (True, False)


def test_object_type_edit_needs_dataset_view_and_ontology_edit(perms, store):
    # Give the viewer ontology edit but NOT dataset view -> no effective access.
    store.set_grants_for_type(
        "secret_obj",
        [Grant(subject_kind=SubjectKind.user, subject="vic", can_edit=True).model_dump(mode="json")],
    ticket=_TICKET)
    store.set_grants_for_dataset(
        "secret_ds",
        [Grant(subject_kind=SubjectKind.role, subject="admin", can_view=True).model_dump(mode="json")],
    ticket=_TICKET)
    assert perms.object_type_permission(VIEWER, "secret_obj", "secret_ds") == (False, False)
    # Now also grant the viewer dataset view -> ontology edit takes effect.
    store.set_grants_for_dataset(
        "secret_ds",
        [
            Grant(subject_kind=SubjectKind.role, subject="admin", can_view=True).model_dump(mode="json"),
            Grant(subject_kind=SubjectKind.user, subject="vic", can_view=True).model_dump(mode="json"),
        ],
    ticket=_TICKET)
    assert perms.object_type_permission(VIEWER, "secret_obj", "secret_ds") == (True, True)


# -- HTTP enforcement --------------------------------------------------------

ADMIN_CREDS = {"username": "root", "password": "trustno1!"}


@pytest.fixture()
def app(ws):
    return create_app(ws)


@pytest.fixture()
def admin(app):
    c = TestClient(app)
    assert c.post("/api/v1/auth/setup", json=ADMIN_CREDS).status_code == 200
    assert c.post("/api/v1/auth/login", json=ADMIN_CREDS).status_code == 200
    return c


@pytest.fixture()
def viewer(app, admin):
    assert admin.post(
        "/api/v1/users", json={"username": "vic", "password": "password123", "role": "viewer"}
    ).status_code == 200
    c = TestClient(app)
    assert c.post("/api/v1/auth/login", json={"username": "vic", "password": "password123"}).status_code == 200
    return c


def _lock_to_admin(admin, dataset):
    r = admin.put(
        f"/api/v1/datasets/{dataset}/permissions",
        json={"grants": [{"subject_kind": "user", "subject": "root", "can_view": True, "can_edit": True}]},
    )
    assert r.status_code == 200


def test_locked_dataset_hidden_from_viewer_everywhere(admin, viewer):
    _lock_to_admin(admin, "secret_ds")
    names = {d["name"] for d in viewer.get("/api/v1/datasets").json()}
    assert names == {"public_ds"}
    assert viewer.get("/api/v1/datasets/secret_ds").status_code == 403
    assert viewer.get("/api/v1/datasets/secret_ds/rows").status_code == 403
    assert viewer.get("/api/v1/datasets/secret_ds/schema").status_code == 403


def test_query_respects_dataset_acl(admin, viewer):
    _lock_to_admin(admin, "secret_ds")
    # Blocked dataset is an unknown table -> 400; allowed dataset works.
    assert viewer.post("/api/v1/query", json={"sql": "SELECT * FROM secret_ds"}).status_code == 400
    ok = viewer.post("/api/v1/query", json={"sql": "SELECT * FROM public_ds"})
    assert ok.status_code == 200
    # admin can still query the locked dataset
    assert admin.post("/api/v1/query", json={"sql": "SELECT count(*) FROM secret_ds"}).status_code == 200


def test_locking_dataset_hides_its_object_type(admin, viewer):
    _lock_to_admin(admin, "secret_ds")
    types = {t["api_name"] for t in viewer.get("/api/v1/ontology/object-types").json()}
    assert types == {"public_obj"}  # secret_obj hidden via its backing dataset
    assert viewer.get("/api/v1/ontology/objects/secret_obj").status_code == 403
    assert viewer.post(
        "/api/v1/ontology/actions/touch_secret/apply",
        json={"pk": "s1", "parameters": {"id": "s1"}},
    ).status_code == 403


def test_dataset_edit_grant_lets_viewer_upload(admin, viewer):
    admin.put(
        "/api/v1/datasets/public_ds/permissions",
        json={"grants": [{"subject_kind": "user", "subject": "vic", "can_edit": True}]},
    )
    r = viewer.post(
        "/api/v1/datasets/public_ds/upload",
        files={"file": ("a.csv", b"id\nx1\n", "text/csv")},
    )
    assert r.status_code == 200
    # but a viewer cannot upload to a default (editor-required) dataset
    assert viewer.post(
        "/api/v1/datasets/secret_ds/upload",
        files={"file": ("a.csv", b"id\ny1\n", "text/csv")},
    ).status_code == 403


def test_dataset_permission_endpoints_are_admin_only(admin, viewer):
    assert viewer.get("/api/v1/dataset-permissions").status_code == 403
    assert viewer.put(
        "/api/v1/datasets/secret_ds/permissions", json={"grants": []}
    ).status_code == 403
    assert admin.get("/api/v1/dataset-permissions").status_code == 200
    # unknown dataset -> 404; bad subject -> 400
    assert admin.put("/api/v1/datasets/nope/permissions", json={"grants": []}).status_code == 404
    assert admin.put(
        "/api/v1/datasets/secret_ds/permissions",
        json={"grants": [{"subject_kind": "user", "subject": "ghost", "can_view": True}]},
    ).status_code == 400
