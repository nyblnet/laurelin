"""Tests for the MCP layer: the REST client (SDK) and the MCP server tools.

The client is exercised over httpx.ASGITransport against a real app, using a
real API token — proving the "agents go through the normal permission + audit
path" property rather than assuming it.
"""

import importlib.util

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Grant, SubjectKind
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
actions:
  - api_name: adjust_pop
    object_type: city
    kind: update
    parameters:
      pop: {type: integer, required: true}
"""


@pytest.fixture()
def env(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="mcp")
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

    boot.post("/api/v1/users", json={"username": "ed", "password": "password123", "role": "editor"})
    ed = TestClient(app)
    ed.post("/api/v1/auth/login", json={"username": "ed", "password": "password123"})
    editor_token = ed.post("/api/v1/tokens", json={"name": "edagent"}).json()["token"]

    # Lock secret_ds to admin only (grant to nobody but root).
    boot.put(
        "/api/v1/datasets/secret_ds/permissions",
        json={"grants": [
            Grant(subject_kind=SubjectKind.user, subject="root", can_view=True).model_dump(mode="json")
        ]},
    )

    def client_for(token: str) -> LaurelinClient:
        # TestClient is an httpx.Client, so the SDK runs against the real app
        # over the real HTTP surface (cookies unused — Bearer token only).
        return LaurelinClient(token=token, http=TestClient(app))

    return app, client_for, admin_token, editor_token


def test_client_end_to_end(env):
    _, client_for, admin_token, _ = env
    c = client_for(admin_token)

    names = {d["name"] for d in c.list_datasets()}
    assert {"cities", "secret_ds"} <= names

    schema = {s["name"]: s["type"] for s in c.dataset_schema("cities")}
    assert schema["pop"] == "int64"

    q = c.query("SELECT sum(pop) AS total FROM cities")
    assert q["rows"][0]["total"] == 460

    types = [t["api_name"] for t in c.list_object_types()]
    assert types == ["city"]
    found = c.search_objects("city", search="tir")
    assert [o["name"] for o in found["objects"]] == ["tirion"]

    edit = c.apply_action("adjust_pop", pk="valmar", parameters={"pop": 150})
    assert edit["kind"] == "update"
    assert c.get_object("city", "valmar")["pop"] == 150

    # No pipelines in this workspace, so a build over "all targets" refuses
    # instead of manufacturing a green vacuous success. The agent gets the
    # same sentence a person does.
    with pytest.raises(LaurelinError) as build_err:
        c.run_build(wait=True)
    assert "no pipelines in this workspace" in str(build_err.value)
    c.close()


def test_client_is_subject_to_acls_and_auth(env):
    app, client_for, _, editor_token = env

    # The editor token cannot see or query the locked dataset.
    c = client_for(editor_token)
    assert "secret_ds" not in {d["name"] for d in c.list_datasets()}
    with pytest.raises(LaurelinError) as err:
        c.dataset_rows("secret_ds")
    # 404, not 403: a 403 confirms the dataset exists, which is an existence
    # oracle one URL over from a list that deliberately omits it. The SQL path
    # below has always answered "unknown table"; _require_dataset_view now
    # matches it, because the withholding boundary is only real if every
    # surface honours it.
    assert err.value.status == 404
    with pytest.raises(LaurelinError):
        c.query("SELECT * FROM secret_ds")  # unknown table -> 400
    c.close()

    # A bad token is a 401, not a silent fallback.
    bad = client_for("laurelin_forged_token")
    with pytest.raises(LaurelinError) as err:
        bad.list_datasets()
    assert err.value.status == 401
    bad.close()


@pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="mcp package not installed (pip install laurelin[mcp])",
)
def test_mcp_server_tools(env):
    import asyncio
    import json

    _, client_for, admin_token, _ = env
    from laurelin.mcp import build_server

    server = build_server(client_for(admin_token))

    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert {
        "list_datasets", "dataset_schema", "dataset_rows", "query_sql",
        "list_object_types", "get_object_type", "search_objects", "get_object",
        "get_linked_objects", "list_actions", "apply_action",
        "list_transforms", "get_lineage", "run_build", "get_build",
        "list_sources", "sync_source",
    } <= tools

    result = asyncio.run(server.call_tool("query_sql", {"sql": "SELECT count(*) AS n FROM cities"}))
    content = result[0] if isinstance(result, tuple) else result
    text = content[0].text if isinstance(content, list) else str(content)
    assert json.loads(text)["rows"][0]["n"] == 2
