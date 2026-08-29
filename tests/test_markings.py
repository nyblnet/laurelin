"""Tests for classification markings: mandatory access control + lineage propagation."""

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.approvals import ChangeTicket as _ChangeTicket
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Role, User
from laurelin.core.permissions import PermissionService
from laurelin.transforms import Builder, collect_transforms

_TICKET = _ChangeTicket(kind="local", actor="test")

PIPELINE = (
    "from laurelin.transforms import transform, Input, Output\n"
    "@transform(output=Output('clean'), r=Input('raw'))\n"
    "def clean(r):\n    return r\n"
    "@transform(output=Output('report'), c=Input('clean'))\n"
    "def report(c):\n    return c\n"
)


@pytest.fixture()
def ws(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="mk")
    DatasetCatalog(ws, MetadataStore(ws.metadata_path)).write("raw", pa.table({"id": ["1"]}))
    (ws.pipelines_dir / "p.py").write_text(PIPELINE)
    return ws


@pytest.fixture()
def store(ws):
    return MetadataStore(ws.metadata_path)


@pytest.fixture()
def perms(store):
    return PermissionService(store)


def _build(ws):
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    Builder(ws, catalog, store, collect_transforms(ws.pipelines_dir)).build()


VIEWER = User(id="1", username="vic", role=Role.viewer)
EDITOR = User(id="2", username="ed", role=Role.editor)
ADMIN = User(id="3", username="ada", role=Role.admin)


# -- unit: propagation + MAC -------------------------------------------------

def test_markings_propagate_through_lineage_on_build(ws, store):
    store.create_marking("pii")
    store.set_explicit_markings("raw", ["pii"], ticket=_TICKET)
    store.recompute_all_markings()
    assert store.get_effective_markings("clean") == []  # not built yet
    _build(ws)
    # PII flows raw -> clean -> report through two transforms
    assert store.get_effective_markings("clean") == ["pii"]
    assert store.get_effective_markings("report") == ["pii"]
    # explicit stays only on raw
    assert store.get_explicit_markings("clean") == []
    assert store.get_explicit_markings("raw") == ["pii"]


def test_mandatory_access_requires_all_clearances(ws, store, perms):
    store.create_marking("pii")
    store.create_marking("secret")
    store.set_explicit_markings("raw", ["pii", "secret"], ticket=_TICKET)
    store.recompute_all_markings()
    # holding only one of two markings is not enough (deny-by-default MAC)
    store.set_clearances("vic", ["pii"], ticket=_TICKET)
    assert perms.dataset_permission(VIEWER, "raw") == (False, False)
    store.set_clearances("vic", ["pii", "secret"], ticket=_TICKET)
    assert perms.dataset_permission(VIEWER, "raw")[0] is True


def test_markings_override_a_permissive_grant(ws, store, perms):
    from laurelin.core.models import Grant, SubjectKind

    store.create_marking("pii")
    store.set_explicit_markings("raw", ["pii"], ticket=_TICKET)
    store.recompute_all_markings()
    # even an explicit edit grant can't beat an unheld marking
    store.set_grants_for_dataset(
        "raw", [Grant(subject_kind=SubjectKind.user, subject="vic", can_edit=True).model_dump(mode="json")]
    , ticket=_TICKET)
    assert perms.dataset_permission(VIEWER, "raw") == (False, False)
    store.set_clearances("vic", ["pii"], ticket=_TICKET)
    assert perms.dataset_permission(VIEWER, "raw") == (True, True)


def test_admins_bypass_markings(ws, store, perms):
    store.create_marking("pii")
    store.set_explicit_markings("raw", ["pii"], ticket=_TICKET)
    store.recompute_all_markings()
    assert perms.dataset_permission(ADMIN, "raw") == (True, True)
    # editors do NOT bypass
    assert perms.dataset_permission(EDITOR, "raw") == (False, False)


def test_delete_marking_recomputes(ws, store, perms):
    store.create_marking("pii")
    store.set_explicit_markings("raw", ["pii"], ticket=_TICKET)
    _build(ws)
    assert store.get_effective_markings("clean") == ["pii"]
    store.delete_marking("pii", ticket=_TICKET)
    store.recompute_all_markings()
    assert store.get_effective_markings("raw") == []
    assert store.get_effective_markings("clean") == []
    assert perms.dataset_permission(VIEWER, "clean")[0] is True  # no marking -> visible


# -- HTTP -------------------------------------------------------------------

CREDS = {"username": "root", "password": "trustno1!"}


@pytest.fixture()
def clients(ws):
    app = create_app(ws)
    admin = TestClient(app)
    admin.post("/api/v1/auth/setup", json=CREDS)
    admin.post("/api/v1/auth/login", json=CREDS)
    admin.post("/api/v1/users", json={"username": "vic", "password": "password123", "role": "viewer"})
    viewer = TestClient(app)
    viewer.post("/api/v1/auth/login", json={"username": "vic", "password": "password123"})
    return app, admin, viewer


def test_http_marking_flow_and_enforcement(ws, clients):
    app, admin, viewer = clients
    # build so lineage exists, then mark the source and propagate
    assert admin.post("/api/v1/builds", json={"wait": True}).status_code == 200
    assert admin.post("/api/v1/markings", json={"name": "PII", "description": "personal"}).status_code == 200
    assert admin.put("/api/v1/datasets/raw/markings", json={"markings": ["pii"]}).status_code == 200

    # effective markings propagated to derived datasets
    dm = {d["dataset"]: d for d in admin.get("/api/v1/dataset-markings").json()}
    assert dm["raw"]["explicit"] == ["pii"]
    assert dm["clean"]["effective"] == ["pii"]
    assert dm["report"]["effective"] == ["pii"]

    # uncleared viewer cannot see the marked datasets anywhere
    names = {d["name"] for d in viewer.get("/api/v1/datasets").json()}
    assert "raw" not in names and "clean" not in names
    assert viewer.get("/api/v1/datasets/clean/rows").status_code == 403
    assert viewer.post("/api/v1/query", json={"sql": "SELECT * FROM clean"}).status_code == 400  # unknown table

    # grant clearance -> now visible
    assert admin.put("/api/v1/users/vic/clearances", json={"markings": ["pii"]}).status_code == 200
    names = {d["name"] for d in viewer.get("/api/v1/datasets").json()}
    assert {"raw", "clean", "report"} <= names
    assert viewer.get("/api/v1/datasets/clean/rows").status_code == 200


def test_marking_endpoints_admin_only(clients):
    app, admin, viewer = clients
    assert viewer.post("/api/v1/markings", json={"name": "x"}).status_code == 403
    assert viewer.get("/api/v1/dataset-markings").status_code == 403
    assert viewer.put("/api/v1/users/vic/clearances", json={"markings": []}).status_code == 403
    assert viewer.get("/api/v1/markings").status_code == 200  # listing definitions is viewer
    # unknown marking on a dataset -> 400
    admin.post("/api/v1/builds", json={"wait": True})
    assert admin.put("/api/v1/datasets/raw/markings", json={"markings": ["ghost"]}).status_code == 400
