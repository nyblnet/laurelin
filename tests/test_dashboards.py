"""Tests for dashboards: CRUD, validation, permissions, and the property that
panels execute through /query (so ACL/RLS filtering is per-viewer)."""

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import DashboardPanel

CREDS = {"username": "root", "password": "trustno1!"}


@pytest.fixture()
def ws(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="dash")
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    cat.write(
        "sales",
        pa.table({"region": ["us", "us", "eu"], "amount": [10.0, 5.0, 7.0]}),
    )
    return ws


@pytest.fixture()
def clients(ws):
    app = create_app(ws)
    admin = TestClient(app)
    assert admin.post("/api/v1/auth/setup", json=CREDS).status_code == 200
    assert admin.post("/api/v1/auth/login", json=CREDS).status_code == 200
    admin.post("/api/v1/users", json={"username": "vic", "password": "password123", "role": "viewer"})
    viewer = TestClient(app)
    viewer.post("/api/v1/auth/login", json={"username": "vic", "password": "password123"})
    return admin, viewer


PANEL = {
    "id": "p1",
    "title": "Revenue by region",
    "sql": "SELECT region, sum(amount) AS total FROM sales GROUP BY region ORDER BY region",
    "chart": "bar",
    "x": "region",
    "y": ["total"],
    "width": 6,
}


def test_dashboard_crud_roundtrip(clients):
    admin, viewer = clients
    r = admin.put(
        "/api/v1/dashboards/revenue",
        json={"title": "Revenue", "description": "By region", "panels": [PANEL]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["panels"][0]["chart"] == "bar"

    # Viewers can read dashboards…
    listed = viewer.get("/api/v1/dashboards").json()
    assert [d["name"] for d in listed] == ["revenue"]
    dash = viewer.get("/api/v1/dashboards/revenue").json()
    assert dash["title"] == "Revenue"
    assert dash["panels"][0]["sql"].startswith("SELECT region")

    # …and the panel executes through the normal query path for them.
    q = viewer.post("/api/v1/query", json={"sql": dash["panels"][0]["sql"]})
    assert q.status_code == 200
    assert {r["region"]: r["total"] for r in q.json()["rows"]} == {"eu": 7.0, "us": 15.0}

    # Update preserves created_at, bumps updated_at.
    created = dash["created_at"]
    r = admin.put("/api/v1/dashboards/revenue", json={"title": "Revenue v2", "panels": []})
    assert r.status_code == 200
    assert r.json()["created_at"] == created
    assert r.json()["title"] == "Revenue v2"

    assert admin.delete("/api/v1/dashboards/revenue").status_code == 200
    assert viewer.get("/api/v1/dashboards/revenue").status_code == 404


def test_dashboard_permissions_and_validation(clients):
    admin, viewer = clients
    # Viewers cannot write or delete.
    assert viewer.put("/api/v1/dashboards/x", json={"panels": []}).status_code == 403
    assert viewer.delete("/api/v1/dashboards/x").status_code == 403

    # Bad names and bad panel payloads are 400.
    assert admin.put("/api/v1/dashboards/Bad Name", json={"panels": []}).status_code == 400
    r = admin.put(
        "/api/v1/dashboards/x",
        json={"panels": [{"id": "p", "sql": "SELECT 1", "chart": "pie3d"}]},
    )
    assert r.status_code == 400
    r = admin.put(
        "/api/v1/dashboards/x",
        json={"panels": [{"id": "p", "sql": "SELECT 1", "width": 13}]},
    )
    assert r.status_code == 400
    assert admin.delete("/api/v1/dashboards/nope").status_code == 404


# -- object-backed panels -----------------------------------------------------
#
# A panel charting the *backing dataset* with SQL misses the ontology's edit
# overlay: it answers from rows an action has already changed, and nothing in
# the chart says it disagrees with the object list beside it. An object panel
# goes through /aggregate instead, which sees the overlay.

def test_a_panel_needs_exactly_one_source():
    with pytest.raises(ValidationError, match="either sql or object_type"):
        DashboardPanel(id="p")
    with pytest.raises(ValidationError, match="not both"):
        DashboardPanel(id="p", sql="SELECT 1", object_type="order",
                       metrics=[{"op": "count"}])


def test_an_object_panel_needs_a_metric():
    """Grouping with nothing to measure produces a chart of nothing."""
    with pytest.raises(ValidationError, match="at least one metric"):
        DashboardPanel(id="p", object_type="order")


def test_panel_kinds_are_distinguishable():
    sql = DashboardPanel(id="a", sql="SELECT 1")
    obj = DashboardPanel(id="b", object_type="order", metrics=[{"op": "count"}])
    assert not sql.is_object_panel
    assert obj.is_object_panel


def test_an_object_panel_round_trips_through_the_api(clients):
    admin, _ = clients
    body = {
        "name": "ops",
        "title": "Ops",
        "panels": [{
            "id": "p1",
            "title": "Aircraft by status",
            "object_type": "aircraft",
            "group_by": ["status"],
            "metrics": [{"op": "count", "alias": "n"}],
            "chart": "bar",
        }],
    }
    assert admin.put("/api/v1/dashboards/ops", json=body).status_code == 200
    got = admin.get("/api/v1/dashboards/ops").json()
    panel = got["panels"][0]
    assert panel["object_type"] == "aircraft"
    assert panel["group_by"] == ["status"]
    assert panel["metrics"] == [{"op": "count", "alias": "n"}]
    assert panel["sql"] == ""


def test_a_panel_with_both_sources_is_rejected_by_the_api(clients):
    admin, _ = clients
    r = admin.put("/api/v1/dashboards/bad", json={
        "name": "bad",
        "panels": [{"id": "p", "sql": "SELECT 1", "object_type": "aircraft",
                    "metrics": [{"op": "count"}]}],
    })
    assert r.status_code == 400
    assert "not both" in r.json()["detail"]
