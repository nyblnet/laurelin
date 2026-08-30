"""MCP governance + ontology-authoring tools inherit ALL governance, invent none.

Every tool body is one ``LaurelinClient`` call carrying the token, so the role
gate, entitlement checks, audit and R2 run once — in the route. These tests
pin that for the ontology-authoring group (object/link/action types, index,
writeback, per-type grants) and the governance group (markings, clearances,
dataset grants, policies, users, groups): a viewer is refused per tool, an
editor is refused by admin-gated tools with the exact REST detail, the
editor-gated tools succeed for an editor, and an admin's writes take effect
and bind other MCP callers immediately.
"""

from __future__ import annotations

import importlib.util

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.auth import AuthService
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Role
from laurelin.mcp import LaurelinClient
from laurelin.mcp.client import LaurelinError

ADMIN_CREDS = {"username": "root", "password": "trustno1!"}

HAND_ONTOLOGY = """
object_types:
  - api_name: city
    backing_dataset: cities
    primary_key: name
    properties:
      name: {type: string}
      pop: {type: integer}
actions:
  - api_name: adjust_pop
    object_type: city
    kind: update
    parameters:
      pop: {type: integer, required: true}
"""

# Every admin-gated tool in the ontology-authoring + governance groups, as
# (tool_name, call). The call arguments are valid, so the ONLY thing standing
# between a low-role token and the mutation is the route's gate.
ADMIN_TOOLS = [
    ("put_object_type", lambda c: c.put_object_type("town", "towns", "town")),
    ("delete_object_type", lambda c: c.delete_object_type("town")),
    ("put_link_type", lambda c: c.put_link_type(
        "twin", "city", "city", "name", "name")),
    ("delete_link_type", lambda c: c.delete_link_type("twin")),
    ("put_action_type", lambda c: c.put_action_type(
        "rename", "city", "update", {"name": {"type": "string"}})),
    ("delete_action_type", lambda c: c.delete_action_type("rename")),
    ("set_object_type_grants", lambda c: c.set_object_type_grants("city", [])),
    ("create_marking", lambda c: c.create_marking("secret")),
    ("set_dataset_markings", lambda c: c.set_dataset_markings("cities", [])),
    ("set_user_clearances", lambda c: c.set_user_clearances("ed", [])),
    ("set_dataset_grants", lambda c: c.set_dataset_grants("cities", [])),
    ("set_dataset_policy", lambda c: c.set_dataset_policy("cities")),
    ("create_user", lambda c: c.create_user("newbie", "password123", "viewer")),
    ("create_group", lambda c: c.create_group("analysts")),
    ("set_group_members", lambda c: c.set_group_members("analysts", [])),
]

# Admin-gated governance READ-BACK tools: governance must be verifiable over
# MCP, not write-only, and the read side is gated exactly like the write side.
ADMIN_READBACK_TOOLS = [
    ("list_dataset_markings", lambda c: c.list_dataset_markings()),
    ("list_dataset_grants", lambda c: c.list_dataset_grants()),
    ("list_dataset_policies", lambda c: c.list_dataset_policies()),
    ("list_object_type_grants", lambda c: c.list_object_type_grants()),
    ("get_user_clearances", lambda c: c.get_user_clearances("ed")),
]

# Editor-gated tools in this group (per-object-type entitlement on top).
EDITOR_TOOLS = [
    ("build_object_index", lambda c: c.build_object_index("city")),
    ("enable_writeback", lambda c: c.enable_writeback("city")),
]

ALL_TOOLS = ADMIN_TOOLS + ADMIN_READBACK_TOOLS + EDITOR_TOOLS


@pytest.fixture()
def env(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="mcpgov")
    store = MetadataStore(ws.metadata_path)
    cat = DatasetCatalog(ws, store)
    cat.write("cities", pa.table({"name": ["valmar", "tirion"], "pop": [120, 340]}))
    cat.write("towns", pa.table({"town": ["osgiliath"], "mayor": ["f"]}))
    (ws.ontology_dir / "hand.yml").write_text(HAND_ONTOLOGY)

    app = create_app(ws)
    admin_http = TestClient(app)
    assert admin_http.post("/api/v1/auth/setup", json=ADMIN_CREDS).status_code == 200
    assert admin_http.post("/api/v1/auth/login", json=ADMIN_CREDS).status_code == 200
    admin_token = admin_http.post(
        "/api/v1/tokens", json={"name": "agent"}).json()["token"]

    # A viewer cannot mint an API token through the API (POST /tokens is
    # editor-gated), so the low-role tokens come from the auth service — the
    # same records the route would create.
    auth = AuthService(store)
    ed = auth.create_user("ed", "password123", Role.editor, actor="root")
    vi = auth.create_user("vi", "password123", Role.viewer, actor="root")
    editor_token, _ = auth.create_api_token(ed, "edagent")
    viewer_token, _ = auth.create_api_token(vi, "viagent")

    def client_for(token: str) -> LaurelinClient:
        return LaurelinClient(token=token, http=TestClient(app))

    return app, client_for, admin_http, admin_token, editor_token, viewer_token


def test_every_governance_write_tool_refuses_a_viewer_token_with_403(env):
    _, client_for, *_, viewer_token = env
    viewer = client_for(viewer_token)
    for name, call in ALL_TOOLS:
        with pytest.raises(LaurelinError) as err:
            call(viewer)
        assert err.value.status == 403, f"{name}: expected 403, got {err.value.status}"


def test_admin_gated_tools_refuse_an_editor_token_exactly_as_rest_does(env):
    _, client_for, _, _, editor_token, _ = env
    editor = client_for(editor_token)
    for name, call in ADMIN_TOOLS + ADMIN_READBACK_TOOLS:
        with pytest.raises(LaurelinError) as err:
            call(editor)
        assert err.value.status == 403, f"{name}: expected 403, got {err.value.status}"
        # Byte-for-byte the REST gate's detail, because it IS the REST gate:
        # require_admin (and require_superadmin in single-workspace mode)
        # renders exactly this sentence.
        assert err.value.detail == "Requires admin role (you are editor)", name


def test_the_mcp_refusal_is_byte_for_byte_the_rest_detail_for_the_same_call(env):
    app, client_for, _, _, editor_token, _ = env
    editor = client_for(editor_token)
    with pytest.raises(LaurelinError) as err:
        editor.put_object_type("town", "towns", "town")
    rest = TestClient(app).put(
        "/api/v1/ontology/object-types/town",
        json={"backing_dataset": "towns", "primary_key": "town"},
        headers={"Authorization": f"Bearer {editor_token}"},
    )
    assert rest.status_code == err.value.status == 403
    assert rest.json()["detail"] == err.value.detail


def test_editor_gated_tools_succeed_for_an_editor(env):
    _, client_for, _, _, editor_token, _ = env
    editor = client_for(editor_token)
    indexed = editor.build_object_index("city")
    assert indexed["object_type"] == "city" and indexed["objects"] == 2
    folded = editor.enable_writeback("city")
    assert folded["object_type"] == "city" and folded["folded"] == 0


def test_a_forged_token_gets_401_from_every_governance_tool(env):
    _, client_for, *_ = env
    forged = client_for("laurelin_forged_token")
    for name, call in ALL_TOOLS:
        with pytest.raises(LaurelinError) as err:
            call(forged)
        assert err.value.status == 401, f"{name}: expected 401, got {err.value.status}"


def test_an_admin_authors_the_ontology_over_mcp_and_it_is_live_immediately(env):
    _, client_for, admin_http, admin_token, _, _ = env
    admin = client_for(admin_token)

    written = admin.put_object_type(
        "town", "towns", "town",
        properties={"town": {"type": "string"}, "mayor": {"type": "string"}},
    )
    assert written["object_type"]["api_name"] == "town"
    assert written["warnings"] == []

    admin.put_link_type("seat_of", "town", "city", "mayor", "name")
    admin.put_action_type(
        "rename_mayor", "town", "update",
        parameters={"mayor": {"type": "string", "required": True}},
    )

    # Live on the next request, through the existing MCP read tools.
    assert admin.get_object_type("town")["backing_dataset"] == "towns"
    assert "rename_mayor" in [a["api_name"] for a in admin.list_actions()]
    assert admin.build_object_index("town")["objects"] == 1

    # And every write is in the audit log, attributed to the token's user.
    rows = admin_http.get("/api/v1/audit", params={"limit": 50}).json()
    by_action = {r["action"] for r in rows}
    assert {"object_type_written", "link_type_written", "action_type_written"} <= by_action
    for r in rows:
        if r["action"].endswith("_written"):
            assert r["actor"] == "root"

    # Deletes go back out through the same governed path.
    assert admin.delete_action_type("rename_mayor")["deleted"] == "rename_mayor"
    assert admin.delete_link_type("seat_of")["deleted"] == "seat_of"
    assert admin.delete_object_type("town")["deleted"] == "town"


def test_governance_set_over_mcp_binds_other_mcp_callers_immediately(env):
    _, client_for, _, admin_token, _, viewer_token = env
    admin = client_for(admin_token)
    viewer = client_for(viewer_token)

    # Grants: locking towns to root cuts the viewer off mid-session.
    assert viewer.dataset_rows("towns")["rows"]
    admin.set_dataset_grants("towns", [
        {"subject_kind": "user", "subject": "root", "can_view": True, "can_edit": True}
    ])
    with pytest.raises(LaurelinError) as err:
        viewer.dataset_rows("towns")
    # 404, not 403: a view refusal must not confirm the dataset exists.
    # (An *edit* refusal stays 403 — that caller can already see it.)
    assert err.value.status == 404

    # Markings: fail closed until the clearance arrives, open after.
    admin.create_marking("secret", description="crown jewels")
    marked = admin.set_dataset_markings("cities", ["secret"])
    assert marked["effective"] == ["secret"]
    with pytest.raises(LaurelinError) as err:
        viewer.dataset_rows("cities")
    # 404 for the same reason as the grant case above: a marking the caller
    # has no clearance for hides the dataset rather than confirming it.
    assert err.value.status == 404
    admin.set_user_clearances("vi", ["secret"])
    assert len(viewer.dataset_rows("cities")["rows"]) == 2

    # Users and groups: members must exist first (the documented order).
    admin.create_user("svc_migration", "password1234", "editor")
    admin.create_group("analysts")
    with pytest.raises(LaurelinError) as err:
        admin.set_group_members("analysts", ["nobody_yet"])
    assert err.value.status == 400
    assert admin.set_group_members(
        "analysts", ["svc_migration", "ed"])["members"] == ["ed", "svc_migration"]

    # Policy: a column mask set over MCP masks the viewer's MCP reads.
    admin.set_dataset_policy("cities", column_masks=[
        {"column": "pop", "mode": "redact"}])
    rows = viewer.dataset_rows("cities")["rows"]
    assert {r["pop"] for r in rows} == {"***"}
    admin_rows = admin.dataset_rows("cities")["rows"]
    assert {r["pop"] for r in admin_rows} == {120, 340}


@pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="mcp package not installed (pip install laurelin[mcp])",
)
def test_the_governance_tools_are_listed_and_callable_on_the_mcp_server(env):
    import asyncio
    import json

    _, client_for, _, admin_token, _, _ = env
    from laurelin.mcp import build_server

    server = build_server(client_for(admin_token))
    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert {name for name, _ in ALL_TOOLS} <= tools

    result = asyncio.run(server.call_tool("put_object_type", {
        "api_name": "town", "backing_dataset": "towns", "primary_key": "town",
        "properties": {"town": {"type": "string"}},
    }))
    content = result[0] if isinstance(result, tuple) else result
    text = content[0].text if isinstance(content, list) else str(content)
    assert json.loads(text)["object_type"]["api_name"] == "town"


def test_governance_state_is_readable_over_mcp_including_marking_propagation(env):
    """Every governance verb used to be write-only over MCP: the
    set_dataset_markings response reports only that one dataset, so what
    propagated down lineage — and grants, policies, clearances — could only be
    verified over raw REST or by re-issuing writes and trusting the echo. An
    agent must be able to VERIFY governance through the same surface it set
    it with."""
    _, client_for, _, admin_token, _, _ = env
    admin = client_for(admin_token)

    # Lineage: a flow reading `cities`, built so the edge is recorded.
    admin.write_flow("city_pops", {
        "name": "city_pops", "output": "city_pops", "terminal": "n1",
        "nodes": [
            {"id": "n0", "kind": "source", "inputs": [],
             "params": {"dataset": "cities"}},
            {"id": "n1", "kind": "aggregate", "inputs": ["n0"], "params": {
                "group_by": ["name"],
                "aggs": [{"fn": "sum", "column": "pop", "as": "pop"}]}},
        ],
    })
    assert admin.run_build(targets=["city_pops"], wait=True)["status"] == "succeeded"

    admin.create_marking("phi")
    marked = admin.set_dataset_markings("cities", ["phi"])
    # The write's echo covers only the dataset it named...
    assert marked == {"dataset": "cities", "explicit": ["phi"], "effective": ["phi"]}
    # ...and the read-back tool shows the downstream propagation.
    markings = {m["dataset"]: m for m in admin.list_dataset_markings()}
    assert markings["city_pops"]["explicit"] == []
    assert markings["city_pops"]["effective"] == ["phi"]

    # Grants, policies, per-type grants and clearances all read back too.
    admin.set_dataset_grants("towns", [
        {"subject_kind": "user", "subject": "root", "can_view": True, "can_edit": True}])
    grants = {g["dataset"]: g["grants"] for g in admin.list_dataset_grants()}
    assert [g["subject"] for g in grants["towns"]] == ["root"]

    admin.set_dataset_policy("towns", column_masks=[
        {"column": "mayor", "mode": "hash"}])
    policies = {p["dataset"]: p["policy"] for p in admin.list_dataset_policies()}
    assert policies["towns"]["column_masks"][0]["mode"] == "hash"

    admin.set_object_type_grants("city", [
        {"subject_kind": "role", "subject": "admin", "can_view": True, "can_edit": True}])
    tgrants = {t["object_type"]: t["grants"] for t in admin.list_object_type_grants()}
    assert [g["subject"] for g in tgrants["city"]] == ["admin"]

    admin.set_user_clearances("ed", ["phi"])
    assert admin.get_user_clearances("ed") == {"username": "ed", "markings": ["phi"]}
