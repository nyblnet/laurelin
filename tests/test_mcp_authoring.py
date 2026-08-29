"""MCP authoring surface: agents build a workspace through the same governed
routes as any user.

The sharing mechanism is HTTP, not Python imports: every MCP tool body is a
single ``LaurelinClient`` call carrying the token, so role gates, lock flags,
per-resource entitlements, author stamping, R2 serialization and audit all
execute exactly once — in the route. These tests pin that for the
pipeline-authoring group (datasets, sources, flows, builds): a viewer is
refused, an editor succeeds, a flows-locked server refuses flow tools, and a
flow authored over MCP builds under the #58 author-entitlement check.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

import laurelin.mcp.server as mcp_server_module
from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.auth import AuthService
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Grant, Role, SubjectKind
from laurelin.mcp import LaurelinClient
from laurelin.mcp.client import LaurelinError

ADMIN_CREDS = {"username": "root", "password": "trustno1!"}

# The reference flow from tests/test_flow_api.py: filter -> aggregate -> sort
# over the `orders` dataset, producing `busy_regions`.
FLOW = {
    "output": "busy_regions",
    "terminal": "n3",
    "nodes": [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
            "predicate": {"t": "op", "op": "ne", "args": [
                {"t": "col", "name": "status"},
                {"t": "lit", "type": "string", "value": "returned"}]}}},
        {"id": "n2", "kind": "aggregate", "inputs": ["n1"], "params": {
            "group_by": ["region"],
            "aggs": [{"fn": "sum", "column": "amount", "as": "total"}]}},
        {"id": "n3", "kind": "sort", "inputs": ["n2"], "params": {
            "by": [{"column": "region", "dir": "asc", "nulls": "last"}]}},
    ],
}


@pytest.fixture()
def env(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="mcpauthor")
    store = MetadataStore(ws.metadata_path)
    cat = DatasetCatalog(ws, store)
    cat.write("orders", pa.table({
        "region": ["us", "us", "eu", "eu"],
        "status": ["ok", "returned", "ok", "ok"],
        "amount": [10, 99, 20, 5],
    }))

    app = create_app(ws)
    boot = TestClient(app)
    assert boot.post("/api/v1/auth/setup", json=ADMIN_CREDS).status_code == 200
    assert boot.post("/api/v1/auth/login", json=ADMIN_CREDS).status_code == 200
    tokens = {"admin": boot.post("/api/v1/tokens", json={"name": "a"}).json()["token"]}

    # A viewer cannot mint an API token through the API (POST /tokens is
    # editor-gated), so the low-role tokens come from the auth service — the
    # same records the route would create.
    auth = AuthService(store)
    ed = auth.create_user("ed", "password123", Role.editor, actor="root")
    vi = auth.create_user("vi", "password123", Role.viewer, actor="root")
    tokens["editor"], _ = auth.create_api_token(ed, "edagent")
    tokens["viewer"], _ = auth.create_api_token(vi, "viagent")

    return ws, app, boot, tokens


def client_for(app, token: str) -> LaurelinClient:
    # TestClient is an httpx.Client, so the SDK runs against the real app over
    # the real HTTP surface — Bearer token only, no cookies.
    return LaurelinClient(token=token, http=TestClient(app))


# ---------------------------------------------------------------------------
# The invariant that makes every other test in this file structural: the MCP
# server has no import through which it *could* bypass a route.
# ---------------------------------------------------------------------------


def test_mcp_server_imports_nothing_but_the_client():
    """A tool that called a service directly would enforce governance zero
    times or twice — the repo's recurring hole. The server module may import
    only the client (plus json/typing and the MCP framework), so every tool is
    forced through the HTTP surface where the route's gates run."""
    tree = ast.parse(Path(mcp_server_module.__file__).read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    allowed = {"__future__", "json", "typing", "laurelin.mcp.client", "mcp.server.fastmcp"}
    assert modules <= allowed, f"unexpected imports in mcp/server.py: {modules - allowed}"
    forbidden = {m for m in modules
                 if m.startswith("laurelin.") and m != "laurelin.mcp.client"}
    assert not forbidden


# ---------------------------------------------------------------------------
# Role gates, through MCP
# ---------------------------------------------------------------------------


def _pipeline_write_calls():
    return [
        ("create_dataset", lambda c: c.create_dataset("viewer_made_this")),
        ("create_source", lambda c: c.upsert_source(
            "crm", "file", "orders", {"path": "/tmp/x.csv"})),
        ("delete_source", lambda c: c.delete_source("crm")),
        ("flow_dataset_schema", lambda c: c.flow_dataset_schema("orders")),
        ("preview_flow", lambda c: c.preview_flow(FLOW | {"name": "busy_regions"})),
        ("write_flow", lambda c: c.write_flow("busy_regions", FLOW)),
        ("delete_flow", lambda c: c.delete_flow("busy_regions")),
    ]


def test_every_pipeline_authoring_tool_refuses_a_viewer_token_with_403(env):
    _, app, _, tokens = env
    c = client_for(app, tokens["viewer"])
    for name, call in _pipeline_write_calls():
        with pytest.raises(LaurelinError) as err:
            call(c)
        assert err.value.status == 403, f"{name}: expected 403, got {err.value.status}"
    c.close()


def test_a_forged_token_gets_401_from_every_pipeline_authoring_tool(env):
    _, app, _, _ = env
    c = client_for(app, "laurelin_forged_token")
    for name, call in _pipeline_write_calls():
        with pytest.raises(LaurelinError) as err:
            call(c)
        assert err.value.status == 401, f"{name}: expected 401, got {err.value.status}"
    c.close()


def test_source_management_over_mcp_refuses_an_editor_with_the_exact_rest_detail(env):
    """Admin-gated source management refuses an editor identically over MCP
    and REST — byte-for-byte the same detail, because it is the same route.
    That equality is the proof there is one governance path, not two."""
    _, app, _, tokens = env
    c = client_for(app, tokens["editor"])
    with pytest.raises(LaurelinError) as err:
        c.upsert_source("crm", "file", "orders", {"path": "/tmp/x.csv"})
    assert err.value.status == 403

    rest = TestClient(app).put(
        "/api/v1/sources/crm",
        json={"type": "file", "dataset": "orders", "config": {"path": "/tmp/x.csv"}},
        headers={"Authorization": f"Bearer {tokens['editor']}"},
    )
    assert rest.status_code == 403
    assert err.value.detail == rest.json()["detail"]
    c.close()


# ---------------------------------------------------------------------------
# The editor path: author, preview, build — and verify the work
# ---------------------------------------------------------------------------


def test_an_editor_authors_previews_builds_and_reads_back_a_flow_over_mcp(env):
    _, app, _, tokens = env
    c = client_for(app, tokens["editor"])

    schema = c.flow_dataset_schema("orders")
    assert set(schema["columns"]) == {"region", "status", "amount"}

    preview = c.preview_flow(FLOW | {"name": "busy_regions"}, max_rows=10)
    assert {r["region"]: r["total"] for r in preview["rows"]} == {"us": 10, "eu": 25}

    saved = c.write_flow("busy_regions", FLOW)
    assert saved["name"] == "busy_regions"
    assert saved["schema"] == ["region", "total"]

    build = c.run_build(targets=["busy_regions"], wait=True)
    assert build["status"] == "succeeded", build

    rows = c.dataset_rows("busy_regions")
    # int(): sum(int64) lands as a DuckDB hugeint, which the JSON layer
    # serializes as a string.
    assert {r["region"]: int(r["total"]) for r in rows["rows"]} == {"us": 10, "eu": 25}
    c.close()


def test_a_flow_authored_over_mcp_is_stamped_with_the_token_user_not_any_payload_field(env):
    """The recorded author is whose read access every future build is checked
    against (#58), so a payload that could set it could pick whose rights to
    borrow. The server stamps it from the authenticated token."""
    _, app, _, tokens = env
    c = client_for(app, tokens["editor"])
    saved = c.write_flow("busy_regions", FLOW | {"author": "root"})
    assert saved["flow"]["author"] == "ed"
    c.close()


def test_a_flow_authored_over_mcp_builds_only_while_its_stamped_author_can_read_the_input(env):
    """The build-time input-entitlement check binds to the MCP-stamped author,
    not the principal who triggers the build: once the editor loses view
    access to `orders`, even the admin's build of the editor's flow fails."""
    _, app, boot, tokens = env
    ed = client_for(app, tokens["editor"])
    ed.write_flow("busy_regions", FLOW)
    assert ed.run_build(targets=["busy_regions"], wait=True)["status"] == "succeeded"

    # Admin locks `orders` down to root only. The flow file is unchanged.
    r = boot.put(
        "/api/v1/datasets/orders/permissions",
        json={"grants": [Grant(
            subject_kind=SubjectKind.user, subject="root",
            can_view=True, can_edit=True,
        ).model_dump(mode="json")]},
    )
    assert r.status_code == 200, r.text

    admin = client_for(app, tokens["admin"])
    build = admin.run_build(targets=["busy_regions"], wait=True)
    assert build["status"] == "failed"
    tasks = {t["transform_name"]: t for t in build["tasks"]}
    assert tasks["busy_regions"]["status"] == "failed"
    # FlowRefused, not TransformRefused: a flow's build-time entitlement runs
    # through flow_governance.check_flow_sources, refusing as its author.
    assert tasks["busy_regions"]["failure"]["exc_class"] == "FlowRefused"
    ed.close()
    admin.close()


# ---------------------------------------------------------------------------
# Lock flags bind MCP exactly as they bind REST
# ---------------------------------------------------------------------------


def test_flow_authoring_over_mcp_is_refused_on_a_flow_locked_server(env):
    ws, app, _, tokens = env
    client_for(app, tokens["editor"]).write_flow("busy_regions", FLOW)

    locked = create_app(ws, lock_flows=True)
    c = client_for(locked, tokens["editor"])
    with pytest.raises(LaurelinError) as err:
        c.write_flow("busy_regions", FLOW)
    assert err.value.status == 403
    assert "--lock-flows" in err.value.detail
    with pytest.raises(LaurelinError) as err:
        c.delete_flow("busy_regions")
    assert err.value.status == 403

    # Reads and previews were never authoring and stay open under the lock.
    assert c.preview_flow(FLOW | {"name": "busy_regions"})["rows"]
    c.close()


def test_locking_pipelines_does_not_block_flow_authoring_over_mcp(env):
    """`--lock-pipelines` locks *code*; flows are the safe no-code path an
    agent is supposed to keep on a hardened server."""
    ws, _, _, tokens = env
    locked = create_app(ws, lock_pipelines=True)
    c = client_for(locked, tokens["editor"])
    assert c.write_flow("busy_regions", FLOW)["name"] == "busy_regions"
    c.close()


# ---------------------------------------------------------------------------
# Sources end to end, and R2 through MCP
# ---------------------------------------------------------------------------


def test_an_admin_can_register_and_sync_a_file_source_over_mcp(env, tmp_path):
    _, app, _, tokens = env
    csv = tmp_path / "cities.csv"
    csv.write_text("name,pop\nvalmar,120\ntirion,340\n")

    admin = client_for(app, tokens["admin"])
    admin.create_dataset("cities", description="synced from landed csv")
    src = admin.upsert_source("cities_csv", "file", "cities", {"path": str(csv)})
    assert src["name"] == "cities_csv"

    admin.sync_source("cities_csv")
    rows = admin.dataset_rows("cities")
    assert {r["name"] for r in rows["rows"]} == {"valmar", "tirion"}

    assert admin.delete_source("cities_csv") == {"deleted": "cities_csv"}
    admin.close()


def test_an_editor_reading_sources_over_mcp_never_sees_connector_config(env, tmp_path):
    """R2 through MCP: `config` (which can carry DSNs and server paths) is
    admin-audience; an editor's listing must not contain the key at all."""
    _, app, _, tokens = env
    admin = client_for(app, tokens["admin"])
    admin.upsert_source("landed", "file", "orders", {"path": str(tmp_path / "x.csv")})

    listed = client_for(app, tokens["editor"]).list_sources()
    assert [s["name"] for s in listed] == ["landed"]
    assert all("config" not in s for s in listed)

    # The admin who may write it may read it (still redaction-processed).
    assert "config" in admin.list_sources()[0]
    admin.close()


# ---------------------------------------------------------------------------
# Tool listing: flows in, Python out
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="mcp package not installed (pip install laurelin[mcp])",
)
def test_mcp_exposes_flow_authoring_but_no_python_pipeline_authoring(env):
    """Agent-authored Python exec'd by the server is the exact surface
    --lock-pipelines exists to close; MCP authors flows only."""
    import asyncio

    from laurelin.mcp import build_server

    _, app, _, tokens = env
    server = build_server(client_for(app, tokens["admin"]))
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {
        "create_dataset", "create_source", "delete_source",
        "flow_dataset_schema", "preview_flow", "write_flow", "delete_flow",
        "run_build", "get_build", "sync_source",
    } <= names
    assert not any("pipeline" in n for n in names)
    # Guards eject_sql (flow -> exec'd Python), not the "eject" hiding inside
    # reject_proposal — an approvals tool that authors nothing.
    assert not any("eject" in n and "reject" not in n for n in names)
