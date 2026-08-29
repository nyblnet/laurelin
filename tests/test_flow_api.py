"""The /flows routes: the public shape the UI is built against.

Also the tests that prove a flow is a *transform* rather than a parallel
system: it collides with a Python pipeline over the same output and it appears
in `GET /transforms`. Authoring locks are split: `--lock-pipelines` locks code
(Python files, and eject — which writes one), `--lock-flows` locks no-code
flow authoring; the lock tests at the bottom pin the split.
"""

from __future__ import annotations

import json

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.approvals import ChangeTicket as _ChangeTicket
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore

_TICKET = _ChangeTicket(kind="local", actor="test")

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
def workspace(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="flows")
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    cat.write("orders", pa.table({
        "region": ["us", "us", "eu", "eu"],
        "status": ["ok", "returned", "ok", "ok"],
        "amount": [10, 99, 20, 5],
    }))
    return ws


@pytest.fixture()
def client(workspace):
    return TestClient(create_app(workspace, no_auth=True))


def put(client, name="busy_regions", flow=None):
    return client.put(f"/api/v1/flows/{name}", json={"flow": flow or FLOW})


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_a_saved_flow_can_be_listed_read_and_deleted(client, workspace):
    assert client.get("/api/v1/flows").json() == []

    r = put(client)
    assert r.status_code == 200, r.text
    assert r.json()["schema"] == ["region", "total"]

    listed = client.get("/api/v1/flows").json()
    assert listed == [{
        "name": "busy_regions", "output": "busy_regions",
        "sources": ["orders"], "nodes": 4, "author": "anonymous",
        "description": "", "failed": False,
    }]

    read = client.get("/api/v1/flows/busy_regions").json()
    assert read["error"] is None
    assert read["flow"]["terminal"] == "n3"

    # It is a file in pipelines/, not a row in metadata.db — which is how it
    # inherits export, import, credential scanning and the acknowledgement gate.
    assert (workspace.pipelines_dir / "busy_regions.flow.json").exists()

    assert client.delete("/api/v1/flows/busy_regions").json()["lineage_retained"] is True
    assert client.get("/api/v1/flows").json() == []


def test_the_author_is_recorded_by_the_server_and_not_taken_from_the_body(client):
    """The recorded author is whose read access the *build* is checked against.

    A client that could set it could pick whose rights to borrow.
    """
    put(client, flow=FLOW | {"author": "someone_else"})
    # `anonymous` is what --no-auth names the implicit user; the point is that
    # it is the *server's* idea of the caller, not the body's.
    assert client.get("/api/v1/flows/busy_regions").json()["flow"]["author"] == "anonymous"


def test_a_flow_that_does_not_validate_is_refused_and_never_written(client, workspace):
    bad = json.loads(json.dumps(FLOW))
    bad["nodes"][1]["params"]["predicate"]["args"][0]["name"] = "no_such_column"

    r = put(client, flow=bad)
    assert r.status_code == 400
    assert "no_such_column" in r.json()["detail"]
    assert not (workspace.pipelines_dir / "busy_regions.flow.json").exists()


def test_a_refusal_names_the_step_and_the_column_and_echoes_no_sql(client):
    bad = json.loads(json.dumps(FLOW))
    bad["nodes"][1]["params"]["predicate"]["args"][0]["name"] = "typo"
    detail = put(client, flow=bad).json()["detail"]

    assert "typo" in detail and "n1" in detail
    assert "status" in detail          # tells them what IS available
    assert "_f0" not in detail         # …but no compiled SQL
    assert "Binder Error" not in detail


def test_reading_a_missing_flow_is_a_404(client):
    assert client.get("/api/v1/flows/nope").status_code == 404
    assert client.delete("/api/v1/flows/nope").status_code == 404


# ---------------------------------------------------------------------------
# One build path
# ---------------------------------------------------------------------------


def test_a_flow_appears_in_the_transform_graph_alongside_python_pipelines(client):
    put(client)
    transforms = client.get("/api/v1/transforms").json()
    ours = [t for t in transforms if t["name"] == "busy_regions"]
    assert len(ours) == 1
    assert ours[0]["kind"] == "flow"
    assert ours[0]["inputs"] == ["orders"]


def test_a_flow_and_a_python_pipeline_producing_the_same_dataset_are_refused_as_duplicate_producers(
    client, workspace
):
    """One registry, so `TransformRegistry.register` catches this for free.

    This is the test that proves flows compile onto the existing build path
    rather than beside it: if they were a second system, two producers of one
    dataset would be two systems each quietly succeeding.
    """
    put(client)
    (workspace.pipelines_dir / "clash.py").write_text(
        "from laurelin.transforms import sql_transform, Input, Output\n\n"
        "@sql_transform(output=Output('busy_regions'),\n"
        "               inputs={'orders': Input('orders')},\n"
        "               query='SELECT * FROM orders')\n"
        "def clash():\n    ...\n"
    )
    r = client.get("/api/v1/transforms")
    assert r.status_code == 409
    assert "busy_regions" in r.json()["detail"] or "clash" in r.json()["detail"]


def _python_transform(workspace, stem: str, name: str, output: str) -> None:
    (workspace.pipelines_dir / f"{stem}.py").write_text(
        "from laurelin.transforms import sql_transform, Input, Output\n\n"
        f"@sql_transform(output=Output('{output}'),\n"
        "               inputs={'orders': Input('orders')},\n"
        "               query='SELECT * FROM orders')\n"
        f"def {name}():\n    ...\n"
    )


def test_saving_a_flow_named_after_an_existing_python_transform_is_a_409_not_a_workspace_outage(
    client, workspace
):
    """The pre-save guard used to compare names only, so a flow named exactly
    like a Python transform was mistaken for 'the flow being updated', saved,
    and the next `collect_transforms` raised `Duplicate transform name` —
    409ing every graph-touching route in the workspace. The guard must check
    the producer's *kind*: only a flow may be replaced by a flow."""
    _python_transform(workspace, "py_made", "py_made", "py_made")
    flow = {
        "name": "py_made", "output": "py_made", "terminal": "n0",
        "nodes": [{"id": "n0", "kind": "source", "inputs": [],
                   "params": {"dataset": "orders"}}],
    }
    r = put(client, name="py_made", flow=flow)
    assert r.status_code == 409, r.text
    assert "code transform" in r.json()["detail"]
    assert not (workspace.pipelines_dir / "py_made.flow.json").exists()
    # The workspace graph never broke: every graph-touching route still works.
    assert client.get("/api/v1/transforms").status_code == 200


def test_a_flow_sharing_only_a_name_with_a_python_transform_is_also_refused_before_saving(
    client, workspace
):
    """Registry uniqueness covers names as well as outputs. A Python transform
    named 'twin' producing some *other* dataset passes the output-producer
    lookup, but saving a flow named 'twin' would still brick collection with
    `Duplicate transform name`."""
    _python_transform(workspace, "twin", "twin", "other_ds")
    flow = {
        "name": "twin", "output": "twin", "terminal": "n0",
        "nodes": [{"id": "n0", "kind": "source", "inputs": [],
                   "params": {"dataset": "orders"}}],
    }
    r = put(client, name="twin", flow=flow)
    assert r.status_code == 409, r.text
    assert not (workspace.pipelines_dir / "twin.flow.json").exists()
    assert client.get("/api/v1/transforms").status_code == 200


def test_a_flow_builds_and_writes_the_dataset_it_names(client):
    put(client)
    r = client.post("/api/v1/builds", json={"targets": ["busy_regions"], "wait": True})
    assert r.status_code in (200, 201), r.text
    assert r.json()["status"] == "succeeded", r.json()

    rows = client.get("/api/v1/datasets/busy_regions/rows").json()["rows"]
    # `total` arrives as a STRING: DuckDB widens sum(BIGINT) to INT128, which
    # lands in Parquet as decimal128 and is JSON-serialised as text rather than
    # silently losing precision. Not flow-specific — an equivalent
    # @sql_transform does the same — but the UI has to render it, so it is
    # pinned here rather than discovered in the grid.
    assert rows == [{"region": "eu", "total": "25"}, {"region": "us", "total": "10"}]


def test_a_broken_flow_file_makes_the_registry_409_rather_than_vanish(
    client, workspace
):
    """Fail loud, like a `.py` that will not import.

    Skipping it instead would make the dataset it produces silently disappear
    from the plan — and "the build stopped producing this table and nothing
    said so" is the failure this codebase is least able to notice.
    """
    put(client)
    (workspace.pipelines_dir / "busy_regions.flow.json").write_text("{ not json")
    r = client.get("/api/v1/transforms")
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def test_preview_runs_a_draft_that_was_never_saved(client):
    """Previewing an unsaved draft is the whole interaction; requiring a save
    first would make the canvas useless."""
    r = client.post("/api/v1/flows/preview", json={"flow": FLOW | {"name": "busy_regions"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows"] == [{"region": "eu", "total": 25},
                            {"region": "us", "total": 10}]
    assert body["schema"] == ["region", "total"]
    assert client.get("/api/v1/flows").json() == []  # nothing was written


def test_preview_can_stop_at_an_upstream_step(client):
    r = client.post("/api/v1/flows/preview", json={
        "flow": FLOW | {"name": "busy_regions"}, "node_id": "n1"})
    body = r.json()
    assert body["node_id"] == "n1"
    assert body["schema"] == ["region", "status", "amount"]
    assert len(body["rows"]) == 3  # the returned row is filtered out


def test_preview_row_count_is_capped_far_below_the_workbench(client):
    """A canvas that previews as you build competes for the same process-wide
    admission semaphore as dashboards, object reads and row pages. Losing that
    race means `QueryRejected` for everyone else on the replica."""
    r = client.post("/api/v1/flows/preview", json={
        "flow": FLOW | {"name": "busy_regions"}, "max_rows": 100_000})
    assert r.json()["max_rows"] == 200


def test_preview_refuses_a_draft_that_does_not_compile(client):
    bad = json.loads(json.dumps(FLOW))
    bad["name"] = "busy_regions"
    bad["nodes"][2]["params"]["group_by"] = ["nope"]
    r = client.post("/api/v1/flows/preview", json={"flow": bad})
    assert r.status_code == 400
    assert "nope" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Read-only SQL, and eject
# ---------------------------------------------------------------------------


def test_the_compiled_sql_is_readable_but_the_parameter_values_are_not(client):
    """`params` is a count. The values are the author's data — a filter
    constant can be a customer name — and that is a different entitlement from
    reading the compiled shape."""
    put(client)
    body = client.get("/api/v1/flows/busy_regions/sql").json()
    assert body["sql"].startswith("WITH ")
    assert body["params"] == 1
    assert "returned" not in body["sql"]
    assert body["inputs"] == ["orders"]


def test_ejecting_a_flow_with_bound_values_carries_them_as_parameters(
    client, workspace
):
    """Ejecting a flow that filters on anything used to be impossible.

    `TransformSpec` had no `params` field, so `eject_sql` refused every flow
    carrying a single filter constant — on a feature whose defining operation is
    filtering. Measured: `source clean_flights -> filter status is not
    'cancelled'`, about as ordinary as a pipeline gets, returned
    `400 This flow uses filter or formula values…`, and the whole explanation
    lived in a `title` tooltip on a disabled menu item.

    The values stay bound. The generated file must contain a `params=` list and
    the query text must still hold `?`, never the value — interpolating it is
    the one thing this feature is built not to do.
    """
    put(client)
    r = client.post("/api/v1/flows/busy_regions/eject")
    assert r.status_code == 200, r.text

    source = (workspace.pipelines_dir / "busy_regions.py").read_text()
    assert "params=['returned']" in source
    assert "'returned'" not in source.split("params=")[0]
    assert "?" in source

    build = client.post("/api/v1/builds",
                        json={"targets": ["busy_regions"], "wait": True})
    assert build.json()["status"] == "succeeded", build.text
    rows = client.get("/api/v1/datasets/busy_regions/rows").json()["rows"]
    # `total` comes back as a string: DuckDB widens sum(int) to HUGEINT, which
    # has no JSON number that round-trips.
    assert [(r["region"], int(r["total"])) for r in rows] == [("eu", 25), ("us", 10)]


def test_ejecting_a_valueless_flow_writes_python_and_removes_the_flow(
    client, workspace
):
    plain = {
        "output": "plain", "terminal": "n1",
        "nodes": [
            {"id": "n0", "kind": "source", "inputs": [],
             "params": {"dataset": "orders"}},
            {"id": "n1", "kind": "aggregate", "inputs": ["n0"], "params": {
                "group_by": ["region"],
                "aggs": [{"fn": "count_star", "as": "n"}]}},
        ],
    }
    assert put(client, name="plain", flow=plain).status_code == 200

    r = client.post("/api/v1/flows/plain/eject")
    assert r.status_code == 200, r.text
    assert r.json()["ejected"] is True
    # Inputs come from the IR, not from a word-boundary regex over the SQL.
    assert r.json()["inputs"] == ["orders"]

    assert not (workspace.pipelines_dir / "plain.flow.json").exists()
    assert (workspace.pipelines_dir / "plain.py").exists()

    # One producer, still exactly one, and it still builds.
    assert [t["name"] for t in client.get("/api/v1/transforms").json()].count("plain") == 1
    build = client.post("/api/v1/builds", json={"targets": ["plain"], "wait": True})
    assert build.json()["status"] == "succeeded"


def test_eject_refuses_when_a_python_pipeline_of_that_name_already_exists(
    client, workspace
):
    """Two producers of one output dataset is a hard `register()` failure,
    which 409s every route that collects the registry — the whole workspace,
    not just this flow."""
    plain = {
        "output": "plain", "terminal": "n0",
        "nodes": [{"id": "n0", "kind": "source", "inputs": [],
                   "params": {"dataset": "orders"}}],
    }
    put(client, name="plain", flow=plain)
    (workspace.pipelines_dir / "plain.py").write_text("# placeholder\n")
    assert client.post("/api/v1/flows/plain/eject").status_code == 409


# ---------------------------------------------------------------------------
# Schema route
# ---------------------------------------------------------------------------


def test_the_column_picker_route_serves_a_datasets_columns_and_their_kinds(client):
    """`kinds` is what lets the form offer "Total of" only for columns that
    hold numbers. The type was always in `ColumnSchema.type`; the form offering
    `sum` over a column of names — and the flow then saving and failing its
    build — was a UI that had never been given it."""
    r = client.get("/api/v1/flows/schema", params={"dataset": "orders"})
    assert r.json() == {
        "dataset": "orders",
        "columns": ["region", "status", "amount"],
        "kinds": {"region": "text", "status": "text", "amount": "number"},
    }


def test_the_column_picker_route_404s_for_a_dataset_that_does_not_exist(client):
    assert client.get("/api/v1/flows/schema", params={"dataset": "nope"}).status_code == 404


# ---------------------------------------------------------------------------
# Hardening
# ---------------------------------------------------------------------------


def test_lock_pipelines_locks_code_not_flows_flow_writes_succeed_python_writes_and_eject_refuse(workspace):
    """`--lock-pipelines` locks *code execution*, which is what its name and its
    help text always said: a pipeline file is Python exec'd as the server
    process. A flow is not code — it compiles to parameterised SQL with every
    value bound and every identifier schema-checked, is refused if it would
    launder a mask, and is re-checked against its recorded author at every
    build. Locking both behind one flag left hardened deployments with no
    authoring path at all, so the flag went unset and every editor kept RCE —
    the safe production posture (Python locked, Flows/Explore open, eject
    locked) did not exist. Now it does. Eject stays locked because it is the
    escape hatch back into code. Operators who want the old total lockdown add
    `--lock-flows` (see the companion test below).
    """
    locked = TestClient(create_app(workspace, no_auth=True, lock_pipelines=True))

    # No-code authoring: available.
    assert put(locked).status_code == 200
    assert locked.get("/api/v1/flows").status_code == 200
    assert locked.get("/api/v1/flows/busy_regions").status_code == 200
    assert locked.post("/api/v1/flows/preview",
                       json={"flow": FLOW | {"name": "busy_regions"}}).status_code == 200

    # The escape hatch back into code: locked.
    assert locked.post("/api/v1/flows/busy_regions/eject").status_code == 403

    # Python authoring: locked.
    assert locked.put("/api/v1/pipelines/x", json={"content": "x = 1"}).status_code == 403
    assert locked.post("/api/v1/pipelines/from-query",
                       json={"sql": "SELECT 1", "output": "o"}).status_code == 403
    assert locked.delete("/api/v1/pipelines/x").status_code == 403

    # Flow delete is no-code too (and fail-closed on lineage — see the route).
    assert locked.delete("/api/v1/flows/busy_regions").status_code == 200

    # The posture is legible before the first 403: the boot probe carries it.
    status = locked.get("/api/v1/auth/status").json()
    assert status["authoring"] == {"pipelines_locked": True, "flows_locked": False}


def test_lock_flows_restores_the_total_authoring_lockdown(workspace):
    """Both flags together are exactly the old `--lock-pipelines` contract:
    no authoring of any kind on this server. The upgrade note in the CHANGELOG
    points hardened operators here."""
    open_client = TestClient(create_app(workspace, no_auth=True))
    put(open_client)

    locked = TestClient(create_app(
        workspace, no_auth=True, lock_pipelines=True, lock_flows=True,
    ))
    assert put(locked).status_code == 403
    assert locked.delete("/api/v1/flows/busy_regions").status_code == 403
    assert locked.post("/api/v1/flows/busy_regions/eject").status_code == 403
    assert locked.put("/api/v1/pipelines/x", json={"content": "x = 1"}).status_code == 403
    assert locked.delete("/api/v1/pipelines/x").status_code == 403

    # Reads and previews were never authoring and stay open under both flags.
    assert locked.get("/api/v1/flows").status_code == 200
    assert locked.get("/api/v1/flows/busy_regions").status_code == 200
    assert locked.post("/api/v1/flows/preview",
                       json={"flow": FLOW | {"name": "busy_regions"}}).status_code == 200

    status = locked.get("/api/v1/auth/status").json()
    assert status["authoring"] == {"pipelines_locked": True, "flows_locked": True}


def test_lock_flows_alone_locks_flow_writes_and_eject_but_not_python(workspace):
    """The flags are independent. `--lock-flows` without `--lock-pipelines` is
    an unusual posture (it trusts editors with code but not with clicks), but
    each flag must mean exactly its own sentence: eject refuses because it
    consumes a flow file, and Python authoring is untouched."""
    open_client = TestClient(create_app(workspace, no_auth=True))
    put(open_client)

    locked = TestClient(create_app(workspace, no_auth=True, lock_flows=True))
    r = put(locked)
    assert r.status_code == 403
    assert "--lock-flows" in r.json()["detail"]
    assert locked.delete("/api/v1/flows/busy_regions").status_code == 403
    assert locked.post("/api/v1/flows/busy_regions/eject").status_code == 403
    assert locked.put("/api/v1/pipelines/x", json={"content": "x = 1"}).status_code == 200


def test_every_flow_route_requires_an_editor(workspace):
    """"If you cannot write it, you cannot read it" — the rule already written
    on the pipeline routes. A viewer's legitimate need is lineage, which
    `GET /transforms` and `GET /lineage` serve structurally."""
    from laurelin.core.models import Role, User

    store = MetadataStore(workspace.metadata_path)
    store.create_user(User(id="9", username="vic", role=Role.viewer), _hash("pw"))

    app = create_app(workspace)
    viewer = TestClient(app)
    viewer.post("/api/v1/auth/login", json={"username": "vic", "password": "pw"})

    for method, path in (
        ("get", "/api/v1/flows"),
        ("get", "/api/v1/flows/busy_regions"),
        ("get", "/api/v1/flows/schema?dataset=orders"),
        ("put", "/api/v1/flows/busy_regions"),
        ("delete", "/api/v1/flows/busy_regions"),
        ("post", "/api/v1/flows/preview"),
        ("get", "/api/v1/flows/busy_regions/sql"),
        ("post", "/api/v1/flows/busy_regions/eject"),
    ):
        kwargs = {"json": {"flow": FLOW}} if method in ("put", "post") else {}
        r = getattr(viewer, method)(path, **kwargs)
        assert r.status_code in (401, 403), (method, path, r.status_code)


def _hash(password: str) -> str:
    from laurelin.core.auth import hash_password

    return hash_password(password)


def test_a_structurally_invalid_flow_can_still_be_opened_and_repaired(
    client, workspace
):
    """The builder must not be locked out of its own file.

    `read()` echoes back a flow that is parseable JSON but structurally invalid
    — a step referring to a deleted step, say — with the refusal alongside it,
    so the UI can render the canvas and let the author fix the one broken step.
    Re-validating it on the way out turned that repair path into a 500; this
    pins the 200.
    """
    put(client)
    broken = json.loads(json.dumps(FLOW))
    broken["nodes"][1]["inputs"] = ["deleted_step"]
    (workspace.pipelines_dir / "busy_regions.flow.json").write_text(
        json.dumps(broken)
    )

    r = client.get("/api/v1/flows/busy_regions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["error"] and "deleted_step" in body["error"]
    assert body["flow"]["terminal"] == "n3"      # echoed back for repair
    assert body["output_will_be_restricted"] is False

    # And the listing reports *that* it is broken without saying how.
    listed = client.get("/api/v1/flows").json()
    assert listed[0]["failed"] is True
    assert "deleted_step" not in json.dumps(listed)


def test_ejecting_does_not_report_the_collision_it_resolved_on_the_way_out(client):
    """`generate_sql_transform` re-collects the workspace as part of writing.

    At that instant the `.py` and the `.flow.json` both exist and both claim
    the same output dataset, so its `collect_error` is a duplicate-producer
    complaint about a collision this very request is halfway through resolving.
    Reporting it would send the author to fix something that is already gone.
    """
    plain = {
        "output": "plain", "terminal": "n0",
        "nodes": [{"id": "n0", "kind": "source", "inputs": [],
                   "params": {"dataset": "orders"}}],
    }
    put(client, name="plain", flow=plain)
    body = client.post("/api/v1/flows/plain/eject").json()
    assert body["collect_error"] is None, body["collect_error"]
    # …and the workspace really is clean afterwards.
    assert client.get("/api/v1/transforms").status_code == 200


# ---------------------------------------------------------------------------
# Regressions on the routes. Each was reproduced through the HTTP API first.
# ---------------------------------------------------------------------------


def test_a_preview_that_shows_only_part_of_the_result_says_so(client):
    """`truncated` was structurally impossible, so the count read as the answer.

    `compile_flow` appends the preview LIMIT at the terminal, and
    `catalog.query` independently fetches `max_rows + 1` and compares — with the
    same number in both places the comparison could never fire. Measured on this
    repo's own demo data: a 56-row dataset previewed as "50 rows · 7 columns"
    with no truncation marker, and a 5,000-group aggregate as "200 rows ·
    2 columns" with 4,800 groups silently missing.
    """
    body = {"flow": {**FLOW, "name": "busy_regions"}, "node_id": "n0",
            "max_rows": 2}
    r = client.post("/api/v1/flows/preview", json=body).json()
    assert r["row_count"] == 2
    assert r["truncated"] is True

    r = client.post("/api/v1/flows/preview",
                    json={**body, "max_rows": 50}).json()
    assert r["row_count"] == 4
    assert r["truncated"] is False


def test_a_preview_reports_the_kind_of_every_column_it_produces(client):
    r = client.post("/api/v1/flows/preview", json={
        "flow": {**FLOW, "name": "busy_regions"}, "max_rows": 10,
    }).json()
    assert r["kinds"] == {"region": "text", "total": "number"}


def test_a_type_mistake_is_explained_in_flow_language_not_as_a_remote_system(client):
    """`_execute_sql` classifies a DuckDB binder error as `column_missing`,
    whose sentence is "A column referenced does not exist…". Measured, that
    reached a flow author about a column visibly present in the picker below,
    on a statement that never left the process. A flow's execution errors get
    Laurelin's own sentence, written for someone who does not read SQL."""
    hostile_cast = {
        "name": "casted", "output": "casted", "terminal": "n1", "nodes": [
            {"id": "n0", "kind": "source", "inputs": [],
             "params": {"dataset": "orders"}},
            {"id": "n1", "kind": "cast", "inputs": ["n0"],
             "params": {"column": "status", "to": "bigint"}},
        ],
    }
    r = client.post("/api/v1/flows/preview",
                    json={"flow": hostile_cast, "max_rows": 10})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "remote" not in detail.lower()
    assert "'cast' step" in detail


def test_a_flow_whose_result_would_feed_itself_is_refused_at_save(client):
    """Saving one used to wedge every build in the workspace.

    `PUT` returned 200; the cycle was found later in `Builder.plan`, which runs
    for *every* build — so `POST /builds` with no targets, the scheduler's path,
    returned 400 and every unrelated transform stopped until somebody found and
    deleted the flow.
    """
    loop = {"output": "orders", "terminal": "n0", "nodes": [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}}]}
    r = client.put("/api/v1/flows/orders", json={"flow": loop})
    assert r.status_code == 400
    assert "itself" in r.json()["detail"]

    # And the workspace still builds.
    assert client.post("/api/v1/builds", json={"wait": True}).status_code == 200


def test_a_flow_definition_is_not_readable_by_someone_who_cannot_read_its_sources(
    workspace
):
    """`GET /flows/{name}/sql` withholds the bound values deliberately — "a
    filter constant can be a customer name … a different entitlement from
    reading the dataset", in its own docstring — and orders the source check
    before compilation for the same reason. Its two sibling read routes did
    neither, so the protection was decorative: measured, bob was 403 on the
    dataset's rows, 403 on its schema, 403 on `/flows/schema` and 400 on
    `/sql` — and `GET /flows/{name}` handed him the column name and the filter
    value in a 200."""
    from laurelin.core.models import Role, User

    store = MetadataStore(workspace.metadata_path)
    for name in ("alice", "bob"):
        store.create_user(User(id=name, username=name, role=Role.editor),
                          _hash("pw"))
    store.set_grants_for_dataset("orders", [{
        "subject_kind": "user", "subject": "alice",
        "can_view": True, "can_edit": True,
    }], ticket=_TICKET)

    app = create_app(workspace)
    alice, bob = TestClient(app), TestClient(app)
    alice.post("/api/v1/auth/login", json={"username": "alice", "password": "pw"})
    bob.post("/api/v1/auth/login", json={"username": "bob", "password": "pw"})

    secret_value = "CONFIDENTIAL-CUSTOMER-NAME"
    flow = {"output": "deals", "terminal": "n1", "nodes": [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
            "predicate": {"t": "op", "op": "eq", "args": [
                {"t": "col", "name": "status"},
                {"t": "lit", "type": "string", "value": secret_value}]}}}]}
    assert alice.put("/api/v1/flows/deals", json={"flow": flow}).status_code == 200

    assert bob.get("/api/v1/datasets/orders/rows").status_code == 403
    read = bob.get("/api/v1/flows/deals")
    assert read.status_code == 403
    assert secret_value not in read.text
    listed = bob.get("/api/v1/flows")
    assert listed.json() == []
    assert "orders" not in listed.text

    # Alice, who may read the source, is not obstructed.
    assert alice.get("/api/v1/flows/deals").status_code == 200
    assert len(alice.get("/api/v1/flows").json()) == 1


def test_a_date_filter_saves_instead_of_returning_error_500(client, workspace):
    """The most ordinary filter an analyst writes, and it lost their work.

    `_coerce_literal` returns a real `datetime.date`, `FlowDef.as_json()` passed
    it through, and `flow_files.write` handed it to `json.dumps` — which raises
    `TypeError`, not `FlowRefused`, so `_flow_or_400` did not catch it and the
    screen said "Error 500: Internal Server Error". `POST /flows/preview`
    returned 200 with correct rows for the same literal, so the step visibly
    worked and then Save destroyed the flow, naming no control.
    """
    cat = DatasetCatalog(workspace, MetadataStore(workspace.metadata_path))
    cat.write("events", pa.table({
        "happened": pa.array(["2026-01-01", "2026-03-01"]).cast("date32[day]"),
        "n": [1, 2],
    }))
    for lit_type, value in (("date", "2026-02-01"),
                            ("timestamp", "2026-02-01T00:00:00")):
        flow = {"output": "recent", "terminal": "n1", "nodes": [
            {"id": "n0", "kind": "source", "inputs": [],
             "params": {"dataset": "events"}},
            {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
                "predicate": {"t": "op", "op": "gte", "args": [
                    {"t": "col", "name": "happened"},
                    {"t": "lit", "type": lit_type, "value": value}]}}}]}
        r = client.put("/api/v1/flows/recent", json={"flow": flow})
        assert r.status_code == 200, (lit_type, r.text)
        # …and it round-trips through the file as ISO 8601, unchanged.
        stored = client.get("/api/v1/flows/recent").json()["flow"]
        assert stored["nodes"][1]["params"]["predicate"]["args"][1] == {
            "t": "lit", "type": lit_type, "value": value,
        }

    build = client.post("/api/v1/builds",
                        json={"targets": ["recent"], "wait": True})
    assert build.json()["status"] == "succeeded", build.text


def test_ejecting_does_not_launder_a_mask_the_flow_itself_is_refused_over(workspace):
    """Eject ran `check_flow_sources` alone, which made it a one-click downgrade
    out of every protection flows add.

    Measured, API-only, as a plain editor: author a flow; an admin later masks a
    column it reads. `PUT` then 400s and the build fails `FlowRefused` — and
    `POST /flows/{name}/eject` returned 200, wrote the `.py`, deleted the flow,
    and rebuilding that pipeline read the column in plaintext. One HTTP call
    moved a flow the platform refuses to build onto the path documented as
    laundering masks.
    """
    from laurelin.core.models import Role, User

    store = MetadataStore(workspace.metadata_path)
    store.create_user(User(id="a", username="ana", role=Role.editor), _hash("pw"))
    app = create_app(workspace)
    ana = TestClient(app)
    ana.post("/api/v1/auth/login", json={"username": "ana", "password": "pw"})

    copy = {"output": "orders_copy", "terminal": "n0", "nodes": [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}}]}
    assert ana.put("/api/v1/flows/orders_copy", json={"flow": copy}).status_code == 200

    store.set_dataset_policy("orders", {
        "column_masks": [{"column": "amount", "mode": "redact"}],
    }, ticket=_TICKET)
    assert ana.put("/api/v1/flows/orders_copy",
                   json={"flow": copy}).status_code == 400

    r = ana.post("/api/v1/flows/orders_copy/eject")
    assert r.status_code == 400
    assert "amount" in r.json()["detail"]
    assert not (workspace.pipelines_dir / "orders_copy.py").exists()
    assert ana.get("/api/v1/flows/orders_copy").status_code == 200


def test_ejecting_carries_the_author_restriction_onto_the_ejected_pipeline(workspace):
    """After eject there is no flow left for `Builder._execute_flow` to apply
    `restrict_output_to_author` for, so eject must apply it itself.

    Measured: a flow over a granted source promised `output_will_be_restricted:
    true`; ejecting it produced a dataset with `grants: []`, readable by an
    editor who was 403 on the source.
    """
    from laurelin.core.models import Role, User

    store = MetadataStore(workspace.metadata_path)
    for name in ("ana", "bob"):
        store.create_user(User(id=name, username=name, role=Role.editor),
                          _hash("pw"))
    store.set_grants_for_dataset("orders", [{
        "subject_kind": "user", "subject": "ana",
        "can_view": True, "can_edit": True,
    }], ticket=_TICKET)
    app = create_app(workspace)
    ana, bob = TestClient(app), TestClient(app)
    ana.post("/api/v1/auth/login", json={"username": "ana", "password": "pw"})
    bob.post("/api/v1/auth/login", json={"username": "bob", "password": "pw"})

    copy = {"output": "orders_copy", "terminal": "n0", "nodes": [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}}]}
    saved = ana.put("/api/v1/flows/orders_copy", json={"flow": copy})
    assert saved.json()["output_will_be_restricted"] is True

    ejected = ana.post("/api/v1/flows/orders_copy/eject")
    assert ejected.status_code == 200, ejected.text
    assert ejected.json()["output_restricted"] is True

    build = ana.post("/api/v1/builds",
                     json={"targets": ["orders_copy"], "wait": True})
    assert build.json()["status"] == "succeeded", build.text
    assert bob.get("/api/v1/datasets/orders_copy/rows").status_code == 403
    assert ana.get("/api/v1/datasets/orders_copy/rows").status_code == 200


def test_a_flow_over_a_federated_dataset_can_be_previewed(client, workspace, tmp_path):
    """Everything about a federated-backed flow worked except the one thing the
    feature promises.

    `catalog.query` skips registering a source-scanned dataset unless
    `LAURELIN_FEDERATION_WORKBENCH=1`, and preview goes through it while the
    build registers via `source_table`. Measured: `GET /flows/schema` listed the
    columns, `PUT` returned 200, `GET /flows/{name}/sql` returned 200, the build
    succeeded — and the preview alone answered
    `400 The table does not exist`, which is *false*, about a table the route
    immediately above had just described.

    A compiled flow is server-authored SQL over a closed IR — the case
    `federation.workbench_enabled`'s own docstring already carves out — so it is
    registered for a preview without opening the ad-hoc workbench.
    """
    import pyarrow.parquet as pq

    path = tmp_path / "events.parquet"
    pq.write_table(pa.table({"region": ["us", "eu"], "n": [1, 2]}), path)
    reg = client.put("/api/v1/datasets/events/federated", json={
        "source": {"type": "parquet", "path": str(path)},
    })
    assert reg.status_code == 200, reg.text

    flow = {"output": "fed_out", "terminal": "n0", "nodes": [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "events"}}]}
    assert client.put("/api/v1/flows/fed_out",
                      json={"flow": flow}).status_code == 200

    preview = client.post("/api/v1/flows/preview",
                          json={"flow": {**flow, "name": "fed_out"},
                                "max_rows": 10})
    assert preview.status_code == 200, preview.text
    assert preview.json()["rows"] == [
        {"region": "us", "n": 1}, {"region": "eu", "n": 2},
    ]

    # And the ad-hoc workbench is still shut: same dataset, ordinary SQL.
    adhoc = client.post("/api/v1/query", json={"sql": "SELECT * FROM events"})
    assert adhoc.status_code == 400
