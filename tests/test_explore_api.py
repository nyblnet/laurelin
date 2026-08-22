"""POST /explore/preview: the Explore screen's one route.

Explore has no representation of its own — the wire format IS a FlowDef, so
every safety property here is inherited from the Flow compiler rather than
re-proven: values bound, identifiers resolved by schema membership, sources
view-checked before the compiler can name a column. What this file pins is
that the inheritance is real (an Explore preview equals the equivalent flow
preview and the equivalent handwritten SQL, row for row) and the one
deliberate divergence: `check_flow_governance` is NOT run, because its
row-policy and referenced-mask refusals guard *materialization* and Explore
materializes nothing — every result is the caller's own policied view.
"""

from __future__ import annotations

import json

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore

# One hostile vocabulary for the whole repo: a value that breaks one surface
# is automatically tried against the others.
from tests.test_flow_compile import HOSTILE

#: The flow Explore's UI synthesizes for "orders, drop returned, total by
#: region" — source → filter → aggregate → sort, always named `explore`.
EXPLORE = {
    "name": "explore",
    "output": "explore",
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
    ws = Workspace.init(tmp_path / "ws", name="explore")
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


def preview(client, flow, max_rows=50):
    return client.post("/api/v1/explore/preview",
                       json={"flow": flow, "max_rows": max_rows})


def _hash(password: str) -> str:
    from laurelin.core.auth import hash_password

    return hash_password(password)


def _login(app, username, password="pw"):
    c = TestClient(app)
    assert c.post("/api/v1/auth/login",
                  json={"username": username, "password": password}).status_code == 200
    return c


# ---------------------------------------------------------------------------
# One compilation path: Explore == Flow == SQL, row for row
# ---------------------------------------------------------------------------


def test_an_explore_preview_equals_the_equivalent_flow_preview_row_for_row(client):
    """The same FlowDef through both routes. If these ever diverge, Explore has
    grown a second compiler — the mistake the whole feature spec forbids."""
    explore = preview(client, EXPLORE).json()
    flows = client.post("/api/v1/flows/preview", json={"flow": EXPLORE}).json()

    assert explore["rows"] == flows["rows"] == [
        {"region": "eu", "total": 25}, {"region": "us", "total": 10}]
    assert explore["schema"] == flows["schema"] == ["region", "total"]
    assert explore["kinds"] == flows["kinds"] == {
        "region": "text", "total": "number"}
    assert explore["truncated"] is False


def test_an_explore_preview_equals_handwritten_sql_over_the_same_dataset(client):
    """The Flow/SQL equivalence, through the API: the synthesized
    group/aggregate answers exactly what a hand-written GROUP BY answers."""
    explore = preview(client, EXPLORE).json()["rows"]
    sql = client.post("/api/v1/query", json={"sql": (
        "SELECT region, sum(amount) AS total FROM orders "
        "WHERE status IS DISTINCT FROM 'returned' "
        "GROUP BY region ORDER BY region"
    )}).json()["rows"]
    assert explore == sql


def test_the_new_vocabulary_is_reachable_through_explore(client):
    """`median` and the floor-bin histogram preset, end to end through the
    route — the two IR extensions this feature added."""
    flow = json.loads(json.dumps(EXPLORE))
    flow["nodes"][2]["params"]["aggs"] = [
        {"fn": "median", "column": "amount", "as": "mid"}]
    r = preview(client, flow)
    assert r.status_code == 200, r.text
    # `returned` rows are filtered first, so us keeps [10] and eu keeps [20, 5].
    assert r.json()["rows"] == [{"region": "eu", "mid": 12.5},
                                {"region": "us", "mid": 10.0}]

    hist = {
        "name": "explore", "output": "explore", "terminal": "n2", "nodes": [
            {"id": "n0", "kind": "source", "inputs": [],
             "params": {"dataset": "orders"}},
            {"id": "n1", "kind": "derive", "inputs": ["n0"], "params": {
                "name": "bin",
                "expr": {"t": "op", "op": "mul", "args": [
                    {"t": "op", "op": "floor", "args": [
                        {"t": "op", "op": "div", "args": [
                            {"t": "col", "name": "amount"},
                            {"t": "lit", "type": "bigint", "value": 50}]}]},
                    {"t": "lit", "type": "bigint", "value": 50}]}}},
            {"id": "n2", "kind": "aggregate", "inputs": ["n1"], "params": {
                "group_by": ["bin"],
                "aggs": [{"fn": "count_star", "as": "n"}]}},
        ],
    }
    r = preview(client, hist)
    assert r.status_code == 200, r.text
    assert {row["bin"]: row["n"] for row in r.json()["rows"]} == {0: 3, 50: 1}


def test_a_text_timestamp_column_buckets_by_month_through_the_cast_step_explore_synthesizes(
    client, workspace
):
    """Datasets constantly arrive with ISO timestamps typed as text, and until
    Explore synthesized the compiler's own `cast` step the whole class of
    time-series questions ("average delay by month") was silently impossible
    on them. One compilation path: the cast is a normal flow node, so nothing
    here is new machinery — this pins that the shape Explore now emits
    actually buckets."""
    cat = DatasetCatalog(workspace, MetadataStore(workspace.metadata_path))
    cat.write("events", pa.table({
        "when": ["2026-01-07 08:30:00", "2026-01-19 09:00:00",
                 "2026-02-02 10:00:00"],
        "delay": [5, 15, 30],
    }))
    flow = {
        "name": "explore", "output": "explore", "terminal": "n4", "nodes": [
            {"id": "n0", "kind": "source", "inputs": [],
             "params": {"dataset": "events"}},
            {"id": "n1", "kind": "cast", "inputs": ["n0"],
             "params": {"column": "when", "to": "timestamp"}},
            {"id": "n2", "kind": "derive", "inputs": ["n1"], "params": {
                "name": "when month",
                "expr": {"t": "op", "op": "date_trunc", "args": [
                    {"t": "lit", "type": "string", "value": "month"},
                    {"t": "col", "name": "when"}]}}},
            {"id": "n3", "kind": "aggregate", "inputs": ["n2"], "params": {
                "group_by": ["when month"],
                "aggs": [{"fn": "avg", "column": "delay",
                          "as": "average delay"}]}},
            {"id": "n4", "kind": "sort", "inputs": ["n3"], "params": {
                "by": [{"column": "when month", "dir": "asc",
                        "nulls": "last"}]}},
        ],
    }
    r = preview(client, flow)
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    # Chronological month buckets, averaged within each.
    assert [str(row["when month"])[:7] for row in rows] == ["2026-01", "2026-02"]
    assert [row["average delay"] for row in rows] == [10.0, 30.0]
    assert r.json()["kinds"]["when month"] == "time"


def test_a_cast_over_undateable_text_refuses_with_a_first_party_sentence(client):
    """The strict cast is the honest one — a column that is not really dates
    must say so, not chart garbage — but the sentence is Laurelin's
    (`_FLOW_EXEC_ADVICE`), never DuckDB's."""
    flow = {
        "name": "explore", "output": "explore", "terminal": "n1", "nodes": [
            {"id": "n0", "kind": "source", "inputs": [],
             "params": {"dataset": "orders"}},
            {"id": "n1", "kind": "cast", "inputs": ["n0"],
             "params": {"column": "status", "to": "timestamp"}},
        ],
    }
    r = preview(client, flow)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "could not be converted" in detail
    assert "Traceback" not in r.text
    assert "duckdb" not in r.text.lower()


# ---------------------------------------------------------------------------
# The hostile matrix, through the route
# ---------------------------------------------------------------------------

#: Members whose absence from a response body is meaningful. Short punctuation
#: (`?`, `_`, `%`, quotes, whitespace) and the words `NULL`/`true` occur in
#: ordinary JSON, so asserting their absence would be noise; the injection
#: payloads are the thing.
_ECHOABLE = [v for v in HOSTILE if len(v) >= 4 and v not in ("NULL", "true")]


@pytest.mark.parametrize("value", HOSTILE, ids=repr)
def test_a_hostile_value_in_any_explore_filter_or_bin_width_never_appears_in_the_compiled_sql(
    client, value
):
    """Every author value an Explore screen can emit travels as a bound
    parameter. A hostile filter value returns 200 with zero rows — treated as
    data, never as syntax — and a hostile bin width either binds or is refused
    with Laurelin's own sentence; no response body echoes generated SQL or
    driver prose, and none echoes the payload back.

    (The byte-identical-SQL form of this claim lives in test_flow_compile.py's
    matrix, which this route inherits by compiling through `compile_flow`;
    `bin_width_literal` and the median/floor identifier positions were added
    to that matrix alongside this test.)
    """
    filter_flow = json.loads(json.dumps(EXPLORE))
    filter_flow["nodes"][1]["params"]["predicate"] = {
        "t": "op", "op": "eq", "args": [
            {"t": "col", "name": "region"},
            {"t": "lit", "type": "string", "value": value}]}
    r = preview(client, filter_flow)
    assert r.status_code == 200, (value, r.text[:200])
    assert r.json()["row_count"] == 0  # bound and compared, matching nothing

    bin_flow = json.loads(json.dumps(EXPLORE))
    bin_flow["terminal"] = "n2"
    bin_flow["nodes"] = bin_flow["nodes"][:1] + [
        {"id": "n2", "kind": "derive", "inputs": ["n0"], "params": {
            "name": "bin",
            "expr": {"t": "op", "op": "mul", "args": [
                {"t": "op", "op": "floor", "args": [
                    {"t": "op", "op": "div", "args": [
                        {"t": "col", "name": "amount"},
                        {"t": "lit", "type": "string", "value": value}]}]},
                {"t": "lit", "type": "string", "value": value}]}}},
    ]
    rb = preview(client, bin_flow)
    assert rb.status_code in (200, 400), (value, rb.text[:200])

    for body in (r.text, rb.text):
        assert "_f0" not in body          # no compiled CTE
        assert "Traceback" not in body
        assert "duckdb" not in body.lower() or "definition" in body
        if value in _ECHOABLE:
            assert value not in body, (value, body[:300])


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def test_explore_preview_is_capped_at_200_rows_and_reports_truncation_honestly(
    client, workspace
):
    cat = DatasetCatalog(workspace, MetadataStore(workspace.metadata_path))
    cat.write("big", pa.table({"n": list(range(250))}))
    flow = {"name": "explore", "output": "explore", "terminal": "n0",
            "nodes": [{"id": "n0", "kind": "source", "inputs": [],
                       "params": {"dataset": "big"}}]}

    r = preview(client, flow, max_rows=100_000).json()
    assert r["max_rows"] == 200
    assert r["row_count"] == 200
    assert r["truncated"] is True

    r = preview(client, flow, max_rows=10).json()
    assert r["row_count"] == 10 and r["truncated"] is True


def test_explore_preview_requires_editor(workspace):
    from laurelin.core.models import Role, User

    store = MetadataStore(workspace.metadata_path)
    store.create_user(User(id="v", username="vic", role=Role.viewer), _hash("pw"))
    vic = _login(create_app(workspace), "vic")
    assert vic.post("/api/v1/explore/preview",
                    json={"flow": EXPLORE}).status_code in (401, 403)


# ---------------------------------------------------------------------------
# Governance: inherited posture, one deliberate divergence
# ---------------------------------------------------------------------------


def test_explore_preview_checks_source_view_rights_before_compiling_and_names_no_columns_when_refusing(
    workspace,
):
    """The disclosure ordering `_compiled_flow` exists for: `resolve_column`'s
    refusal enumerates a dataset's schema, so the view check must run first —
    a caller who cannot read the dataset learns neither its columns nor, via a
    deliberately mistyped reference, the shape of what they missed."""
    from laurelin.core.models import Role, User

    store = MetadataStore(workspace.metadata_path)
    for name in ("alice", "bob"):
        store.create_user(User(id=name, username=name, role=Role.editor),
                          _hash("pw"))
    store.set_grants_for_dataset("orders", [{
        "subject_kind": "user", "subject": "alice",
        "can_view": True, "can_edit": True,
    }])
    app = create_app(workspace)
    alice, bob = _login(app, "alice"), _login(app, "bob")

    probe = json.loads(json.dumps(EXPLORE))
    probe["nodes"][2]["params"]["group_by"] = ["not_a_column"]

    r = bob.post("/api/v1/explore/preview", json={"flow": probe})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "cannot read" in detail and "orders" in detail
    for column in ("region", "status", "amount"):
        assert f"'{column}'" not in detail  # the schema was never enumerated

    # Alice, who may read the source, gets the useful refusal instead.
    r = alice.post("/api/v1/explore/preview", json={"flow": probe})
    assert r.status_code == 400
    assert "not_a_column" in r.json()["detail"]
    assert "'region'" in r.json()["detail"]  # names what IS available


def test_explore_preview_applies_the_callers_row_policy_and_column_masks(workspace):
    """Preview is the caller's own policied view — never more than they may
    read. A masked column arrives as the mask's rendering, and
    `masked_columns` names it so the UI can grey it out of the measure pickers
    instead of letting DuckDB surface a binder error."""
    from laurelin.core.models import Role, User

    store = MetadataStore(workspace.metadata_path)
    store.create_user(User(id="e", username="eve", role=Role.editor), _hash("pw"))
    store.set_dataset_policy("orders", {
        "row_policy": {"column": "region", "rules": [
            {"subject_kind": "user", "subject": "eve", "values": ["eu"]}]},
        "column_masks": [{"column": "status", "mode": "redact"}],
    })
    eve = _login(create_app(workspace), "eve")

    flow = {"name": "explore", "output": "explore", "terminal": "n0",
            "nodes": [{"id": "n0", "kind": "source", "inputs": [],
                       "params": {"dataset": "orders"}}]}
    r = eve.post("/api/v1/explore/preview", json={"flow": flow})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {row["region"] for row in body["rows"]} == {"eu"}   # her rows only
    assert {row["status"] for row in body["rows"]} == {"***"}  # the mask, working
    assert body["masked_columns"] == {"orders": ["status"]}


def test_explore_preview_allows_a_row_policied_source_because_nothing_is_materialized(
    workspace,
):
    """The one deliberate divergence from `/flows/preview`, pinned so it can
    never be "discovered" as a bug. `check_flow_governance` refuses any flow
    reading a row-policied source because a *built* output is a new dataset
    with no policy — a materialization concern. Explore materializes nothing:
    the result above is computed under the caller's own row policy, exactly as
    the workbench and SQL panels already serve the same dataset. So the flow
    route refuses this source and the Explore route charts it."""
    from laurelin.core.models import Role, User

    store = MetadataStore(workspace.metadata_path)
    store.create_user(User(id="e", username="eve", role=Role.editor), _hash("pw"))
    store.set_dataset_policy("orders", {
        "row_policy": {"column": "region", "rules": [
            {"subject_kind": "user", "subject": "eve", "values": ["eu"]}]},
        "column_masks": [],
    })
    eve = _login(create_app(workspace), "eve")

    flows = eve.post("/api/v1/flows/preview", json={"flow": EXPLORE})
    assert flows.status_code == 400
    assert "row policy" in flows.json()["detail"]

    explore = eve.post("/api/v1/explore/preview", json={"flow": EXPLORE})
    assert explore.status_code == 200, explore.text
    assert explore.json()["rows"] == [{"region": "eu", "total": 25}]
