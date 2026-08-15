"""Tier A: everything a flow can be refused for without touching a schema.

Tier A runs on every registry collection — which is every API request — so it
must need no catalog and no remote round trip. What it establishes is that a
flow is *structurally* a flow: closed enums, correct arity, typed literals, a
connected acyclic graph with one terminal. Identifier membership is Tier B and
lives in ``test_flow_compile.py``.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from laurelin.transforms.flow_ir import FlowDef, FlowRefused

SRC = {"id": "n0", "kind": "source", "inputs": [], "params": {"dataset": "orders"}}


def flow(nodes, terminal="n0", name="out", **kw):
    body = {"output": name, "author": "alice", "terminal": terminal,
            "nodes": nodes, **kw}
    return FlowDef.from_json(body, name=name)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_a_flow_needs_at_least_one_step_and_a_terminal_that_is_one_of_them():
    with pytest.raises(FlowRefused):
        flow([])
    with pytest.raises(FlowRefused):
        flow([SRC], terminal="nope")


def test_a_flows_output_dataset_must_be_its_own_name():
    """Flow name == transform name == output dataset name.

    Not cosmetic. `spec.name` is the primary key of `lineage_edges`,
    `transform_state` and `build_tasks`, and nothing in this tree deletes
    lineage on rename — so a flow that could name a different output would be
    a flow that could orphan governance state.
    """
    with pytest.raises(FlowRefused) as exc:
        FlowDef.from_json(
            {"output": "something_else", "author": "a", "terminal": "n0",
             "nodes": [SRC]},
            name="out",
        )
    assert "output" in str(exc.value)


def test_the_name_in_the_path_wins_over_the_name_in_the_body():
    """The route path and the filename are authoritative.

    Otherwise a PUT to /flows/mine carrying `"name": "yours"` writes to
    someone else's flow.
    """
    with pytest.raises(FlowRefused):
        FlowDef.from_json(
            {"name": "yours", "output": "yours", "author": "a",
             "terminal": "n0", "nodes": [SRC]},
            name="mine",
        )


def test_every_step_kind_has_a_fixed_input_arity():
    with pytest.raises(FlowRefused) as exc:
        flow([SRC, {"id": "n1", "kind": "filter", "inputs": ["n0", "n0"],
                    "params": {"predicate": _pred()}}], terminal="n1")
    assert "input" in str(exc.value)

    # join is the only 2-ary kind, and its inputs are ordered.
    with pytest.raises(FlowRefused):
        flow([SRC, {"id": "n1", "kind": "join", "inputs": ["n0"],
                    "params": {"how": "inner",
                               "keys": [{"left": "a", "right": "b"}]}}],
             terminal="n1")


def test_a_cycle_is_refused_structurally_rather_than_left_to_the_build_planner():
    """Refused at PUT, where an author is watching.

    `Builder.plan` would also catch it, but only at build time — where the only
    witness is a failed task.
    """
    nodes = [
        SRC,
        {"id": "a", "kind": "filter", "inputs": ["b"], "params": {"predicate": _pred()}},
        {"id": "b", "kind": "filter", "inputs": ["a"], "params": {"predicate": _pred()}},
    ]
    with pytest.raises(FlowRefused) as exc:
        flow(nodes, terminal="a")
    assert "loop" in str(exc.value)


def test_a_step_not_connected_to_the_terminal_is_refused():
    """Not tidiness: an orphaned `source` node would still be counted by
    `source_datasets()`, and would therefore propagate that dataset's
    classification markings onto an output that never read it."""
    orphan = {"id": "orphan", "kind": "source", "inputs": [],
              "params": {"dataset": "other"}}
    with pytest.raises(FlowRefused) as exc:
        flow([SRC, orphan], terminal="n0")
    assert "orphan" in str(exc.value)


def test_two_steps_cannot_share_an_id():
    with pytest.raises(FlowRefused):
        flow([SRC, dict(SRC)], terminal="n0")


def test_source_datasets_are_derived_from_the_ir_and_deduplicated():
    nodes = [
        SRC,
        {"id": "n1", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n2", "kind": "source", "inputs": [],
         "params": {"dataset": "regions"}},
        {"id": "j", "kind": "join", "inputs": ["n0", "n2"], "params": {
            "how": "inner", "keys": [{"left": "a", "right": "b"}]}},
        {"id": "k", "kind": "join", "inputs": ["j", "n1"], "params": {
            "how": "left", "keys": [{"left": "a", "right": "b"}]}},
    ]
    assert flow(nodes, terminal="k").source_datasets() == ["orders", "regions"]


# ---------------------------------------------------------------------------
# Literals
# ---------------------------------------------------------------------------


def _pred(value="us"):
    return {"t": "op", "op": "eq", "args": [
        {"t": "col", "name": "region"},
        {"t": "lit", "type": "string", "value": value}]}


def _with_lit(type_name, value):
    return flow(
        [SRC, {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
            "predicate": {"t": "op", "op": "eq", "args": [
                {"t": "col", "name": "c"},
                {"t": "lit", "type": type_name, "value": value}]}}}],
        terminal="n1",
    )


def test_a_literal_is_coerced_to_a_typed_python_object_by_the_validator():
    """So what reaches the parameter list is never a string to reinterpret."""
    node = _with_lit("date", "2024-03-01").node("n1")
    assert node.params["predicate"]["args"][1]["value"] == dt.date(2024, 3, 1)

    node = _with_lit("timestamp", "2024-03-01T12:30:00").node("n1")
    assert node.params["predicate"]["args"][1]["value"] == dt.datetime(
        2024, 3, 1, 12, 30
    )

    assert _with_lit("double", 3).node("n1").params["predicate"]["args"][1][
        "value"
    ] == 3.0


@pytest.mark.parametrize(
    "type_name,value",
    [
        ("bigint", "5"),        # a string is not a number
        ("bigint", True),       # a checkbox is not a number
        ("bigint", 1.5),
        ("double", "x"),
        ("boolean", 1),
        ("boolean", "true"),
        ("date", "not-a-date"),
        ("date", 20240301),
        ("timestamp", "yesterday"),
        ("string", 5),
        ("null", "x"),          # a null literal carries no value
    ],
)
def test_a_literal_whose_value_does_not_match_its_declared_type_is_refused(
    type_name, value
):
    """Strict on purpose.

    Silently accepting "5" where a bigint was declared means the author's
    filter and their preview can disagree about what `>` does.
    """
    with pytest.raises(FlowRefused):
        _with_lit(type_name, value)


def test_a_null_literal_is_accepted_and_carries_none():
    node = _with_lit("null", None).node("n1")
    assert node.params["predicate"]["args"][1]["value"] is None


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


def test_a_filter_predicate_must_be_a_comparison_not_a_bare_column_or_value():
    """`WHERE "amount"` is a type error DuckDB would report, not us."""
    for expr in ({"t": "col", "name": "flag"},
                 {"t": "lit", "type": "boolean", "value": True},
                 {"t": "op", "op": "upper", "args": [{"t": "col", "name": "c"}]}):
        with pytest.raises(FlowRefused) as exc:
            flow([SRC, {"id": "n1", "kind": "filter", "inputs": ["n0"],
                        "params": {"predicate": expr}}], terminal="n1")
        assert "comparison" in str(exc.value)


def test_the_values_of_an_in_list_must_be_literals_and_never_expressions():
    """Each one is bound individually; an expression there has nowhere to go."""
    with pytest.raises(FlowRefused):
        flow([SRC, {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
            "predicate": {"t": "op", "op": "in", "args": [
                {"t": "col", "name": "region"},
                {"t": "col", "name": "other"}]}}}], terminal="n1")


def test_a_like_pattern_must_be_a_text_value_so_its_wildcards_stay_data():
    with pytest.raises(FlowRefused):
        flow([SRC, {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
            "predicate": {"t": "op", "op": "like", "args": [
                {"t": "col", "name": "region"},
                {"t": "lit", "type": "bigint", "value": 5}]}}}], terminal="n1")


def test_a_date_trunc_unit_is_both_bound_and_restricted_to_a_closed_vocabulary():
    """Belt and braces: bound so a value cannot become syntax, enum-checked so
    a typo is our sentence rather than a DuckDB one."""
    with pytest.raises(FlowRefused) as exc:
        flow([SRC, {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
            "predicate": {"t": "op", "op": "eq", "args": [
                {"t": "op", "op": "date_trunc", "args": [
                    {"t": "lit", "type": "string", "value": "fortnight"},
                    {"t": "col", "name": "at"}]},
                {"t": "col", "name": "at"}]}}}], terminal="n1")
    assert "fortnight" in str(exc.value)


def test_an_expression_nested_beyond_the_depth_limit_is_refused_not_recursed():
    expr = {"t": "col", "name": "c"}
    for _ in range(40):
        expr = {"t": "op", "op": "upper", "args": [expr]}
    with pytest.raises(FlowRefused) as exc:
        flow([SRC, {"id": "n1", "kind": "derive", "inputs": ["n0"],
                    "params": {"name": "x", "expr": expr}}], terminal="n1")
    assert "deeply" in str(exc.value)


def test_an_unknown_key_in_an_expression_does_not_survive_validation():
    """Validation returns a normalized copy, so a hostile extra key cannot
    ride along to be picked up by some later reader."""
    f = flow([SRC, {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
        "predicate": {"t": "op", "op": "eq", "args": [
            {"t": "col", "name": "region", "sneaky": "payload"},
            {"t": "lit", "type": "string", "value": "us"}]}}}], terminal="n1")
    assert f.node("n1").params["predicate"]["args"][0] == {
        "t": "col", "name": "region"
    }


# ---------------------------------------------------------------------------
# Per-node parameter rules
# ---------------------------------------------------------------------------


def test_counting_rows_takes_no_column_and_counting_values_requires_one():
    """`count(*)` counts rows; `count(c)` counts non-NULLs. An analyst who
    cannot see the difference in the UI will pick the wrong one, so the IR
    refuses the ambiguous middle."""
    with pytest.raises(FlowRefused):
        _agg([{"fn": "count_star", "column": "amount", "as": "n"}])
    with pytest.raises(FlowRefused):
        _agg([{"fn": "sum", "as": "n"}])
    _agg([{"fn": "count_star", "as": "n"}])


def _agg(aggs, group_by=("region",)):
    return flow([SRC, {"id": "n1", "kind": "aggregate", "inputs": ["n0"],
                       "params": {"group_by": list(group_by), "aggs": aggs}}],
                terminal="n1")


def test_an_aggregate_cannot_produce_two_columns_with_the_same_name():
    with pytest.raises(FlowRefused):
        _agg([{"fn": "sum", "column": "amount", "as": "region"}])
    with pytest.raises(FlowRefused):
        _agg([{"fn": "sum", "column": "amount", "as": "t"},
              {"fn": "min", "column": "amount", "as": "t"}])


def test_a_dedupe_must_declare_an_order_because_first_without_one_is_a_coin_flip():
    """A nondeterministic dedupe feeding an object type reshuffles object pages
    under readers — the ontology assigns object ordinals from file order."""
    with pytest.raises(FlowRefused):
        flow([SRC, {"id": "n1", "kind": "dedupe", "inputs": ["n0"],
                    "params": {"keys": ["id"], "keep": "first"}}], terminal="n1")
    with pytest.raises(FlowRefused):
        flow([SRC, {"id": "n1", "kind": "dedupe", "inputs": ["n0"], "params": {
            "keys": ["id"], "keep": "first", "order_by": []}}], terminal="n1")


def test_a_rename_cannot_rename_two_columns_to_the_same_name():
    with pytest.raises(FlowRefused):
        flow([SRC, {"id": "n1", "kind": "rename", "inputs": ["n0"], "params": {
            "pairs": [{"from": "a", "to": "x"}, {"from": "b", "to": "x"}]}}],
             terminal="n1")


def test_a_select_cannot_list_the_same_column_twice():
    with pytest.raises(FlowRefused):
        flow([SRC, {"id": "n1", "kind": "select", "inputs": ["n0"],
                    "params": {"mode": "keep", "columns": ["a", "a"]}}],
             terminal="n1")


# ---------------------------------------------------------------------------
# Expectations
# ---------------------------------------------------------------------------


def test_the_expectations_a_flow_may_declare_are_a_closed_subset():
    """`expectations.expression()` interpolates a raw predicate into
    `WHERE NOT (...)`. It is never reachable from a flow, at any level — it
    stays for the Python path."""
    with pytest.raises(FlowRefused):
        flow([SRC], expectations=[{"kind": "expression", "column": "a"}])
    with pytest.raises(FlowRefused):
        flow([SRC], expectations=[{"kind": "not_null"}])  # needs a column

    f = flow([SRC], expectations=[
        {"kind": "not_null", "column": "id"},
        {"kind": "unique", "column": "id"},
        {"kind": "accepted_values", "column": "s", "values": ["a", "b"]},
        {"kind": "row_count_between", "min": 1, "max": 10},
    ])
    assert [e.kind for e in f.expectations] == [
        "not_null", "unique", "accepted_values", "row_count_between"
    ]


def test_row_count_bounds_must_make_sense():
    with pytest.raises(FlowRefused):
        flow([SRC], expectations=[{"kind": "row_count_between"}])
    with pytest.raises(FlowRefused):
        flow([SRC], expectations=[
            {"kind": "row_count_between", "min": 10, "max": 1}])


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_a_flow_survives_a_json_round_trip_unchanged():
    """The file on disk is the source of truth, so this has to be exact."""
    original = flow(
        [SRC,
         {"id": "n1", "kind": "filter", "inputs": ["n0"],
          "params": {"predicate": _pred()}},
         {"id": "n2", "kind": "derive", "inputs": ["n1"], "params": {
             "name": "big", "expr": {"t": "op", "op": "gt", "args": [
                 {"t": "col", "name": "amount"},
                 {"t": "lit", "type": "double", "value": 1.5}]}}}],
        terminal="n2",
        description="a flow",
        expectations=[{"kind": "not_null", "column": "region"}],
    )
    text = json.dumps(original.as_json())
    again = FlowDef.from_json(json.loads(text), name="out")
    assert again.as_json() == original.as_json()
    assert again == original


def test_a_flow_definition_is_frozen_once_validated():
    """A validated object that can be mutated afterwards is validated in name
    only — and this one is handed to the Builder and to the governance check."""
    import dataclasses

    f = flow([SRC])
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.terminal = "elsewhere"
