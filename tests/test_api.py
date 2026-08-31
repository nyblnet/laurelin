"""End-to-end tests for the FastAPI server over a tmp_path workspace."""

from __future__ import annotations

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Role

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
        # #75: `inputs` is row-filtered to what this reader may view, and the
        # boolean says whether anything was dropped. Never a count.
        "hidden_inputs": False,
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


# -- R2: who may read the trail ------------------------------------------------

@pytest.fixture
def three_roles(workspace):
    """admin / editor / viewer against one real, authenticated app."""
    app = create_app(workspace)
    creds = {"username": "root", "password": "trustno1!"}
    admin = TestClient(app)
    assert admin.post("/api/v1/auth/setup", json=creds).status_code == 200
    assert admin.post("/api/v1/auth/login", json=creds).status_code == 200
    out = [admin]
    for name, role in (("ed", "editor"), ("vic", "viewer")):
        assert admin.post("/api/v1/users", json={
            "username": name, "password": "password123", "role": role,
        }).status_code in (200, 201)
        c = TestClient(app)
        assert c.post("/api/v1/auth/login", json={
            "username": name, "password": "password123",
        }).status_code == 200
        out.append(c)
    return tuple(out)


def test_audit_is_editor_gated_and_a_viewer_sees_only_their_own_actions(three_roles):
    """`log_audit` takes an open dict, and rounds 2 and 3 were both "a writer
    put driver text in the bag". A viewer reading other people's bags is one
    careless call site from round 5.

    The stated need — "a user should see what was done" — is served by
    /audit/mine, which discloses nothing new by construction: you cannot learn a
    secret from a row you wrote."""
    admin, editor, viewer = three_roles
    assert admin.put(
        "/api/v1/dashboards/d", json={"title": "d", "panels": []}
    ).status_code == 200

    assert viewer.get("/api/v1/audit").status_code == 403
    assert editor.get("/api/v1/audit").status_code == 200
    assert admin.get("/api/v1/audit").status_code == 200

    mine = viewer.get("/api/v1/audit/mine")
    assert mine.status_code == 200
    assert all(row["actor"] == "vic" for row in mine.json())
    assert not any(row["action"] == "dashboard_updated" for row in mine.json())


def test_an_undeclared_audit_writer_is_admin_only_by_default(workspace, three_roles):
    """New call site ⇒ fails closed. The writer has to argue the level down."""
    admin, editor, _viewer = three_roles
    store = MetadataStore(workspace.metadata_path)
    store.log_audit("careless_writer", {"reason": "driver said SEKRET"}, actor="root")

    assert "careless_writer" not in [e["action"] for e in editor.get("/api/v1/audit").json()]
    assert "careless_writer" in [e["action"] for e in admin.get("/api/v1/audit").json()]
    assert "SEKRET" not in editor.get("/api/v1/audit").text


def test_an_applied_action_does_not_record_the_objects_property_values(client):
    """The sharpest leak in the trail, and one no credential matcher could have
    caught: `parameters` was the object's own property VALUES, and /audit was
    viewer-gated. A viewer holding a 403 on the object type read them."""
    r = client.post(
        "/api/v1/ontology/actions/update_plane_status/apply",
        json={"pk": "N100", "parameters": {"status": "GOVERNED_VALUE"}},
    )
    assert r.status_code == 200, r.text
    audit = client.get("/api/v1/audit").json()
    applied = [e for e in audit if e["action"] == "action_applied"]
    assert applied, "the action should still be recorded"
    assert "parameters" not in applied[0]["details"]
    assert applied[0]["details"]["edit_id"], "the full record is still reachable"
    assert "GOVERNED_VALUE" not in client.get("/api/v1/audit").text


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


# ---------------------------------------------------------------------------
# One unimportable pipeline file must not take out the pages that read the DAG
# ---------------------------------------------------------------------------
#
# Found by driving the UI: dropping a file that raises on import into
# `pipelines/` made a VIEWER's Pipeline page render "Error 500: Internal Server
# Error". `get_registry` calls `collect_transforms`, which raises
# `PipelineError`, which has no exception handler — so every route depending on
# the registry died, `GET /transforms` (viewer) included.

def _broken_pipeline_clients(tmp_path):
    from fastapi.testclient import TestClient

    from laurelin.api import create_app
    from laurelin.core.config import Workspace

    ws = Workspace.init(tmp_path / "ws", name="brokenpipes")
    ws.pipelines_dir.mkdir(exist_ok=True)
    (ws.pipelines_dir / "unimportable.py").write_text(
        # Stands in for a library that quotes its own configuration back at you
        # while a pipeline imports it.
        'raise RuntimeError("import blew up: postgresql://u:PIPE-SENTINEL@h/db")\n'
    )
    app = create_app(ws)
    creds = {"username": "root", "password": "trustno1!"}
    admin = TestClient(app)
    admin.post("/api/v1/auth/setup", json=creds)
    admin.post("/api/v1/auth/login", json=creds)
    admin.post("/api/v1/users",
               json={"username": "vic", "password": "password123", "role": "viewer"})
    viewer = TestClient(app)
    viewer.post("/api/v1/auth/login", json={"username": "vic", "password": "password123"})
    return admin, viewer


def test_a_pipeline_file_that_will_not_import_names_itself_instead_of_500ing(tmp_path):
    _admin, viewer = _broken_pipeline_clients(tmp_path)
    r = viewer.get("/api/v1/transforms")
    # 409 — the request is fine, the state of the workspace is not.
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "unimportable" in detail, "the operator has to know which file"
    assert "Transforms" in detail, "and where to go and fix it"


def test_the_pipeline_collection_error_does_not_repeat_the_librarys_own_words(tmp_path):
    """R1 at a catch site nobody had converted.

    `PipelineError`'s message interpolates `f"{type(exc).__name__}: {exc}"` from
    whatever the file raised, plus the server's absolute path — and this route
    is readable by a viewer. Only `exc.pipeline`, a Laurelin identifier, may
    reach the response.
    """
    _admin, viewer = _broken_pipeline_clients(tmp_path)
    body = viewer.get("/api/v1/transforms").text
    assert "PIPE-SENTINEL" not in body
    assert "RuntimeError" not in body
    assert "/pipelines/" not in body, "nor the server's filesystem layout"


def test_a_build_still_refuses_when_a_pipeline_file_will_not_import(tmp_path):
    """Degrading must not become "quietly build nothing". A workspace whose
    pipelines do not import cannot be built, and says so."""
    admin, _viewer = _broken_pipeline_clients(tmp_path)
    r = admin.post("/api/v1/builds", json={})
    assert r.status_code == 409
    assert "unimportable" in r.json()["detail"]


# ---------------------------------------------------------------------------
# R1's backstop: the two global exception handlers
# ---------------------------------------------------------------------------

def test_an_uncaught_library_exception_does_not_return_that_librarys_words(
    workspace, monkeypatch
):
    """`@app.exception_handler(ValueError)` turned **any** library's exception
    into a response body carrying that library's raw text, because
    `pyarrow.lib.ArrowInvalid` is a `ValueError` and `ArrowKeyError` is a
    `KeyError`.

    Measured: with `LAURELIN_ICEBERG_WAREHOUSE=s3://AKIA…:SECRET@bucket/wh`, an
    EDITOR uploading a CSV to an Iceberg dataset got back

        400 {"detail": "Not a valid bucket name: 'AKIA…:SECRET@bucket'"}

    — pyiceberg handed the netloc, userinfo and all, to pyarrow, which raised.
    Catch sites are still where a failure should be classified; this is the net
    under them, and it fails closed on anything Laurelin did not raise itself.
    """
    from fastapi.routing import APIRoute

    def _boom():
        import pyarrow as pa

        # Raised inside pyarrow, from a call Laurelin made — the exact shape of
        # the iceberg warehouse case, with no pyiceberg needed to reproduce it.
        pa.compute.cast(pa.array(["S3KRET_LIBRARY_TEXT"]), pa.int64())

    app = create_app(workspace, no_auth=True)
    # In front of the StaticFiles mount at "/", which create_app adds last and
    # which would otherwise answer this path with a 404 from the SPA.
    app.router.routes.insert(0, APIRoute("/api/v1/__probe_thirdparty", _boom, methods=["GET"]))
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/api/v1/__probe_thirdparty")
    assert r.status_code == 400
    assert "S3KRET_LIBRARY_TEXT" not in r.text, r.text
    assert "err-" in r.text, "the operator still needs a handle into the log"


def test_a_first_party_message_still_reaches_the_caller(client):
    """The backstop must not flatten Laurelin's own 400s into a code. A route
    that raises `ValueError("...")` is how most of this API says "you typed it
    wrong", and that sentence is ours."""
    r = client.post("/api/v1/query", json={"sql": ""})
    assert r.status_code in (400, 422)
    assert r.text.strip()


# ---------------------------------------------------------------------------
# The audit trail: one mechanism, not two
# ---------------------------------------------------------------------------

def test_a_viewer_reading_their_own_audit_rows_does_not_receive_the_details_bag(
    three_roles,
):
    """`GET /audit/mine` was `_dump(entry) | {"details": entry.details}` — an
    explicit override of the projection, in the module that defines the single
    serialization point.

    The justification was "the details came from their own request, so there is
    nothing here they did not already have", and it is false for a whole class
    of rows: a viewer triggering a source sync supplies a *name*, and Laurelin
    builds the rest of the bag from an ADMIN-authored connector config.
    Reproduced: a viewer read a live endpoint, driver and rendered message out
    of this route.
    """
    admin, _editor, viewer = three_roles
    store = MetadataStore(_ws_of(admin))
    store.log_audit(
        "source_sync_failed",
        {"endpoint": "secret-db.internal.corp:55999", "password": "S3KRET_BAG"},
        actor="vic",
    )
    r = viewer.get("/api/v1/audit/mine")
    assert r.status_code == 200
    assert "S3KRET_BAG" not in r.text, r.text[:400]
    # They still learn *that* they did it, which is the stated need.
    assert any(row["action"] == "source_sync_failed" for row in r.json())
    assert all("details" not in row for row in r.json())


def test_an_audit_row_declared_editor_readable_actually_reaches_an_editor(three_roles):
    """`min_read_role` was inert: `list_audit` chose which rows an editor saw
    and then the serializer dropped `details` from all of them, because
    `AuditEvent` is class-level admin. All five `min_read_role=Role.editor`
    declarations in the tree were dead code, each with a comment asserting a
    disclosure that did not happen — while `/audit/mine` handed a viewer the
    same bag whole. The privilege ordering was inverted."""
    admin, editor, viewer = three_roles
    store = MetadataStore(_ws_of(admin))
    store.log_audit("source_sync_failed", {"source": "crm", "why": "editor_visible"},
                    actor="root", min_read_role=Role.editor)
    store.log_audit("engine_updated", {"uri": "grpc://x", "why": "admin_only"},
                    actor="root")

    rows = {r["action"]: r for r in editor.get("/api/v1/audit").json()}
    assert rows["source_sync_failed"]["details"]["why"] == "editor_visible"
    # An undeclared row is not offered to an editor at all — `min_read_role` is
    # still the row filter on this route. The declaration now decides *both*
    # questions, which is the fix: it used to choose the rows and then be
    # overruled on their contents.
    assert "engine_updated" not in rows
    # An admin reads both, and a viewer reaches neither route.
    admin_rows = {r["action"]: r for r in admin.get("/api/v1/audit").json()}
    assert admin_rows["engine_updated"]["details"]["why"] == "admin_only"
    assert viewer.get("/api/v1/audit").status_code == 403


def test_a_pre_migration_audit_row_is_not_readable_by_the_viewer_who_wrote_it(
    three_roles,
):
    """`_migrate_failures` stamps every pre-existing row `admin` precisely
    because "the rows were written by callers who had no idea who would read
    them" — and `/audit/mine` then exempted them from that stamp. Reproduced: a
    user demoted from editor to viewer read a live DSN and password out of a
    migrated row they had written themselves."""
    admin, _editor, viewer = three_roles
    store = MetadataStore(_ws_of(admin))
    store.log_audit(
        "source_sync_failed",
        {"error": "connection failed: postgresql://svc:OLD_AUDIT_PW@pg.internal/crm"},
        actor="vic",  # they wrote it, back when they were an editor
    )
    r = viewer.get("/api/v1/audit/mine")
    assert r.status_code == 200
    assert "OLD_AUDIT_PW" not in r.text, r.text[:400]


def _ws_of(admin_client):
    """The metadata path behind a TestClient's app."""
    return admin_client.app.state.workspace.metadata_path


def test_a_route_level_catch_does_not_return_a_librarys_words_either(
    workspace, monkeypatch
):
    """The global handlers are the net; these are the catches *above* it.

    Several `except (ValueError, KeyError)` blocks in the API wrap a call that
    reaches a third-party library — `catalog.iceberg_branch` goes into pyiceberg
    — and `pyarrow.lib.ArrowInvalid` **is** a `ValueError` while `ArrowKeyError`
    **is** a `KeyError`. Same class as the confirmed iceberg-warehouse
    disclosure, one level down, so `failure.safe_detail` answers it the same
    way: our message if we raised it, a `Failure` if we did not.
    """
    import pyarrow as pa

    from laurelin.core.failure import safe_detail

    try:
        raise ValueError("a first-party 400 the caller should read")
    except ValueError as exc:
        # (raised here, so not first-party — the point is the *other* branch)
        assert "err-" in safe_detail(exc)

    from laurelin.connectors.connectors import validate_source

    try:
        validate_source("not_a_connector", {})
    except ValueError as exc:
        assert "not_a_connector" in safe_detail(exc)

    try:
        pa.compute.cast(pa.array(["S3KRET_ROUTE_LEVEL"]), pa.int64())
    except ValueError as exc:
        detail = safe_detail(exc, subject="dataset:x")
        assert "S3KRET_ROUTE_LEVEL" not in detail
        assert "err-" in detail


def test_audit_filters_are_applied_server_side_before_the_limit(three_roles):
    """S4: the UI's filter bar sends since/until/actor/action and used to
    re-filter a 200-row window client-side — so a filter aimed at anything
    older than the newest 200 events rendered a false "No events match".
    The params now narrow the query itself, as ANDs applied AFTER the
    min_read_role visibility clause: a filter can only narrow what the role
    may read, never widen it."""
    admin, editor, _viewer = three_roles
    assert admin.put(
        "/api/v1/dashboards/f", json={"title": "f", "panels": []}
    ).status_code == 200

    # actor: exact match, applied in SQL — not the same bytes as unfiltered.
    everyone = admin.get("/api/v1/audit").json()
    just_root = admin.get("/api/v1/audit", params={"actor": "root"}).json()
    assert just_root and all(e["actor"] == "root" for e in just_root)
    nobody = admin.get("/api/v1/audit", params={"actor": "no_such_user"}).json()
    assert nobody == []
    assert len(everyone) >= len(just_root)

    # action: exact match.
    only = admin.get("/api/v1/audit", params={"action": "dashboard_created"}).json()
    assert only and all(e["action"] == "dashboard_created" for e in only)

    # since/until: inclusive dates. Everything logged today survives a
    # [today, today] window and nothing survives a window in the past.
    today = everyone[0]["timestamp"][:10]
    windowed = admin.get(
        "/api/v1/audit", params={"since": today, "until": today}
    ).json()
    assert len(windowed) == len(everyone)
    assert admin.get(
        "/api/v1/audit", params={"until": "1999-12-31"}
    ).json() == []
    # Filtering happens BEFORE the limit: with limit=1 and an actor filter,
    # the one row returned matches the filter rather than being the newest
    # row overall re-filtered to nothing.
    one = admin.get(
        "/api/v1/audit", params={"limit": 1, "actor": "root"}
    ).json()
    assert len(one) == 1 and one[0]["actor"] == "root"

    # No filter widens visibility: an editor filtering for an admin-level
    # action still reads only rows their role may read.
    for e in editor.get("/api/v1/audit", params={"actor": "root"}).json():
        assert e["min_read_role"] in ("viewer", "editor")

    # Malformed dates are refused, not silently ignored.
    assert admin.get(
        "/api/v1/audit", params={"since": "not-a-date"}
    ).status_code in (400, 422)
