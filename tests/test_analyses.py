"""Analyses: the multi-cell governed notebook.

The one genuinely new mechanism here is cell chaining, and the invariant it
must keep is stated once and tested below: every run or preview of a cell
executes exactly ONE SQL statement, compiled from the closed Flow IR with
every author value bound as a parameter, through one ``_execute_sql`` call as
the current caller — so the entire chain, including every upstream cell, is
computed under that one caller's ACL / row-level security / masking in a
single policy pass, and no cell's intermediate result ever exists outside
that statement. A cell structurally cannot show a viewer data the viewer's
own policy hides, even where the author's policy did not.

Everything else is the dashboard contract, inherited deliberately: R2 splits
a cell into a presentation half a viewer receives and an instruction half
they never do, and the viewer gets the ROWS from the run route instead.
"""

import json

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core import serialize
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import AnalysisCell, AnalysisInfo, Role

# One hostile vocabulary for the whole repo: a value that breaks one surface
# is automatically tried against the others.
from tests.test_flow_compile import HOSTILE

CREDS = {"username": "root", "password": "trustno1!"}


@pytest.fixture()
def ws(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="ana")
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    cat.write("orders", pa.table({
        "region": ["us", "us", "eu", "eu"],
        "status": ["ok", "returned", "ok", "ok"],
        "amount": [10.0, 99.0, 20.0, 5.0],
    }))
    return ws


@pytest.fixture()
def clients(ws):
    app = create_app(ws)
    admin = TestClient(app)
    assert admin.post("/api/v1/auth/setup", json=CREDS).status_code == 200
    assert admin.post("/api/v1/auth/login", json=CREDS).status_code == 200
    admin.post("/api/v1/users", json={
        "username": "vic", "password": "password123", "role": "viewer"})
    viewer = TestClient(app)
    viewer.post("/api/v1/auth/login", json={
        "username": "vic", "password": "password123"})
    return admin, viewer


def _client(app, username, password="password123"):
    c = TestClient(app)
    assert c.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    ).status_code == 200
    return c


#: Cell 1: orders minus returns — a plain shaping spine, no aggregate, so the
#: chain below actually exercises "a later cell reads an earlier cell's rows".
CELL_FILTER = {
    "title": "Orders minus returns",
    "flow": {
        "terminal": "s2",
        "nodes": [
            {"id": "s1", "kind": "source", "inputs": [],
             "params": {"dataset": "orders"}},
            {"id": "s2", "kind": "filter", "inputs": ["s1"], "params": {
                "predicate": {"t": "op", "op": "ne", "args": [
                    {"t": "col", "name": "status"},
                    {"t": "lit", "type": "string", "value": "returned"}]}}},
        ],
    },
}

#: Cell 2: revenue by region over cell 1's output — the chain.
CELL_AGG = {
    "title": "Revenue by region",
    "inputs": ["c1"],
    "chart": "bar", "x": "region", "y": ["total"],
    "flow": {
        "terminal": "a1",
        "nodes": [
            {"id": "a1", "kind": "aggregate", "inputs": ["cell:c1"], "params": {
                "group_by": ["region"],
                "aggs": [{"fn": "sum", "column": "amount", "as": "total"}]}},
        ],
    },
}


def _make_chain(editor, name="rev"):
    assert editor.put(f"/api/v1/analyses/{name}",
                      json={"title": "Revenue", "cells": []}).status_code == 200
    assert editor.post(f"/api/v1/analyses/{name}/cells",
                       json=CELL_FILTER).status_code == 200
    r = editor.post(f"/api/v1/analyses/{name}/cells", json=CELL_AGG)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# R2 — the viewer never receives instruction text
# ---------------------------------------------------------------------------

def test_a_viewer_sees_cell_rows_for_sql_they_never_received(clients):
    """The non-negotiable constraint: a viewer sees the analysis AND its data,
    without ever receiving the query. Both halves asserted, so this fails if
    R2 leaks ``sql`` back and also if it is "solved" with an empty box."""
    admin, viewer = clients
    assert admin.put("/api/v1/analyses/rev", json={"title": "Revenue", "cells": [
        {"id": "c1", "title": "Answer",
         "sql": "SELECT region, sum(amount) AS total FROM orders GROUP BY region"},
    ]}).status_code == 200

    ana = viewer.get("/api/v1/analyses/rev").json()
    cell = ana["cells"][0]
    assert "sql" not in cell, "a viewer received the cell's query text"

    r = viewer.post("/api/v1/analyses/rev/cells/c1/run", json={"max_rows": 100})
    assert r.status_code == 200, r.text
    assert {row["region"]: row["total"] for row in r.json()["rows"]} == {
        "eu": 25.0, "us": 109.0
    }


def test_a_shaping_cells_flow_inputs_and_top_are_withheld_from_a_viewer(clients):
    admin, viewer = clients
    _make_chain(admin)
    assert admin.put("/api/v1/analyses/rev/cells/c2",
                     json={**CELL_AGG, "top": 5}).status_code == 200
    cells = viewer.get("/api/v1/analyses/rev").json()["cells"]
    for cell in cells:
        assert "flow" not in cell
        assert "inputs" not in cell
        assert "top" not in cell
    # The editor still receives the whole record they could have written.
    whole = admin.get("/api/v1/analyses/rev").json()["cells"][1]
    assert whole["inputs"] == ["c1"] and whole["top"] == 5


def test_a_cells_viewer_projection_is_exactly_the_presentation_set():
    """Every added viewer field is a new disclosure to maintain forever; this
    pins the set to exactly what `DashboardPanel` discloses today. The
    structural audit in tests/test_audience.py covers the new Governed models
    automatically; this is the per-field statement of the same rule."""
    cell = AnalysisCell(id="c1", sql="SELECT 1", inputs=[])
    out = serialize.dump_as(
        AnalysisInfo(name="a", cells=[cell]), Role.viewer
    )["cells"][0]
    assert set(out) == {
        "id", "title", "chart", "x", "y", "series", "stacked", "width",
    }
    ana = serialize.dump_as(AnalysisInfo(name="a", cells=[cell]), Role.viewer)
    assert "created_by" not in ana and "next_cell" not in ana


def test_a_cell_error_does_not_echo_the_query_back_to_the_viewer(ws):
    """A syntax error in a SQL cell and a dropped column under a shaping cell:
    the viewer gets the `Failure` brief, the editor gets the sentence.
    Withholding the instruction on the read path while the error path hands it
    back would not be withholding it."""
    app = create_app(ws)
    admin = TestClient(app)
    assert admin.post("/api/v1/auth/setup", json=CREDS).status_code == 200
    assert admin.post("/api/v1/auth/login", json=CREDS).status_code == 200
    admin.post("/api/v1/users", json={
        "username": "vic", "password": "password123", "role": "viewer"})

    assert admin.put("/api/v1/analyses/bad", json={"cells": [
        {"id": "c1", "title": "broken",
         "sql": "SELECT no_such_column_S3KRET FROM orders"},
    ]}).status_code == 200
    _make_chain(admin, name="chain")
    # Break the chain from behind: replace the dataset so `amount` is gone.
    # The save-time check cannot see the future; the run-time refusal is the
    # one a viewer meets.
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    cat.write("orders", pa.table({"region": ["us"], "status": ["ok"],
                                  "amt": [1.0]}))

    viewer = _client(app, "vic")
    r = viewer.post("/api/v1/analyses/bad/cells/c1/run", json={})
    assert r.status_code == 400
    assert "S3KRET" not in r.text
    assert "LINE 1" not in r.text

    r = viewer.post("/api/v1/analyses/chain/cells/c2/run", json={})
    assert r.status_code == 400, r.text
    # The shaping refusal names the author's column and the live schema —
    # authoring text, for the editor only.
    assert "amount" not in r.text and "amt" not in r.text

    r = admin.post("/api/v1/analyses/chain/cells/c2/run", json={})
    assert r.status_code == 400
    assert "amount" in r.text  # the editor may read what did not run, and why


def test_a_refusal_for_an_unviewable_source_does_not_enumerate_its_columns(ws):
    """Sources are view-checked against the caller BEFORE compilation
    (`_compiled_flow`'s disclosure ordering): the compiler's own refusals
    enumerate a schema, and an editor may hear "you cannot read this" without
    being handed the columns of the thing they cannot read."""
    app = create_app(ws)
    admin = TestClient(app)
    assert admin.post("/api/v1/auth/setup", json=CREDS).status_code == 200
    assert admin.post("/api/v1/auth/login", json=CREDS).status_code == 200
    admin.post("/api/v1/users", json={
        "username": "ed", "password": "password123", "role": "editor"})
    assert admin.put("/api/v1/datasets/orders/permissions", json={
        "grants": [{"subject_kind": "user", "subject": "root",
                    "can_view": True, "can_edit": True}]}).status_code == 200

    ed = _client(app, "ed")
    r = ed.post("/api/v1/analyses/preview", json={
        "cells": [{"id": "c1", **CELL_FILTER}], "cell_id": "c1"})
    assert r.status_code == 400
    assert "cannot read 'orders'" in r.json()["detail"]
    for column in ("region", "status", "amount"):
        assert column not in r.json()["detail"]


# ---------------------------------------------------------------------------
# The chaining invariant — one policy pass, as the caller
# ---------------------------------------------------------------------------

def _two_viewers_app(ws):
    app = create_app(ws)
    admin = TestClient(app)
    assert admin.post("/api/v1/auth/setup", json=CREDS).status_code == 200
    assert admin.post("/api/v1/auth/login", json=CREDS).status_code == 200
    for name in ("eve", "uma"):
        assert admin.post("/api/v1/users", json={
            "username": name, "password": "password123", "role": "viewer",
        }).status_code in (200, 201)
    assert admin.put("/api/v1/datasets/orders/policy", json={
        "row_policy": {"column": "region", "rules": [
            {"subject_kind": "user", "subject": "eve", "values": ["eu"]},
            {"subject_kind": "user", "subject": "uma", "values": ["us"]},
        ]},
        "column_masks": [],
    }).status_code == 200
    return app, admin


def test_two_viewers_get_different_rows_from_the_same_cell_chain(ws):
    """Server-side execution of the WHOLE chain runs as the CALLER. A chain
    that ran any upstream cell with the author's privileges would be a
    privilege-escalation primitive shaped like a notebook."""
    app, admin = _two_viewers_app(ws)
    _make_chain(admin)

    def rows_for(username):
        c = _client(app, username)
        r = c.post("/api/v1/analyses/rev/cells/c2/run", json={})
        assert r.status_code == 200, r.text
        return {row["region"]: row["total"] for row in r.json()["rows"]}

    assert rows_for("eve") == {"eu": 25.0}
    assert rows_for("uma") == {"us": 10.0}


def test_a_chained_cell_cannot_show_a_viewer_rows_the_authors_policy_would_have_allowed(ws):
    """The test this design exists for. The author (root, unrestricted) built
    and can see the full aggregate; the viewer's aggregate must equal the
    aggregate over ONLY the viewer's rows — proof the upstream cell was
    computed under the viewer's policy inside the same single statement,
    rather than replayed from anything the author's run produced."""
    app, admin = _two_viewers_app(ws)
    _make_chain(admin)

    # The author's own run sees everything: both regions.
    r = admin.post("/api/v1/analyses/rev/cells/c2/run", json={})
    assert {row["region"]: row["total"] for row in r.json()["rows"]} == {
        "eu": 25.0, "us": 10.0
    }

    eve = _client(app, "eve")
    r = eve.post("/api/v1/analyses/rev/cells/c2/run", json={})
    assert r.status_code == 200, r.text
    rows = {row["region"]: row["total"] for row in r.json()["rows"]}
    # eu only — and 25.0 is sum over eve's non-returned eu rows, nothing more.
    assert rows == {"eu": 25.0}


def test_a_cell_over_a_dataset_the_viewer_cannot_view_fails_as_an_unknown_table(ws):
    app = create_app(ws)
    admin = TestClient(app)
    assert admin.post("/api/v1/auth/setup", json=CREDS).status_code == 200
    assert admin.post("/api/v1/auth/login", json=CREDS).status_code == 200
    admin.post("/api/v1/users", json={
        "username": "vic", "password": "password123", "role": "viewer"})
    _make_chain(admin)
    # A SQL cell over the same dataset: for the viewer it is not registered,
    # so it fails exactly as an unknown table would — no confirmation that
    # the dataset exists behind the ACL.
    assert admin.post("/api/v1/analyses/rev/cells", json={
        "sql": "SELECT region FROM orders",
    }).status_code == 200
    assert admin.put("/api/v1/datasets/orders/permissions", json={
        "grants": [{"subject_kind": "user", "subject": "root",
                    "can_view": True, "can_edit": True}]}).status_code == 200

    viewer = _client(app, "vic")
    for cell_id in ("c2", "c3"):
        r = viewer.post(f"/api/v1/analyses/rev/cells/{cell_id}/run", json={})
        assert r.status_code == 400, (cell_id, r.text)
        # The viewer's refusal must not name the dataset's columns or echo
        # the instruction; they get the laundered brief.
        assert "amount" not in r.text
        assert "SELECT" not in r.text


def test_no_cell_result_is_ever_persisted(ws):
    """Two viewers get different rows from the same stored instruction, so a
    cached result would be a silent row-level-security bypass. After the
    author runs the chain, the stored record contains instructions only."""
    app, admin = _two_viewers_app(ws)
    _make_chain(admin)
    assert admin.post("/api/v1/analyses/rev/cells/c2/run",
                      json={}).status_code == 200

    store = MetadataStore(ws.metadata_path)
    with store._conn() as c:
        row = c.execute("SELECT cells_json FROM analyses WHERE name = 'rev'"
                        ).fetchone()
    cells = json.loads(row["cells_json"])
    text = row["cells_json"]
    assert "rows" not in {k for cell in cells for k in cell}
    # The author's visible aggregates (25.0 / 10.0) appear nowhere on disk.
    assert "25.0" not in text and "10.0" not in text

    # And a viewer's first run recomputes under their own policy.
    eve = _client(app, "eve")
    r = eve.post("/api/v1/analyses/rev/cells/c2/run", json={})
    assert {row["region"] for row in r.json()["rows"]} == {"eu"}


# ---------------------------------------------------------------------------
# Hostile values and the closed IR
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", HOSTILE, ids=repr)
def test_a_hostile_value_in_any_cell_filter_never_appears_in_the_compiled_sql(
    ws, value
):
    """A hostile filter value in an upstream cell travels as a bound parameter
    through the synthesized chain: the downstream preview and run return 200
    with it treated as data (matching nothing), never as syntax, and no
    response echoes compiled SQL or driver prose."""
    client = TestClient(create_app(ws, no_auth=True))
    hostile_filter = json.loads(json.dumps(CELL_FILTER))
    hostile_filter["flow"]["nodes"][1]["params"]["predicate"] = {
        "t": "op", "op": "eq", "args": [
            {"t": "col", "name": "region"},
            {"t": "lit", "type": "string", "value": value}]}

    r = client.post("/api/v1/analyses/preview", json={
        "cells": [{"id": "c1", **hostile_filter}, {"id": "c2", **CELL_AGG}],
        "cell_id": "c2",
    })
    assert r.status_code == 200, (value, r.text[:200])
    assert r.json()["row_count"] == 0  # bound and compared, matching nothing

    assert client.put("/api/v1/analyses/h", json={
        "cells": [{"id": "c1", **hostile_filter}, {"id": "c2", **CELL_AGG}],
    }).status_code == 200, value
    rr = client.post("/api/v1/analyses/h/cells/c2/run", json={})
    assert rr.status_code == 200, (value, rr.text[:200])
    assert rr.json()["row_count"] == 0

    for body in (r.text, rr.text):
        assert "_f0" not in body      # no compiled CTE
        assert "WITH " not in body    # no statement echo


def test_a_sql_cell_cannot_be_referenced_by_a_shaping_cell_and_vice_versa(clients):
    admin, _viewer = clients
    assert admin.put("/api/v1/analyses/mix", json={"cells": [
        {"id": "c1", "title": "sql", "sql": "SELECT 1 AS n"},
    ]}).status_code == 200

    r = admin.post("/api/v1/analyses/mix/cells", json={
        "inputs": ["c1"],
        "flow": {"terminal": "a1", "nodes": [
            {"id": "a1", "kind": "aggregate", "inputs": ["cell:c1"], "params": {
                "group_by": [],
                "aggs": [{"fn": "count_star", "column": None, "as": "n"}]}}]},
    })
    assert r.status_code == 400
    assert "SQL cell" in r.json()["detail"]  # a first-party sentence

    # And the other direction is unrepresentable: a SQL cell with inputs is
    # refused by the model itself.
    r = admin.post("/api/v1/analyses/mix/cells",
                   json={"sql": "SELECT 2", "inputs": ["c1"]})
    assert r.status_code == 400
    assert "cannot take input" in r.json()["detail"]


def test_deleting_a_cell_with_dependents_is_refused_naming_the_dependents(clients):
    admin, _viewer = clients
    _make_chain(admin)
    r = admin.delete("/api/v1/analyses/rev/cells/c1")
    assert r.status_code == 400
    assert "Revenue by region" in r.json()["detail"]
    # Repoint-then-delete works: drop the dependent first.
    assert admin.delete("/api/v1/analyses/rev/cells/c2").status_code == 200
    assert admin.delete("/api/v1/analyses/rev/cells/c1").status_code == 200


def test_a_cycle_between_cells_is_refused_with_a_sentence(clients):
    admin, _viewer = clients
    _make_chain(admin)
    # Try to point c1 at c2, closing the loop.
    cyclic = json.loads(json.dumps(CELL_FILTER))
    cyclic["inputs"] = ["c2"]
    cyclic["flow"]["nodes"] = [
        {"id": "s2", "kind": "filter", "inputs": ["cell:c2"], "params":
            cyclic["flow"]["nodes"][1]["params"]},
    ]
    r = admin.put("/api/v1/analyses/rev/cells/c1", json=cyclic)
    assert r.status_code == 400
    assert "loop" in r.json()["detail"]


def test_a_round_tripped_viewer_projection_does_not_blank_stored_cells(clients):
    """Absence is "unchanged", never "blank it": a client re-PUTting cells it
    fetched as a projection (operational keys omitted) must not erase the
    instructions it was never shown."""
    admin, _viewer = clients
    _make_chain(admin)
    stripped = [
        {"id": "c1", "title": "Orders minus returns", "chart": "table"},
        {"id": "c2", "title": "Revenue v2", "chart": "bar"},
    ]
    r = admin.put("/api/v1/analyses/rev",
                  json={"title": "Revenue", "cells": stripped})
    assert r.status_code == 200, r.text
    cells = {c["id"]: c for c in admin.get("/api/v1/analyses/rev").json()["cells"]}
    assert cells["c1"]["flow"] == CELL_FILTER["flow"], "c1's flow was blanked"
    assert cells["c2"]["inputs"] == ["c1"]
    assert cells["c2"]["title"] == "Revenue v2"
    # The chain still runs.
    assert admin.post("/api/v1/analyses/rev/cells/c2/run",
                      json={}).status_code == 200


def test_a_reorder_put_of_id_only_cells_preserves_titles_and_chart_bindings(clients):
    """Absence means "unchanged" for BOTH halves of a cell, not just the
    secret one. A reorder is naturally sent as `[{"id":"c2"},{"id":"c1"}]` —
    the round-tripped record minus what the client didn't touch — and before
    this rule covered presentation fields, that PUT kept every cell's
    sql/flow/inputs but silently reset titles to auto-labels and chart
    bindings to defaults, and later refusals then named cells by the blanked
    title."""
    admin, _viewer = clients
    _make_chain(admin)
    r = admin.put("/api/v1/analyses/rev",
                  json={"title": "Revenue", "cells": [{"id": "c2"}, {"id": "c1"}]})
    assert r.status_code == 200, r.text
    cells = {c["id"]: c for c in r.json()["cells"]}
    assert [c["id"] for c in r.json()["cells"]] == ["c2", "c1"]  # the reorder
    assert cells["c1"]["title"] == "Orders minus returns"
    assert cells["c2"]["title"] == "Revenue by region"
    assert cells["c2"]["chart"] == "bar"
    assert cells["c2"]["x"] == "region" and cells["c2"]["y"] == ["total"]
    # The secret half survived too, and the chain still runs.
    assert cells["c1"]["flow"] == CELL_FILTER["flow"]
    assert cells["c2"]["inputs"] == ["c1"]
    assert admin.post("/api/v1/analyses/rev/cells/c2/run",
                      json={}).status_code == 200

    # Same contract on the per-cell route: a title-only update keeps the
    # cell's flow, inputs and chart bindings.
    r = admin.put("/api/v1/analyses/rev/cells/c2", json={"title": "Renamed"})
    assert r.status_code == 200, r.text
    cell = next(c for c in r.json()["cells"] if c["id"] == "c2")
    assert cell["title"] == "Renamed"
    assert cell["chart"] == "bar" and cell["inputs"] == ["c1"]


def test_a_save_refusal_names_the_cell_and_card_not_a_namespaced_step_id(clients):
    """The closure namespaces steps as `{cell_id}_{step}` before the compiler
    sees them, so Tier A/B sentences would name ids the analyst never authored
    ("Step 'c2_a1' refers to…") and never say which cell to fix. The server
    rewrites them into the vocabulary the product speaks — for every consumer,
    not just the bundled UI's client-side map."""
    admin, _viewer = clients
    _make_chain(admin)
    # Break c1 under its dependent: rename `amount` away.
    broken = json.loads(json.dumps(CELL_FILTER))
    broken["flow"]["terminal"] = "s3"
    broken["flow"]["nodes"].append(
        {"id": "s3", "kind": "rename", "inputs": ["s2"],
         "params": {"pairs": [{"from": "amount", "to": "amt"}]}})
    r = admin.put("/api/v1/analyses/rev/cells/c1", json=broken)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "Cell 2 ('Revenue by region')'s Summarise card" in detail
    assert "c2_a1" not in detail and "Step '" not in detail

    # Malformed node params get the same treatment.
    bad = json.loads(json.dumps(CELL_FILTER))
    bad["flow"]["terminal"] = "s3"
    bad["flow"]["nodes"].append(
        {"id": "s3", "kind": "rename", "inputs": ["s2"],
         "params": {"pairs": "nope"}})
    r = admin.put("/api/v1/analyses/rev/cells/c1", json=bad)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "Cell 1 ('Orders minus returns')" in detail
    assert "c1_s3" not in detail

    # The rewriter reads unvalidated author input (the refusal being
    # rewritten may be the one refusing it): an unhashable `kind` must stay a
    # clean 400, not become a TypeError on the card lookup.
    hostile = json.loads(json.dumps(CELL_FILTER))
    hostile["flow"]["nodes"][1]["kind"] = ["evil"]
    r = admin.put("/api/v1/analyses/rev/cells/c1", json=hostile)
    assert r.status_code == 400
    assert "Unknown step kind" in r.json()["detail"]


def test_a_run_time_refusal_reaches_the_editor_in_cell_vocabulary(ws):
    """When the world changes under a stored chain, the editor's copy of the
    run-time refusal speaks cells and cards, not synthesized step ids. (The
    viewer's non-echoing `Failure` brief is asserted by
    test_a_cell_error_does_not_echo_the_query_back_to_the_viewer.)"""
    app = create_app(ws)
    admin = TestClient(app)
    assert admin.post("/api/v1/auth/setup", json=CREDS).status_code == 200
    assert admin.post("/api/v1/auth/login", json=CREDS).status_code == 200
    _make_chain(admin, name="chain")
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    cat.write("orders", pa.table({"region": ["us"], "status": ["ok"],
                                  "amt": [1.0]}))
    r = admin.post("/api/v1/analyses/chain/cells/c2/run", json={})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "Summarise card" in detail and "amount" in detail
    assert "c2_a1" not in detail


def test_a_self_referencing_cell_is_refused_with_a_sentence_about_itself(clients):
    """A one-cell loop gets its own sentence — "takes its own output as an
    input" — not the plural "these cells feed each other", which reads as if
    some other cell were involved."""
    admin, _viewer = clients
    selfref = {
        "id": "c1", "title": "Self", "inputs": ["c1"],
        "flow": {"terminal": "t", "nodes": [
            {"id": "t", "kind": "aggregate", "inputs": ["cell:c1"], "params": {
                "group_by": [],
                "aggs": [{"fn": "count_star", "column": None, "as": "n"}]}}]},
    }
    r = admin.post("/api/v1/analyses/preview",
                   json={"cells": [selfref], "cell_id": "c1"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "its own output" in detail
    assert "feed each other" not in detail


# ---------------------------------------------------------------------------
# Preview is resource-limited and not an oracle
# ---------------------------------------------------------------------------

@pytest.fixture()
def big_ws(tmp_path):
    ws = Workspace.init(tmp_path / "big", name="big")
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    n = 500
    cat.write("orders", pa.table({
        "region": ["us"] * n,
        "status": ["ok"] * n,
        "amount": [1.0] * n,
    }))
    return ws


def test_a_cell_preview_is_capped_at_flow_preview_max_rows(big_ws):
    """200 rows (`FLOW_PREVIEW_MAX_ROWS`), with `truncated` reported honestly
    — the limit binds as max_rows + 1 so the flag can actually fire."""
    client = TestClient(create_app(big_ws, no_auth=True))
    r = client.post("/api/v1/analyses/preview", json={
        "cells": [{"id": "c1", **CELL_FILTER}],
        "cell_id": "c1", "max_rows": 100_000,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["max_rows"] == 200
    assert body["row_count"] == 200
    assert body["truncated"] is True


def test_a_preview_limit_binds_at_the_previewed_terminal_only(big_ws):
    """Previewing the upstream cell at 200 rows must not change the
    downstream aggregate: the LIMIT binds at whichever cell is being
    previewed, never inside the chain. An aggregate over a limited input is a
    different (wrong) answer."""
    client = TestClient(create_app(big_ws, no_auth=True))
    cells = [{"id": "c1", **CELL_FILTER}, {"id": "c2", **CELL_AGG}]
    up = client.post("/api/v1/analyses/preview",
                     json={"cells": cells, "cell_id": "c1", "max_rows": 200})
    assert up.json()["truncated"] is True  # the upstream really is larger
    down = client.post("/api/v1/analyses/preview",
                       json={"cells": cells, "cell_id": "c2", "max_rows": 200})
    assert down.status_code == 200, down.text
    assert down.json()["rows"] == [{"region": "us", "total": 500.0}]


def test_a_masked_column_renders_masked_in_a_cell_preview_and_is_listed_in_masked_columns(ws):
    app = create_app(ws)
    admin = TestClient(app)
    assert admin.post("/api/v1/auth/setup", json=CREDS).status_code == 200
    assert admin.post("/api/v1/auth/login", json=CREDS).status_code == 200
    admin.post("/api/v1/users", json={
        "username": "ed", "password": "password123", "role": "editor"})
    assert admin.put("/api/v1/datasets/orders/policy", json={
        "row_policy": None,
        "column_masks": [{"column": "status", "mode": "redact"}],
    }).status_code == 200

    ed = _client(app, "ed")
    r = ed.post("/api/v1/analyses/preview", json={
        "cells": [{"id": "c1", **CELL_FILTER}], "cell_id": "c1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["masked_columns"] == {"orders": ["status"]}
    statuses = {row["status"] for row in body["rows"]}
    assert statuses == {"***"}, "the mask must render in the preview"
