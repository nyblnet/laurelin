"""Ontology definition authoring over the API (the governed YAML write path).

The ontology stays file-defined and is re-loaded per request, so the invariants
here are about the *files*: a write is live immediately, a duplicate api_name
is refused before it can brick the loader for every subsequent request, a
failed write rolls back, and hand-written files are never touched. The routes
are ADMIN-gated and audited with the server-resolved actor.
"""

from __future__ import annotations

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

import laurelin.ontology.authoring as authoring
from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore

ADMIN_CREDS = {"username": "root", "password": "trustno1!"}

HAND_ONTOLOGY = """
object_types:
  - api_name: legacy_city
    backing_dataset: cities
    primary_key: name
    properties:
      name: {type: string}
      pop: {type: integer}
"""


@pytest.fixture()
def env(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="ontauth")
    store = MetadataStore(ws.metadata_path)
    cat = DatasetCatalog(ws, store)
    cat.write("cities", pa.table({"name": ["valmar", "tirion"], "pop": [120, 340]}))
    cat.write("towns", pa.table({"town": ["osgiliath"], "mayor": ["f"]}))
    (ws.ontology_dir / "hand.yml").write_text(HAND_ONTOLOGY)

    app = create_app(ws)
    admin = TestClient(app, raise_server_exceptions=False)
    assert admin.post("/api/v1/auth/setup", json=ADMIN_CREDS).status_code == 200
    assert admin.post("/api/v1/auth/login", json=ADMIN_CREDS).status_code == 200

    admin.post("/api/v1/users", json={
        "username": "ed", "password": "password123", "role": "editor"})
    editor = TestClient(app, raise_server_exceptions=False)
    editor.post("/api/v1/auth/login", json={"username": "ed", "password": "password123"})

    return ws, app, admin, editor


def _put_town_type(client, api_name="town", **overrides):
    body = {
        "backing_dataset": "towns",
        "primary_key": "town",
        "properties": {"town": {"type": "string"}, "mayor": {"type": "string"}},
        "description": "a settlement",
    }
    body.update(overrides)
    return client.put(f"/api/v1/ontology/object-types/{api_name}", json=body)


def test_an_object_type_written_through_the_api_is_live_on_the_next_request(env):
    ws, _, admin, _ = env
    resp = _put_town_type(admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == []
    # No restart, no cache: the very next request serves the type and its rows.
    got = admin.get("/api/v1/ontology/object-types/town")
    assert got.status_code == 200
    assert got.json()["backing_dataset"] == "towns"
    objects = admin.get("/api/v1/ontology/objects/town").json()["objects"]
    assert [o["town"] for o in objects] == ["osgiliath"]
    # And the managed file is exactly one definition under its kind key.
    assert (ws.ontology_dir / "town.yml").is_file()


def test_object_type_authoring_requires_admin_and_refuses_an_editor_with_403(env):
    _, _, admin, editor = env
    resp = _put_town_type(editor)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Requires admin role (you are editor)"
    # The refusal wrote nothing.
    assert admin.get("/api/v1/ontology/object-types/town").status_code in (403, 404)


def test_link_and_action_authoring_require_admin_too(env):
    _, _, _, editor = env
    link = editor.put("/api/v1/ontology/link-types/lives_in", json={
        "from_type": "legacy_city", "to_type": "legacy_city",
        "from_property": "name", "to_property": "name"})
    assert link.status_code == 403
    action = editor.put("/api/v1/ontology/action-types/rename", json={
        "object_type": "legacy_city", "kind": "update",
        "parameters": {"name": {"type": "string", "required": True}}})
    assert action.status_code == 403
    for kind in ("object-types", "link-types", "action-types"):
        assert editor.delete(f"/api/v1/ontology/{kind}/legacy_city").status_code == 403


def test_a_duplicate_api_name_against_a_hand_written_file_is_a_409_and_the_ontology_stays_loadable(env):
    _, _, admin, _ = env
    resp = _put_town_type(admin, api_name="legacy_city")
    assert resp.status_code == 409
    assert "hand.yml" in resp.json()["detail"]
    # The refusal happened BEFORE disk: every ontology read still works.
    assert admin.get("/api/v1/ontology/object-types").status_code == 200
    assert admin.get("/api/v1/ontology/object-types/legacy_city").status_code == 200


def test_a_failed_write_never_leaves_the_merged_ontology_broken(env, monkeypatch):
    ws, _, admin, _ = env

    def boom(_dir):
        raise ValueError("simulated loader failure after the write")

    # Force the post-write postcondition to fail, as a stand-in for any fault
    # between os.replace and the response.
    monkeypatch.setattr(authoring, "load_ontology", boom)
    resp = _put_town_type(admin)
    assert resp.status_code == 500
    monkeypatch.undo()

    # Rolled back: no managed file, no half-written type, every read is a 200.
    assert not (ws.ontology_dir / "town.yml").exists()
    assert admin.get("/api/v1/ontology/object-types").status_code == 200
    names = [t["api_name"] for t in admin.get("/api/v1/ontology/object-types").json()]
    assert "town" not in names


def test_delete_refuses_to_touch_a_hand_written_definition_file(env):
    ws, _, admin, _ = env
    resp = admin.delete("/api/v1/ontology/object-types/legacy_city")
    assert resp.status_code == 409
    assert "hand.yml" in resp.json()["detail"]
    assert (ws.ontology_dir / "hand.yml").is_file()
    assert admin.get("/api/v1/ontology/object-types/legacy_city").status_code == 200


def test_deleting_an_api_managed_definition_removes_it_and_an_unknown_one_is_404(env):
    ws, _, admin, _ = env
    assert _put_town_type(admin).status_code == 200
    assert admin.delete("/api/v1/ontology/object-types/town").status_code == 200
    assert not (ws.ontology_dir / "town.yml").exists()
    assert admin.get("/api/v1/ontology/object-types/town").status_code in (403, 404)
    assert admin.delete("/api/v1/ontology/object-types/town").status_code == 404


def test_an_object_type_whose_backing_dataset_does_not_exist_is_a_404(env):
    _, _, admin, _ = env
    resp = _put_town_type(admin, backing_dataset="no_such_dataset")
    assert resp.status_code == 404


def test_property_names_missing_from_the_backing_schema_are_a_warning_not_a_refusal(env):
    _, _, admin, _ = env
    resp = _put_town_type(admin, properties={
        "town": {"type": "string"}, "elevation": {"type": "integer"}})
    assert resp.status_code == 200
    warnings = resp.json()["warnings"]
    assert any("elevation" in w for w in warnings)
    # The type still resolves; the phantom property just reads as empty.
    assert admin.get("/api/v1/ontology/object-types/town").status_code == 200


def test_link_types_require_both_endpoint_object_types_to_exist(env):
    _, _, admin, _ = env
    assert _put_town_type(admin).status_code == 200
    missing = admin.put("/api/v1/ontology/link-types/twinned_with", json={
        "from_type": "town", "to_type": "ghost_type",
        "from_property": "town", "to_property": "name"})
    assert missing.status_code == 404
    ok = admin.put("/api/v1/ontology/link-types/twinned_with", json={
        "from_type": "town", "to_type": "legacy_city",
        "from_property": "mayor", "to_property": "name"})
    assert ok.status_code == 200, ok.text
    # Live on the next request: the linked objects resolve through the join.
    linked = admin.get("/api/v1/ontology/objects/legacy_city/valmar/links/twinned_with")
    assert linked.status_code == 200


def test_actions_require_an_existing_object_type_and_a_valid_kind(env):
    _, _, admin, _ = env
    assert _put_town_type(admin).status_code == 200
    bad_type = admin.put("/api/v1/ontology/action-types/rename", json={
        "object_type": "ghost_type", "kind": "update",
        "parameters": {"town": {"type": "string"}}})
    assert bad_type.status_code == 404
    bad_kind = admin.put("/api/v1/ontology/action-types/rename", json={
        "object_type": "town", "kind": "explode", "parameters": {}})
    assert bad_kind.status_code == 400
    ok = admin.put("/api/v1/ontology/action-types/rename", json={
        "object_type": "town", "kind": "update",
        "parameters": {"mayor": {"type": "string", "required": True}}})
    assert ok.status_code == 200, ok.text
    # The authored action is applicable on the next request.
    applied = admin.post("/api/v1/ontology/actions/rename/apply", json={
        "pk": "osgiliath", "parameters": {"mayor": "faramir"}})
    assert applied.status_code == 200, applied.text
    assert admin.get("/api/v1/ontology/objects/town/osgiliath").json()["mayor"] == "faramir"


def test_an_api_name_that_is_not_a_safe_filename_is_refused(env):
    _, _, admin, _ = env
    for bad in ("..", "a/b", "A-Type", "x" * 70):
        resp = _put_town_type(admin, api_name=bad)
        # 400 from the api_name check; 404/405 when the path segment cannot
        # even route (".." normalizes away, "/" splits) — never a 2xx write.
        assert resp.status_code in (400, 404, 405), bad


def test_definition_writes_and_deletes_land_in_the_audit_log_with_the_server_resolved_actor(env):
    _, _, admin, _ = env
    assert _put_town_type(admin).status_code == 200
    assert admin.delete("/api/v1/ontology/object-types/town").status_code == 200
    rows = admin.get("/api/v1/audit", params={"limit": 50}).json()
    written = [r for r in rows if r["action"] == "object_type_written"]
    deleted = [r for r in rows if r["action"] == "object_type_deleted"]
    assert written and written[0]["actor"] == "root"
    assert deleted and deleted[0]["actor"] == "root"
