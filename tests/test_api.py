"""End-to-end tests for the FastAPI server over a tmp_path workspace."""

from __future__ import annotations

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore

PIPELINE = '''\
import pyarrow.compute as pc

from laurelin.transforms import Input, Output, sql_transform, transform


@transform(Output("planes", description="Active planes only"), raw=Input("raw_planes"))
def clean_planes(raw):
    return raw.filter(pc.equal(raw["status"], "active"))


@sql_transform(
    Output("flight_counts", description="Flights per tail number"),
    inputs={"flights": Input("raw_flights")},
    query="SELECT tail_number, COUNT(*) AS n FROM flights GROUP BY tail_number",
)
def count_flights():
    ...


@sql_transform(
    Output("plane_models"),
    inputs={"p": Input("planes")},
    query="SELECT model, COUNT(*) AS n FROM p GROUP BY model",
)
def agg_plane_models():
    ...
'''

ONTOLOGY = """\
object_types:
  - api_name: plane
    display_name: Plane
    backing_dataset: raw_planes
    primary_key: tail_number
    title_property: model
    properties:
      tail_number: {type: string}
      model: {type: string}
      status: {type: string}
  - api_name: flight
    backing_dataset: raw_flights
    primary_key: flight_id
    properties:
      flight_id: {type: string}
      tail_number: {type: string}
      origin: {type: string}

link_types:
  - api_name: plane_flights
    from: plane
    to: flight
    cardinality: one_to_many
    from_property: tail_number
    to_property: tail_number

actions:
  - api_name: update_plane_status
    object_type: plane
    kind: update
    parameters:
      status: {type: string, required: true}
  - api_name: add_plane
    object_type: plane
    kind: create
    parameters:
      tail_number: {type: string, required: true}
      model: {type: string}
      status: {type: string}
"""


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    ws = Workspace.init(tmp_path / "ws", name="testws", description="A test workspace")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write(
        "raw_planes",
        pa.table(
            {
                "tail_number": ["N100", "N200", "N300"],
                "model": ["A320", "B737", "A320"],
                "status": ["active", "active", "retired"],
            }
        ),
    )
    catalog.write(
        "raw_flights",
        pa.table(
            {
                "flight_id": ["F1", "F2", "F3", "F4"],
                "tail_number": ["N100", "N100", "N200", "N999"],
                "origin": ["SFO", "LAX", "JFK", "SEA"],
            }
        ),
    )
    (ws.pipelines_dir / "pipe.py").write_text(PIPELINE)
    (ws.ontology_dir / "onto.yml").write_text(ONTOLOGY)
    return ws


@pytest.fixture
def client(workspace) -> TestClient:
    # These tests exercise the data plane; auth has its own suite (test_auth.py).
    return TestClient(create_app(workspace, no_auth=True))


# ---------------------------------------------------------------------------
# Health & workspace
# ---------------------------------------------------------------------------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_workspace(client, workspace):
    r = client.get("/api/v1/workspace")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "testws"
    assert body["description"] == "A test workspace"
    assert body["root"] == str(workspace.root)


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------

def test_dataset_list_and_detail(client):
    r = client.get("/api/v1/datasets")
    assert r.status_code == 200
    names = [d["name"] for d in r.json()]
    assert names == ["raw_flights", "raw_planes"]

    r = client.get("/api/v1/datasets/raw_planes")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "raw_planes"
    assert body["latest_version"] == 1
    assert len(body["versions"]) == 1
    v = body["versions"][0]
    assert v["version"] == 1
    assert v["row_count"] == 3
    assert {c["name"] for c in v["schema"]} == {"tail_number", "model", "status"}


def test_dataset_schema(client):
    r = client.get("/api/v1/datasets/raw_planes/schema")
    assert r.status_code == 200
    cols = {c["name"]: c["type"] for c in r.json()}
    assert cols == {"tail_number": "string", "model": "string", "status": "string"}


def test_dataset_rows(client):
    r = client.get("/api/v1/datasets/raw_flights/rows", params={"limit": 2, "offset": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["row_count"] == 4
    assert [row["flight_id"] for row in body["rows"]] == ["F2", "F3"]


def test_dataset_404s(client):
    for path in (
        "/api/v1/datasets/nope",
        "/api/v1/datasets/nope/schema",
        "/api/v1/datasets/nope/rows",
        "/api/v1/datasets/raw_planes/rows?version=99",
    ):
        r = client.get(path)
        assert r.status_code == 404, path
        assert "detail" in r.json()


def test_create_dataset(client):
    r = client.post("/api/v1/datasets", json={"name": "new_ds", "description": "hi"})
    assert r.status_code == 200
    assert r.json()["name"] == "new_ds"
    assert "new_ds" in [d["name"] for d in client.get("/api/v1/datasets").json()]

    r = client.post("/api/v1/datasets", json={"name": "Bad-Name"})
    assert r.status_code == 400
    assert "detail" in r.json()


def test_upload_csv(client):
    csv = b"tail_number,model,status\nN400,E175,active\nN500,E175,stored\n"
    r = client.post(
        "/api/v1/datasets/uploaded_planes/upload",
        files={"file": ("planes.csv", csv, "text/csv")},
        headers={"X-Laurelin-User": "amdt"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["dataset"] == "uploaded_planes"
    assert body["version"] == 1
    assert body["row_count"] == 2
    assert body["source"] == "upload"

    rows = client.get("/api/v1/datasets/uploaded_planes/rows").json()["rows"]
    assert rows[0]["tail_number"] == "N400"

    audit = client.get("/api/v1/audit").json()
    uploaded = [e for e in audit if e["action"] == "dataset_uploaded"]
    assert uploaded and uploaded[0]["actor"] == "amdt"
    assert uploaded[0]["details"]["dataset"] == "uploaded_planes"


def test_upload_bad_extension(client):
    r = client.post(
        "/api/v1/datasets/bad_upload/upload",
        files={"file": ("data.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Transforms, builds, lineage
# ---------------------------------------------------------------------------

def test_list_transforms(client):
    r = client.get("/api/v1/transforms")
    assert r.status_code == 200
    by_name = {t["name"]: t for t in r.json()}
    assert set(by_name) == {"clean_planes", "count_flights", "agg_plane_models"}
    assert by_name["clean_planes"] == {
        "name": "clean_planes",
        "output": "planes",
        "inputs": ["raw_planes"],
        "kind": "python",
    }
    assert by_name["agg_plane_models"]["kind"] == "sql"


def test_build_all_and_listing(client):
    r = client.post("/api/v1/builds", json={"wait": True})
    assert r.status_code == 200
    build = r.json()
    assert build["status"] == "succeeded"
    assert {t["transform_name"] for t in build["tasks"]} == {
        "clean_planes",
        "count_flights",
        "agg_plane_models",
    }
    assert all(t["status"] == "succeeded" for t in build["tasks"])

    # Outputs materialized.
    rows = client.get("/api/v1/datasets/planes/rows").json()
    assert rows["row_count"] == 2  # retired plane filtered out

    # Listing and detail.
    builds = client.get("/api/v1/builds").json()
    assert [b["id"] for b in builds] == [build["id"]]
    detail = client.get(f"/api/v1/builds/{build['id']}").json()
    assert detail["id"] == build["id"]
    assert len(detail["tasks"]) == 3

    assert client.get("/api/v1/builds/doesnotexist").status_code == 404


def test_build_missing_body_and_targets(client):
    r = client.post("/api/v1/builds", json={"wait": True})
    assert r.status_code == 200
    assert r.json()["status"] == "succeeded"

    r = client.post("/api/v1/builds", json={"targets": ["planes"], "wait": True})
    assert r.status_code == 200
    assert [t["transform_name"] for t in r.json()["tasks"]] == ["clean_planes"]

    r = client.post("/api/v1/builds", json={"targets": ["unknown_target"]})
    assert r.status_code == 400


def test_async_build_default(client):
    """POST /builds without wait returns a pending build immediately; a worker
    finishes it and GET /builds/{id} converges to succeeded."""
    import time

    r = client.post("/api/v1/builds", json={})
    assert r.status_code == 200
    build = r.json()
    assert build["status"] in ("pending", "running")

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        detail = client.get(f"/api/v1/builds/{build['id']}").json()
        if detail["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.05)
    assert detail["status"] == "succeeded"
    assert {t["transform_name"] for t in detail["tasks"]} == {
        "clean_planes",
        "count_flights",
        "agg_plane_models",
    }


def test_lineage(client):
    client.post("/api/v1/builds", json={"wait": True})
    r = client.get("/api/v1/lineage")
    assert r.status_code == 200
    graph = r.json()
    nodes = {(n["id"], n["type"]) for n in graph["nodes"]}
    assert ("raw_planes", "dataset") in nodes
    assert ("planes", "dataset") in nodes
    assert ("clean_planes", "transform") in nodes
    assert ("agg_plane_models", "transform") in nodes
    edges = {(e["from"], e["to"]) for e in graph["edges"]}
    assert ("raw_planes", "clean_planes") in edges
    assert ("clean_planes", "planes") in edges
    assert ("planes", "agg_plane_models") in edges
    assert ("agg_plane_models", "planes") not in edges
    assert ("agg_plane_models", "plane_models") in edges
    assert len(edges) == len(graph["edges"])  # deduped


# ---------------------------------------------------------------------------
# Ontology
# ---------------------------------------------------------------------------

def test_ontology_object_types(client):
    r = client.get("/api/v1/ontology/object-types")
    assert r.status_code == 200
    assert [t["api_name"] for t in r.json()] == ["plane", "flight"]

    r = client.get("/api/v1/ontology/object-types/plane")
    assert r.status_code == 200
    body = r.json()
    assert body["api_name"] == "plane"
    assert body["primary_key"] == "tail_number"
    assert [lt["api_name"] for lt in body["links"]] == ["plane_flights"]
    assert body["links"][0]["from"] == "plane"
    assert body["links"][0]["to"] == "flight"
    assert {a["api_name"] for a in body["actions"]} == {
        "update_plane_status",
        "add_plane",
    }

    assert client.get("/api/v1/ontology/object-types/nope").status_code == 404


def test_ontology_query_search_filter(client):
    r = client.get("/api/v1/ontology/objects/plane")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    pks = {o["__pk"] for o in body["objects"]}
    assert pks == {"N100", "N200", "N300"}
    titles = {o["__title"] for o in body["objects"]}
    assert titles == {"A320", "B737"}

    r = client.get("/api/v1/ontology/objects/plane", params={"search": "b737"})
    assert [o["__pk"] for o in r.json()["objects"]] == ["N200"]

    r = client.get("/api/v1/ontology/objects/plane", params={"filter.status": "active"})
    assert {o["__pk"] for o in r.json()["objects"]} == {"N100", "N200"}

    r = client.get(
        "/api/v1/ontology/objects/plane",
        params={"filter.status": "active", "filter.model": "A320"},
    )
    assert [o["__pk"] for o in r.json()["objects"]] == ["N100"]

    r = client.get("/api/v1/ontology/objects/plane", params={"limit": 1, "offset": 1})
    body = r.json()
    assert body["total"] == 3
    assert len(body["objects"]) == 1

    assert client.get("/api/v1/ontology/objects/nope").status_code == 404


def test_ontology_get_and_links(client):
    r = client.get("/api/v1/ontology/objects/plane/N100")
    assert r.status_code == 200
    assert r.json()["model"] == "A320"

    assert client.get("/api/v1/ontology/objects/plane/N999").status_code == 404

    r = client.get("/api/v1/ontology/objects/plane/N100/links/plane_flights")
    assert r.status_code == 200
    assert {o["flight_id"] for o in r.json()["objects"]} == {"F1", "F2"}

    # Reverse direction: flight -> plane.
    r = client.get("/api/v1/ontology/objects/flight/F3/links/plane_flights")
    assert [o["__pk"] for o in r.json()["objects"]] == ["N200"]

    assert (
        client.get("/api/v1/ontology/objects/plane/N100/links/nope").status_code == 404
    )


def test_actions_list_and_apply(client):
    r = client.get("/api/v1/ontology/actions")
    assert r.status_code == 200
    assert {a["api_name"] for a in r.json()} == {"update_plane_status", "add_plane"}

    # Update: effect visible.
    r = client.post(
        "/api/v1/ontology/actions/update_plane_status/apply",
        json={"pk": "N300", "parameters": {"status": "active"}},
        headers={"X-Laurelin-User": "amdt"},
    )
    assert r.status_code == 200
    edit = r.json()
    assert edit["kind"] == "update"
    assert edit["actor"] == "amdt"
    assert client.get("/api/v1/ontology/objects/plane/N300").json()["status"] == "active"

    # Create: new object appears.
    r = client.post(
        "/api/v1/ontology/actions/add_plane/apply",
        json={"parameters": {"tail_number": "N400", "model": "E175", "status": "active"}},
    )
    assert r.status_code == 200
    assert client.get("/api/v1/ontology/objects/plane").json()["total"] == 4
    assert client.get("/api/v1/ontology/objects/plane/N400").json()["model"] == "E175"

    # Audit trail records the action with the actor.
    audit = client.get("/api/v1/audit").json()
    applied = [e for e in audit if e["action"] == "action_applied"]
    assert any(e["actor"] == "amdt" for e in applied)


def test_action_validation_errors(client):
    # Missing required parameter.
    r = client.post(
        "/api/v1/ontology/actions/update_plane_status/apply",
        json={"pk": "N100", "parameters": {}},
    )
    assert r.status_code == 400
    assert "status" in r.json()["detail"]

    # Unknown parameter.
    r = client.post(
        "/api/v1/ontology/actions/update_plane_status/apply",
        json={"pk": "N100", "parameters": {"status": "active", "bogus": 1}},
    )
    assert r.status_code == 400

    # Nonexistent pk on update.
    r = client.post(
        "/api/v1/ontology/actions/update_plane_status/apply",
        json={"pk": "N999", "parameters": {"status": "active"}},
    )
    assert r.status_code == 400

    # Unknown action -> 404 (an unknown resource, like an unknown dataset/type).
    r = client.post(
        "/api/v1/ontology/actions/nope/apply", json={"parameters": {}}
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def test_audit_listing_and_limit(client):
    client.post("/api/v1/builds", json={"wait": True})
    r = client.get("/api/v1/audit")
    assert r.status_code == 200
    actions = [e["action"] for e in r.json()]
    assert "build_started" in actions
    assert "build_finished" in actions
    assert "build_requested" in actions

    r = client.get("/api/v1/audit", params={"limit": 1})
    assert len(r.json()) == 1


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_auth_on_by_default_requires_setup(workspace):
    auth_client = TestClient(create_app(workspace))

    r = auth_client.get("/api/v1/datasets")
    assert r.status_code == 401
    assert r.json() == {"detail": "setup required"}

    # Health and auth status stay open.
    assert auth_client.get("/health").status_code == 200
    status = auth_client.get("/api/v1/auth/status")
    assert status.status_code == 200
    body = status.json()
    assert body["auth_required"] is True
    assert body["setup_required"] is True
    assert body["user"] is None


def test_auth_setup_login_and_bearer(workspace):
    auth_client = TestClient(create_app(workspace))
    r = auth_client.post(
        "/api/v1/auth/setup", json={"username": "root", "password": "trustno1!"}
    )
    assert r.status_code == 200

    r = auth_client.get("/api/v1/datasets", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401

    r = auth_client.post(
        "/api/v1/auth/login", json={"username": "root", "password": "trustno1!"}
    )
    assert r.status_code == 200
    assert auth_client.get("/api/v1/datasets").status_code == 200

    token = auth_client.post("/api/v1/tokens", json={"name": "e2e"}).json()["token"]
    fresh = TestClient(create_app(workspace))
    r = fresh.get("/api/v1/datasets", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_no_auth_env_var_opens_everything(workspace, monkeypatch):
    monkeypatch.setenv("LAURELIN_NO_AUTH", "1")
    open_client = TestClient(create_app(workspace))
    assert open_client.get("/api/v1/datasets").status_code == 200
    assert open_client.get("/api/v1/auth/status").json()["auth_required"] is False


# ---------------------------------------------------------------------------
# Live reload of pipelines and ontology
# ---------------------------------------------------------------------------

def test_pipeline_and_ontology_reload(client, workspace):
    assert len(client.get("/api/v1/transforms").json()) == 3
    (workspace.pipelines_dir / "extra.py").write_text(
        "from laurelin.transforms import Input, Output, sql_transform\n"
        "@sql_transform(Output('tails'), inputs={'p': Input('raw_planes')},\n"
        "               query='SELECT tail_number FROM p')\n"
        "def tails(): ...\n"
    )
    assert len(client.get("/api/v1/transforms").json()) == 4

    assert len(client.get("/api/v1/ontology/object-types").json()) == 2
    (workspace.ontology_dir / "extra.yml").write_text(
        "object_types:\n"
        "  - api_name: airport\n"
        "    backing_dataset: raw_airports\n"
        "    primary_key: code\n"
        "    properties:\n"
        "      code: {type: string}\n"
    )
    assert len(client.get("/api/v1/ontology/object-types").json()) == 3
