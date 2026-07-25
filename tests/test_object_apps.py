"""Object apps — curated views over the ontology.

The ontology explorer is generic. An app narrows it to one type, the columns
that matter and the actions an operator should reach for. The properties that
matter: definitions are validated against the live ontology, and an app grants
no access of its own.
"""

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Grant, SubjectKind

ONTOLOGY = """
object_types:
  - api_name: aircraft
    backing_dataset: fleet
    primary_key: tail_number
    title_property: tail_number
    properties:
      tail_number: {type: string}
      model: {type: string}
      status: {type: string}
  - api_name: flight
    backing_dataset: flights
    primary_key: flight_id
    properties:
      flight_id: {type: string}
      tail_number: {type: string}
link_types:
  - api_name: aircraft_flights
    from: aircraft
    to: flight
    cardinality: one_to_many
    from_property: tail_number
    to_property: tail_number
actions:
  - api_name: update_aircraft_status
    object_type: aircraft
    kind: update
    parameters:
      status: {type: string, required: true}
  - api_name: cancel_flight
    object_type: flight
    kind: update
    parameters:
      status: {type: string, required: true}
"""

CREDS = {"username": "root", "password": "trustno1!"}

APP = {
    "title": "Fleet Operations",
    "object_type": "aircraft",
    "columns": ["tail_number", "status"],
    "filters": {"status": "maintenance"},
    "actions": ["update_aircraft_status"],
    "links": ["aircraft_flights"],
}


@pytest.fixture()
def clients(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURELIN_SCHEDULER", "0")
    ws = Workspace.init(tmp_path / "ws", name="apps")
    store = MetadataStore(ws.metadata_path)
    cat = DatasetCatalog(ws, store)
    cat.write("fleet", pa.table({
        "tail_number": ["N1", "N2", "N3"],
        "model": ["A320", "B737", "A320"],
        "status": ["maintenance", "active", "maintenance"],
    }))
    cat.write("flights", pa.table({"flight_id": ["F1"], "tail_number": ["N1"]}))
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)

    app = create_app(ws)
    admin = TestClient(app)
    admin.post("/api/v1/auth/setup", json=CREDS)
    admin.post("/api/v1/auth/login", json=CREDS)
    admin.post("/api/v1/users", json={"username": "vic", "password": "password123",
                                      "role": "viewer"})
    viewer = TestClient(app)
    viewer.post("/api/v1/auth/login", json={"username": "vic", "password": "password123"})
    return admin, viewer, store


# -- definition ------------------------------------------------------------------

def test_app_lifecycle(clients):
    admin, viewer, _ = clients
    r = admin.put("/api/v1/apps/fleet_ops", json=APP)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Fleet Operations"
    assert body["columns"] == ["tail_number", "status"]
    assert body["filters"] == {"status": "maintenance"}

    # Viewers can open apps — they are a presentation of data they can see.
    assert [a["name"] for a in viewer.get("/api/v1/apps").json()] == ["fleet_ops"]
    assert viewer.get("/api/v1/apps/fleet_ops").json()["object_type"] == "aircraft"

    assert admin.delete("/api/v1/apps/fleet_ops").status_code == 200
    assert viewer.get("/api/v1/apps/fleet_ops").status_code == 404


@pytest.mark.parametrize("patch, msg", [
    ({"object_type": "spaceship"}, "Unknown object type"),
    ({"columns": ["tail_number", "nope"]}, "Unknown property"),
    ({"filters": {"ghost": "x"}}, "Unknown filter property"),
    ({"actions": ["cancel_flight"]}, "not defined on"),      # wrong object type
    ({"actions": ["nonexistent"]}, "not defined on"),
    ({"links": ["nonexistent"]}, "does not involve"),
])
def test_definitions_are_validated_against_the_ontology(clients, patch, msg):
    """A misconfigured app must fail at save, not in front of a user."""
    admin, _, _ = clients
    r = admin.put("/api/v1/apps/bad", json={**APP, **patch})
    assert r.status_code == 400, r.text
    assert msg in r.json()["detail"]


def test_bad_name_is_rejected(clients):
    admin, _, _ = clients
    assert admin.put("/api/v1/apps/Bad Name", json=APP).status_code == 400


def test_defining_an_app_is_admin_only(clients):
    """An app shapes what a whole team sees."""
    admin, viewer, _ = clients
    assert viewer.put("/api/v1/apps/x", json=APP).status_code == 403
    assert viewer.delete("/api/v1/apps/x").status_code == 403


def test_title_defaults_to_the_name(clients):
    admin, _, _ = clients
    body = admin.put("/api/v1/apps/plain",
                     json={"object_type": "aircraft"}).json()
    assert body["title"] == "plain"
    assert body["columns"] == [] and body["actions"] == []


# -- an app grants no access of its own --------------------------------------------

def test_an_app_over_a_hidden_type_is_invisible(clients):
    """Apps are presentation; they must not become a side door into data the
    ontology hides."""
    admin, viewer, _ = clients
    admin.put("/api/v1/apps/fleet_ops", json=APP)
    assert len(viewer.get("/api/v1/apps").json()) == 1

    # Lock the object type to admins only.
    admin.put("/api/v1/ontology/permissions/aircraft", json={"grants": [
        Grant(subject_kind=SubjectKind.user, subject="root",
              can_view=True).model_dump(mode="json")
    ]})

    assert viewer.get("/api/v1/apps").json() == [], "hidden type -> hidden app"
    assert viewer.get("/api/v1/apps/fleet_ops").status_code == 403
    assert admin.get("/api/v1/apps/fleet_ops").status_code == 200


def test_an_app_over_a_locked_backing_dataset_is_invisible(clients):
    """Object-type access composes with the backing dataset, so an app follows
    that composition too."""
    admin, viewer, _ = clients
    admin.put("/api/v1/apps/fleet_ops", json=APP)
    admin.put("/api/v1/datasets/fleet/permissions", json={"grants": [
        Grant(subject_kind=SubjectKind.user, subject="root",
              can_view=True).model_dump(mode="json")
    ]})
    assert viewer.get("/api/v1/apps").json() == []
    assert viewer.get("/api/v1/apps/fleet_ops").status_code == 403


def test_app_objects_come_from_the_ordinary_ontology_endpoints(clients):
    """The app carries configuration; the data path is unchanged, so filters
    and permissions behave exactly as they do in the explorer."""
    admin, _, _ = clients
    admin.put("/api/v1/apps/fleet_ops", json=APP)
    app_def = admin.get("/api/v1/apps/fleet_ops").json()

    params = "&".join(f"filter.{k}={v}" for k, v in app_def["filters"].items())
    r = admin.get(f"/api/v1/ontology/objects/{app_def['object_type']}?{params}")
    assert r.status_code == 200
    tails = {o["tail_number"] for o in r.json()["objects"]}
    assert tails == {"N1", "N3"}, "the app's filter scopes it to maintenance"
