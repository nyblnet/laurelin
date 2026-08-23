"""MCP presentation tools: dashboards and schedules, governed by the routes.

Every tool here is a thin wrapper over the same REST route the UI uses, called
with the token's own principal — so these tests are about the governance
properties that must hold *through* MCP: role gates refuse exactly as REST
does (byte-for-byte the same detail, because it IS the same route), R2 keeps a
panel's query text away from a viewer token, and running a panel executes as
the caller, not as the author.
"""

import importlib.util

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.auth import AuthService
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Grant, Role, SubjectKind
from laurelin.mcp import LaurelinClient
from laurelin.mcp.client import LaurelinError

CREDS = {"username": "root", "password": "trustno1!"}

ONTOLOGY = """
object_types:
  - api_name: city
    backing_dataset: cities
    primary_key: name
    properties:
      name: {type: string}
      pop: {type: integer}
"""

SQL_PANEL = {"id": "p1", "title": "Populations", "sql": "SELECT name, pop FROM cities", "chart": "table"}
OBJECT_PANEL = {
    "id": "p2", "title": "City count", "object_type": "city",
    "metrics": [{"op": "count", "alias": "cities"}],
}
SECRET_PANEL = {"id": "p3", "title": "Secret", "sql": "SELECT * FROM secret_ds"}


@pytest.fixture()
def env(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="mcp-pres")
    store = MetadataStore(ws.metadata_path)
    cat = DatasetCatalog(ws, store)
    cat.write("cities", pa.table({"name": ["valmar", "tirion"], "pop": [120, 340]}))
    cat.write("secret_ds", pa.table({"k": ["classified"]}))
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)

    app = create_app(ws)
    boot = TestClient(app)
    assert boot.post("/api/v1/auth/setup", json=CREDS).status_code == 200
    assert boot.post("/api/v1/auth/login", json=CREDS).status_code == 200
    admin_token = boot.post("/api/v1/tokens", json={"name": "agent"}).json()["token"]

    # A viewer cannot mint an API token through the API (POST /tokens is
    # editor-gated), so the low-role tokens come from the auth service — the
    # same records the route would create.
    auth = AuthService(store)
    ed = auth.create_user("ed", "password123", Role.editor, actor="root")
    vi = auth.create_user("vi", "password123", Role.viewer, actor="root")
    tokens = {
        "admin": admin_token,
        "editor": auth.create_api_token(ed, "edagent")[0],
        "viewer": auth.create_api_token(vi, "viagent")[0],
    }

    # secret_ds is visible to root only.
    boot.put(
        "/api/v1/datasets/secret_ds/permissions",
        json={"grants": [
            Grant(subject_kind=SubjectKind.user, subject="root", can_view=True).model_dump(mode="json")
        ]},
    )

    def client_for(token: str) -> LaurelinClient:
        return LaurelinClient(token=token, http=TestClient(app))

    return app, client_for, tokens, boot


def test_an_editor_can_author_a_dashboard_and_a_schedule_over_mcp(env):
    _, client_for, tokens, _ = env
    c = client_for(tokens["editor"])

    dash = c.upsert_dashboard(
        "ops", title="Operations", panels=[SQL_PANEL, OBJECT_PANEL]
    )
    assert dash["name"] == "ops"
    assert [p["id"] for p in dash["panels"]] == ["p1", "p2"]

    sched = c.upsert_schedule("nightly", trigger="cron", cron="0 6 * * *", action="build")
    assert sched["name"] == "nightly"
    assert sched["next_run_at"] is not None

    queued = c.run_schedule("nightly")
    assert queued["queued"] == "nightly"
    c.close()


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c: c.upsert_dashboard("d", panels=[SQL_PANEL]), id="upsert_dashboard"),
        pytest.param(lambda c: c.upsert_schedule("s", cron="0 6 * * *"), id="upsert_schedule"),
        pytest.param(lambda c: c.run_schedule("s"), id="run_schedule"),
    ],
)
def test_every_presentation_write_tool_refuses_a_viewer_token_with_403(env, call):
    _, client_for, tokens, _ = env
    c = client_for(tokens["viewer"])
    with pytest.raises(LaurelinError) as err:
        call(c)
    assert err.value.status == 403
    c.close()


def test_the_mcp_refusal_is_byte_for_byte_the_rest_detail_because_it_is_the_same_route(env):
    app, client_for, tokens, _ = env
    with pytest.raises(LaurelinError) as err:
        client_for(tokens["viewer"]).upsert_dashboard("d", panels=[SQL_PANEL])

    rest = TestClient(app).put(
        "/api/v1/dashboards/d",
        json={"title": "", "description": "", "panels": [SQL_PANEL]},
        headers={"Authorization": f"Bearer {tokens['viewer']}"},
    )
    assert rest.status_code == 403
    assert err.value.detail == rest.json()["detail"]


def test_a_viewer_reading_a_dashboard_over_mcp_never_sees_a_panels_query(env):
    _, client_for, tokens, _ = env
    client_for(tokens["editor"]).upsert_dashboard(
        "ops", title="Ops", panels=[SQL_PANEL, OBJECT_PANEL]
    )

    viewer = client_for(tokens["viewer"])
    for dash in (viewer.get_dashboard("ops"), *viewer.list_dashboards()):
        assert dash["name"] == "ops"
        for panel in dash["panels"]:
            # Presentation arrives; the query half is OMITTED, not blanked.
            assert panel["id"] and panel["title"]
            for operational in ("sql", "object_type", "metrics", "group_by", "flow"):
                assert operational not in panel

    # The author's own token still reads the full document back.
    editor_view = client_for(tokens["editor"]).get_dashboard("ops")
    assert editor_view["panels"][0]["sql"] == SQL_PANEL["sql"]


def test_running_a_panel_over_mcp_executes_as_the_caller_not_as_the_author(env):
    _, client_for, tokens, _ = env
    # root authors a panel over a dataset only root can view.
    client_for(tokens["admin"]).upsert_dashboard("locked", panels=[SECRET_PANEL])

    admin_run = client_for(tokens["admin"]).run_dashboard_panel("locked", "p3")
    assert admin_run["rows"] == [{"k": "classified"}]

    # The viewer runs the SAME stored panel and gets a refusal, not the rows —
    # and the refusal does not echo the SQL they were never given.
    with pytest.raises(LaurelinError) as err:
        client_for(tokens["viewer"]).run_dashboard_panel("locked", "p3")
    assert err.value.status == 400
    assert "SELECT" not in err.value.detail
    assert "classified" not in err.value.detail


def test_a_viewer_can_run_an_aggregate_panel_and_gets_results_without_the_instruction(env):
    _, client_for, tokens, _ = env
    client_for(tokens["editor"]).upsert_dashboard("ops", panels=[OBJECT_PANEL])

    result = client_for(tokens["viewer"]).run_dashboard_panel("ops", "p2")
    assert result["columns"] == ["cities"]
    assert result["row_count"] == 1
    assert result["rows"][0]["cities"] == 2


def test_presentation_writes_are_audited_to_the_token_user(env):
    _, client_for, tokens, boot = env
    c = client_for(tokens["editor"])
    c.upsert_dashboard("ops", panels=[SQL_PANEL])
    c.upsert_schedule("nightly", cron="0 6 * * *")
    c.run_schedule("nightly")

    events = boot.get("/api/v1/audit", params={"limit": 50}).json()
    by_kind = {e["action"]: e for e in events}
    for kind in ("dashboard_created", "schedule_created", "schedule_run_requested"):
        assert kind in by_kind, f"missing audit event {kind}"
        assert by_kind[kind]["actor"] == "ed"


def test_a_forged_token_gets_401_from_every_presentation_tool(env):
    _, client_for, _, _ = env
    bad = client_for("laurelin_forged_token")
    for call in (
        bad.list_dashboards,
        lambda: bad.get_dashboard("ops"),
        lambda: bad.upsert_dashboard("d", panels=[SQL_PANEL]),
        lambda: bad.run_dashboard_panel("ops", "p1"),
        lambda: bad.upsert_schedule("s", cron="0 6 * * *"),
        lambda: bad.run_schedule("s"),
    ):
        with pytest.raises(LaurelinError) as err:
            call()
        assert err.value.status == 401
    bad.close()


@pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="mcp package not installed (pip install laurelin[mcp])",
)
def test_the_presentation_tools_are_listed_and_callable_on_the_mcp_server(env):
    import asyncio
    import json

    _, client_for, tokens, _ = env
    from laurelin.mcp import build_server

    server = build_server(client_for(tokens["editor"]))
    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert {
        "list_dashboards", "get_dashboard", "upsert_dashboard",
        "run_dashboard_panel", "upsert_schedule", "run_schedule",
    } <= tools

    result = asyncio.run(server.call_tool(
        "upsert_dashboard",
        {"name": "ops", "title": "Ops", "panels": [OBJECT_PANEL]},
    ))
    content = result[0] if isinstance(result, tuple) else result
    text = content[0].text if isinstance(content, list) else str(content)
    assert json.loads(text)["name"] == "ops"

    run = asyncio.run(server.call_tool(
        "run_dashboard_panel", {"name": "ops", "panel_id": "p2"}
    ))
    content = run[0] if isinstance(run, tuple) else run
    text = content[0].text if isinstance(content, list) else str(content)
    assert json.loads(text)["rows"][0]["cities"] == 2
