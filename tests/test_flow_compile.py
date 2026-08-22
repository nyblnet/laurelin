"""The compiler's safety contract, swept over every position an author fills.

The headline test in this file is
``test_two_flows_differing_only_in_their_literal_values_compile_to_identical_sql``.
That single assertion *is* the invariant of ``flow_compile``:

    the compiled SQL text is a pure function of the flow's structure, and
    contains not one byte derived from any author-supplied value.

Everything else here is coverage of that claim across the surface.
"""

from __future__ import annotations

import copy
import json

import duckdb
import pyarrow as pa
import pytest

from laurelin.transforms.flow_compile import compile_flow
from laurelin.transforms.flow_ir import FlowDef, FlowRefused

# ---------------------------------------------------------------------------
# The shared hostile vocabulary.
#
# One tuple, used by every matrix in this file and by tests/test_authoring.py,
# so a value that breaks one surface is automatically tried against the others.
# The members are not decorative: `"""` is the string that crashed
# `generate_sql_transform`, `\\` is the one that silently corrupted it,
# `read_csv_auto('/etc/passwd')` is the primitive that was live on the build
# connection, and `; DROP TABLE lineage_edges; --` is what a stacked statement
# would target if one were ever reachable.
# ---------------------------------------------------------------------------

HOSTILE = (
    '"', '""', '"""', "'", "''", "\\", "\\\\", "`", ";",
    "; DROP TABLE lineage_edges; --", "-- x", "/* x */",
    "\x00", "\n", "\r\n", "\t", "a\\`b", "%", "_", "?", "$1",
    "' OR 1=1 --", "' UNION SELECT * FROM secret --",
    "read_csv_auto('/etc/passwd')", "ATTACH 'x' AS y", "COPY t TO '/tmp/x'",
    "${x}", "{{x}}", "‮", "ünïcode", "NULL", "true", "", " ", "a" * 200,
)

SCHEMAS = {
    "orders": ["region", "status", "amount", "customer", "placed_at"],
    "regions": ["region_code", "region_name"],
}


def col(name):
    return {"t": "col", "name": name}


def lit(type_name, value):
    return {"t": "lit", "type": type_name, "value": value}


def _flow(nodes, terminal, name="out", expectations=None):
    return FlowDef.from_json(
        {
            "output": name, "author": "alice", "terminal": terminal,
            "nodes": nodes, "expectations": expectations or [],
        },
        name=name,
    )


def base_nodes():
    """source -> filter, the smallest flow with a literal and an identifier."""
    return [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
            "predicate": {"t": "op", "op": "eq",
                          "args": [col("region"), lit("string", "us")]}}},
    ]


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


def test_two_flows_differing_only_in_their_literal_values_compile_to_identical_sql():
    """The one assertion that states the whole safety contract.

    If a value ever reaches the SQL text, these two strings diverge. No other
    test in this file is load-bearing in the same way: this one fails for
    *every* interpolation defect, including ones in positions nobody thought
    to enumerate.
    """
    a = _flow(base_nodes(), "n1")
    hostile = copy.deepcopy(base_nodes())
    hostile[1]["params"]["predicate"]["args"][1]["value"] = (
        "'; DROP TABLE lineage_edges; --"
    )
    b = _flow(hostile, "n1")

    ca = compile_flow(a, SCHEMAS)
    cb = compile_flow(b, SCHEMAS)

    assert ca.sql == cb.sql
    assert ca.params != cb.params
    assert ca.params == ["us"]
    assert cb.params == ["'; DROP TABLE lineage_edges; --"]


#: A value with no SQL meaning, used as the baseline every hostile value is
#: compared against.
BENIGN = "benign_baseline_value"

LITERAL_POSITIONS = {
    "filter_literal": lambda v: _with_filter_literal(v),
    "in_element": lambda v: _with_in_element(v),
    "like_pattern": lambda v: _with_like_pattern(v),
    "derive_literal": lambda v: _with_derive_literal(v),
    # The histogram composition Explore emits: bin = floor(col / width) * width.
    # The width is an author value and appears in TWO positions of the same
    # expression, so this sweeps both at once.
    "bin_width_literal": lambda v: _with_bin_width(v),
}


@pytest.mark.parametrize("value", HOSTILE, ids=repr)
@pytest.mark.parametrize("position", sorted(LITERAL_POSITIONS))
def test_a_hostile_value_in_any_literal_position_never_appears_in_the_sql_text(
    position, value
):
    """The value goes to `params`; the SQL is byte-identical to the benign one.

    Stated as "identical to the baseline" rather than "the value is not a
    substring of the SQL", because the substring form has false *positives*
    that hide real failures behind noise: a value of `"` or `_` or `?` or
    `' UNION SELECT ...` shares characters with the compiler's own output
    (quoted identifiers, the `_f0` CTE names, the placeholder itself, the word
    SELECT), so a substring assertion fires on values that are perfectly inert
    — and an empty-string value makes it fire unconditionally.

    Baseline equality has no such blind spot and is strictly stronger: it fails
    for *any* difference the value produced, including a partial or truncating
    interpolation that a substring check on the whole value would miss.
    """
    build = LITERAL_POSITIONS[position]
    baseline = compile_flow(build(BENIGN), SCHEMAS)
    compiled = compile_flow(build(value), SCHEMAS)

    assert compiled.sql == baseline.sql
    assert value in compiled.params
    assert BENIGN not in compiled.params
    # Same number of placeholders either way: a value cannot add or remove one.
    assert compiled.sql.count("?") == baseline.sql.count("?")
    assert len(compiled.params) == len(baseline.params)


def _with_filter_literal(value):
    nodes = base_nodes()
    nodes[1]["params"]["predicate"]["args"][1] = lit("string", value)
    return _flow(nodes, "n1")


def _with_in_element(value):
    nodes = base_nodes()
    nodes[1]["params"]["predicate"] = {
        "t": "op", "op": "in",
        "args": [col("region"), lit("string", value), lit("string", "other")],
    }
    return _flow(nodes, "n1")


def _with_like_pattern(value):
    nodes = base_nodes()
    nodes[1]["params"]["predicate"] = {
        "t": "op", "op": "like", "args": [col("region"), lit("string", value)],
    }
    return _flow(nodes, "n1")


def _with_derive_literal(value):
    nodes = base_nodes()
    nodes.append({"id": "n2", "kind": "derive", "inputs": ["n1"], "params": {
        "name": "tag",
        "expr": {"t": "op", "op": "coalesce",
                 "args": [col("status"), lit("string", value)]},
    }})
    return _flow(nodes, "n2")


def _with_bin_width(value):
    nodes = base_nodes()
    nodes.append({"id": "n2", "kind": "derive", "inputs": ["n1"], "params": {
        "name": "bin",
        "expr": {"t": "op", "op": "mul", "args": [
            {"t": "op", "op": "floor", "args": [
                {"t": "op", "op": "div",
                 "args": [col("amount"), lit("string", value)]}]},
            lit("string", value)]},
    }})
    return _flow(nodes, "n2")


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------

IDENTIFIER_POSITIONS = (
    "source_dataset", "filter_column", "select_column", "rename_from",
    "derive_column_ref", "cast_column", "join_key_left", "join_key_right",
    "group_by_column", "agg_source_column", "median_source_column",
    "floor_column_ref", "dedupe_key",
    "dedupe_order_column", "sort_column",
)


def _flow_with_identifier(position, value):
    nodes = [{"id": "n0", "kind": "source", "inputs": [],
              "params": {"dataset": "orders"}}]
    if position == "source_dataset":
        nodes[0]["params"]["dataset"] = value
        return _flow(nodes, "n0")
    if position == "filter_column":
        nodes.append({"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
            "predicate": {"t": "op", "op": "eq",
                          "args": [col(value), lit("string", "x")]}}})
    elif position == "select_column":
        nodes.append({"id": "n1", "kind": "select", "inputs": ["n0"],
                      "params": {"mode": "keep", "columns": [value]}})
    elif position == "rename_from":
        nodes.append({"id": "n1", "kind": "rename", "inputs": ["n0"],
                      "params": {"pairs": [{"from": value, "to": "renamed"}]}})
    elif position == "derive_column_ref":
        nodes.append({"id": "n1", "kind": "derive", "inputs": ["n0"], "params": {
            "name": "derived",
            "expr": {"t": "op", "op": "upper", "args": [col(value)]}}})
    elif position == "cast_column":
        nodes.append({"id": "n1", "kind": "cast", "inputs": ["n0"],
                      "params": {"column": value, "to": "varchar"}})
    elif position in ("join_key_left", "join_key_right"):
        nodes.append({"id": "nr", "kind": "source", "inputs": [],
                      "params": {"dataset": "regions"}})
        left = value if position == "join_key_left" else "region"
        right = value if position == "join_key_right" else "region_code"
        nodes.append({"id": "n1", "kind": "join", "inputs": ["n0", "nr"],
                      "params": {"how": "inner",
                                 "keys": [{"left": left, "right": right}]}})
    elif position == "group_by_column":
        nodes.append({"id": "n1", "kind": "aggregate", "inputs": ["n0"], "params": {
            "group_by": [value],
            "aggs": [{"fn": "count_star", "as": "n"}]}})
    elif position == "agg_source_column":
        nodes.append({"id": "n1", "kind": "aggregate", "inputs": ["n0"], "params": {
            "group_by": ["region"],
            "aggs": [{"fn": "sum", "column": value, "as": "total"}]}})
    elif position == "median_source_column":
        nodes.append({"id": "n1", "kind": "aggregate", "inputs": ["n0"], "params": {
            "group_by": ["region"],
            "aggs": [{"fn": "median", "column": value, "as": "mid"}]}})
    elif position == "floor_column_ref":
        nodes.append({"id": "n1", "kind": "derive", "inputs": ["n0"], "params": {
            "name": "binned",
            "expr": {"t": "op", "op": "floor", "args": [col(value)]}}})
    elif position == "dedupe_key":
        nodes.append({"id": "n1", "kind": "dedupe", "inputs": ["n0"], "params": {
            "keys": [value], "keep": "first",
            "order_by": [{"column": "amount", "dir": "asc"}]}})
    elif position == "dedupe_order_column":
        nodes.append({"id": "n1", "kind": "dedupe", "inputs": ["n0"], "params": {
            "keys": ["region"], "keep": "first",
            "order_by": [{"column": value, "dir": "asc"}]}})
    elif position == "sort_column":
        nodes.append({"id": "n1", "kind": "sort", "inputs": ["n0"], "params": {
            "by": [{"column": value, "dir": "asc", "nulls": "last"}]}})
    else:  # pragma: no cover - the parametrise list and this must agree
        raise AssertionError(position)
    return _flow(nodes, "n1")


@pytest.mark.parametrize("value", HOSTILE, ids=repr)
@pytest.mark.parametrize("position", IDENTIFIER_POSITIONS)
def test_a_hostile_value_in_any_identifier_position_is_refused_when_it_is_not_a_real_column(
    position, value
):
    """An identifier is legal only by membership in the live schema.

    Never a pattern. A parameter placeholder is not an option either: measured
    on this tree, `SELECT ? AS c FROM r` with `["a"]` returns the *constant*
    `'a'` on every row rather than column `a` — a silent wrong answer, which is
    worse than an error.
    """
    with pytest.raises(FlowRefused) as exc:
        flow = _flow_with_identifier(position, value)
        compile_flow(flow, SCHEMAS)

    message = str(exc.value)
    # R1: the refusal is Laurelin's own sentence, raised before any SQL exists.
    #
    # It *does* echo the author's own input — that is the point, and it is why
    # this does not assert the absence of words like SELECT: a column the
    # author typed as `' UNION SELECT * FROM secret --` has to be quoted back
    # at them or the message is useless. What must be absent is *generated* SQL
    # and any third-party sentence.
    assert "_f0" not in message          # no compiled CTE
    assert "WITH " not in message        # no compiled statement
    assert "duckdb" not in message.lower()
    assert "Traceback" not in message
    assert "/home" not in message and "\\Users" not in message


def test_a_column_whose_real_name_contains_a_double_quote_is_addressed_exactly():
    """The case that must WORK, not be refused.

    `a"b`, `a\\`b` and `total (USD)` are all legal Parquet column names. A regex
    over identifiers would reject data an analyst legitimately has — which is
    why the rule is membership-then-quote, and why the quoter is not asked to
    be a security boundary.
    """
    weird = 'a"b'
    schemas = {"orders": [weird, "amount"]}
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
            "predicate": {"t": "op", "op": "eq",
                          "args": [col(weird), lit("string", "keep")]}}},
    ]
    compiled = compile_flow(_flow(nodes, "n1"), schemas)
    # DuckDB's own rule: double the inner quote.
    assert '"a""b"' in compiled.sql

    table = pa.table({weird: ["keep", "drop"], "amount": [1, 2]})
    con = duckdb.connect()
    con.register("orders", table)
    rows = con.execute(compiled.sql, compiled.params).fetchall()
    con.close()
    assert rows == [("keep", 1)]


def test_an_invented_identifier_is_restricted_to_letters_digits_underscore_and_space():
    """A column the author *invents* is narrower than one they point at.

    Deliberate asymmetry. A referenced column must accept whatever the data
    really contains; an invented one can be restricted without losing anything,
    and restricting it means a flow can never mint a name that a future
    ClickHouse (backslash) or StarRocks (backtick) renderer would mis-resolve.
    """
    assert _derive_named("total in usd") is None  # accepted
    for bad in ('a"b', "a\\b", "a`b", "a.b", "a\nb", "1abc", "", "a" * 200):
        assert _derive_named(bad) is not None, bad


def _derive_named(name):
    nodes = base_nodes()
    nodes.append({"id": "n2", "kind": "derive", "inputs": ["n1"], "params": {
        "name": name, "expr": {"t": "op", "op": "upper", "args": [col("status")]}}})
    try:
        compile_flow(_flow(nodes, "n2"), SCHEMAS)
        return None
    except FlowRefused as exc:
        return str(exc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

ENUM_POSITIONS = (
    "cast_to", "agg_fn", "join_how", "sort_dir", "sort_nulls",
    "dedupe_keep", "select_mode", "op", "lit_type", "date_trunc_unit",
)


def _flow_with_enum(position, value):
    nodes = base_nodes()
    if position == "cast_to":
        nodes.append({"id": "n2", "kind": "cast", "inputs": ["n1"],
                      "params": {"column": "amount", "to": value}})
    elif position == "agg_fn":
        nodes.append({"id": "n2", "kind": "aggregate", "inputs": ["n1"], "params": {
            "group_by": ["region"],
            "aggs": [{"fn": value, "column": "amount", "as": "total"}]}})
    elif position == "join_how":
        nodes.append({"id": "nr", "kind": "source", "inputs": [],
                      "params": {"dataset": "regions"}})
        nodes.append({"id": "n2", "kind": "join", "inputs": ["n1", "nr"], "params": {
            "how": value,
            "keys": [{"left": "region", "right": "region_code"}]}})
    elif position in ("sort_dir", "sort_nulls"):
        entry = {"column": "amount", "dir": "asc", "nulls": "last"}
        entry["dir" if position == "sort_dir" else "nulls"] = value
        nodes.append({"id": "n2", "kind": "sort", "inputs": ["n1"],
                      "params": {"by": [entry]}})
    elif position == "dedupe_keep":
        nodes.append({"id": "n2", "kind": "dedupe", "inputs": ["n1"], "params": {
            "keys": ["region"], "keep": value,
            "order_by": [{"column": "amount", "dir": "asc"}]}})
    elif position == "select_mode":
        nodes.append({"id": "n2", "kind": "select", "inputs": ["n1"],
                      "params": {"mode": value, "columns": ["region"]}})
    elif position == "op":
        nodes[1]["params"]["predicate"] = {
            "t": "op", "op": value, "args": [col("region"), lit("string", "x")]}
    elif position == "lit_type":
        nodes[1]["params"]["predicate"]["args"][1] = {
            "t": "lit", "type": value, "value": "x"}
    elif position == "date_trunc_unit":
        nodes[1]["params"]["predicate"] = {
            "t": "op", "op": "eq", "args": [
                {"t": "op", "op": "date_trunc",
                 "args": [lit("string", value), col("placed_at")]},
                col("placed_at")]}
    else:  # pragma: no cover
        raise AssertionError(position)
    terminal = "n2" if position not in ("op", "lit_type", "date_trunc_unit") else "n1"
    return _flow(nodes, terminal)


@pytest.mark.parametrize("value", HOSTILE, ids=repr)
@pytest.mark.parametrize("position", ENUM_POSITIONS)
def test_every_enum_position_rejects_a_value_outside_its_vocabulary(position, value):
    """Enum positions are rendered, so they must be closed.

    `CAST(x AS ?)` is a *parser* error in DuckDB and `ORDER BY ?` is rejected
    outright, so these positions can only ever be text. The safety property is
    therefore that the text is a keyword the compiler already contains, chosen
    by a key that was checked against a fixed tuple.
    """
    with pytest.raises(FlowRefused):
        compile_flow(_flow_with_enum(position, value), SCHEMAS)


#: The one HOSTILE member that is a perfectly ordinary Laurelin name. It is
#: kept in HOSTILE because it is a SQL keyword and worth sweeping through every
#: *other* position, but a dataset may legitimately be called `true` and
#: refusing it here would be a bug, not a defence.
LEGAL_AS_A_NAME = frozenset({"true"})


@pytest.mark.parametrize("value", HOSTILE, ids=repr)
def test_a_hostile_value_in_a_flow_name_is_refused_before_any_sql_is_built(value):
    """A flow's name is a dataset name AND a filename, so it is doubly bounded.

    `'a' * 200` is refused by the length bound rather than the character rule:
    the name becomes `<name>.flow.json` on disk, and an unbounded one would
    surface as an OSError from the atomic write — at the moment of saving
    work — instead of as our sentence.
    """
    if value in LEGAL_AS_A_NAME:
        assert _flow(base_nodes(), "n1", name=value).name == value
        return
    with pytest.raises(FlowRefused):
        _flow(base_nodes(), "n1", name=value)


@pytest.mark.parametrize("value", HOSTILE, ids=repr)
def test_a_hostile_value_in_a_node_id_is_refused_or_never_reaches_the_sql(value):
    """Node ids are the one author-supplied name with a *structural* defence.

    Some HOSTILE members (`'_'`, `'true'`) are legal node ids and are supposed
    to be — they are internal handles, not SQL. The property that matters is
    not that they are rejected but that they are *unreachable*: CTEs are named
    positionally (`_f0`, `_f1`, …), so a node id has no path into the statement
    at all.

    Asserted as equality with the baseline rather than "not a substring", for
    the reason given on the literal matrix above: `'_'` occurs in the
    compiler's own CTE names, so a substring check would fail on a value that
    is entirely inert.
    """
    baseline = compile_flow(_flow(base_nodes(), "n1"), SCHEMAS)

    nodes = base_nodes()
    nodes[0]["id"] = value
    nodes[1]["inputs"] = [value]
    try:
        flow = _flow(nodes, "n1")
    except FlowRefused:
        return  # refused outright: also fine
    assert compile_flow(flow, SCHEMAS).sql == baseline.sql


def test_a_node_id_never_reaches_the_compiled_sql():
    """CTEs are named positionally, not after the author's step ids.

    Node ids are pattern-validated, but naming CTEs `_f0`, `_f1`, … means even
    a defect in that pattern could not put an author byte into the statement.
    """
    nodes = base_nodes()
    nodes[0]["id"] = "sneaky_source_name"
    nodes[1]["inputs"] = ["sneaky_source_name"]
    compiled = compile_flow(_flow(nodes, "n1"), SCHEMAS)
    assert "sneaky_source_name" not in compiled.sql
    assert "_f0" in compiled.sql


# ---------------------------------------------------------------------------
# Per-node compilation
# ---------------------------------------------------------------------------


def _run(flow, tables, schemas=None):
    compiled = compile_flow(flow, schemas or SCHEMAS)
    con = duckdb.connect()
    try:
        for name, table in tables.items():
            con.register(name, table)
        return compiled, con.execute(compiled.sql, compiled.params).fetchall()
    finally:
        con.close()


ORDERS = pa.table({
    "region": ["us", "us", "eu", "eu"],
    "status": ["ok", "returned", "ok", "ok"],
    "amount": [10, 99, 20, 5],
    "customer": ["a", "b", "c", "d"],
    "placed_at": ["2024-01-01", "2024-02-01", "2024-01-15", "2024-03-02"],
})


def test_a_filter_and_an_aggregate_produce_the_answer_the_author_described():
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
            "predicate": {"t": "op", "op": "ne",
                          "args": [col("status"), lit("string", "returned")]}}},
        {"id": "n2", "kind": "aggregate", "inputs": ["n1"], "params": {
            "group_by": ["region"],
            "aggs": [{"fn": "sum", "column": "amount", "as": "total"}]}},
        {"id": "n3", "kind": "sort", "inputs": ["n2"], "params": {
            "by": [{"column": "region", "dir": "asc", "nulls": "last"}]}},
    ]
    compiled, rows = _run(_flow(nodes, "n3"), {"orders": ORDERS})
    assert rows == [("eu", 25), ("us", 10)]
    assert compiled.schema == ["region", "total"]
    assert compiled.inputs == ["orders"]


def test_a_dedupe_keeps_one_row_per_key_in_the_order_the_author_chose():
    """`order_by` is mandatory, and this is why it has to be.

    "Keep the first" without an order is not a definition — it is stable until
    the input is compacted. A nondeterministic dedupe feeding an object type
    reshuffles object pages under readers, because the ontology assigns object
    ordinals from file order.
    """
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "dedupe", "inputs": ["n0"], "params": {
            "keys": ["region"], "keep": "last",
            "order_by": [{"column": "amount", "dir": "asc"}]}},
        {"id": "n2", "kind": "sort", "inputs": ["n1"], "params": {
            "by": [{"column": "region", "dir": "asc", "nulls": "last"}]}},
    ]
    _compiled, rows = _run(_flow(nodes, "n2"), {"orders": ORDERS})
    assert [(r[0], r[2]) for r in rows] == [("eu", 20), ("us", 99)]


def test_a_join_refuses_two_sides_that_share_a_column_and_names_the_fix():
    schemas = {"a": ["id", "amount"], "b": ["id", "amount"]}
    nodes = [
        {"id": "l", "kind": "source", "inputs": [], "params": {"dataset": "a"}},
        {"id": "r", "kind": "source", "inputs": [], "params": {"dataset": "b"}},
        {"id": "j", "kind": "join", "inputs": ["l", "r"], "params": {
            "how": "inner", "keys": [{"left": "id", "right": "id"}]}},
    ]
    with pytest.raises(FlowRefused) as exc:
        compile_flow(_flow(nodes, "j"), schemas)
    assert "amount" in str(exc.value)
    assert "rename" in str(exc.value)


def test_a_dedupe_over_a_column_literally_named_like_the_helper_column_still_works():
    """The row-number column the compiler invents cannot collide with real data."""
    schemas = {"orders": ["region", "_laurelin_rn"]}
    table = pa.table({"region": ["us", "us"], "_laurelin_rn": [7, 8]})
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "dedupe", "inputs": ["n0"], "params": {
            "keys": ["region"], "keep": "first",
            "order_by": [{"column": "_laurelin_rn", "dir": "asc"}]}},
    ]
    _compiled, rows = _run(_flow(nodes, "n1"), {"orders": table}, schemas)
    assert rows == [("us", 7)]


def test_the_preview_limit_is_bound_and_applied_only_at_the_previewed_node():
    """An aggregate over a limited upstream is not the build's aggregate.

    A preview that quietly answers a different question is worse than a slow
    one, so the limit never moves into an upstream CTE.
    """
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "aggregate", "inputs": ["n0"], "params": {
            "group_by": ["region"],
            "aggs": [{"fn": "sum", "column": "amount", "as": "total"}]}},
    ]
    compiled = compile_flow(_flow(nodes, "n1"), SCHEMAS, limit=5)
    assert compiled.sql.rstrip().endswith("LIMIT ?")
    assert compiled.params[-1] == 5
    # One LIMIT, at the end — not pushed into `_f0`.
    assert compiled.sql.count("LIMIT") == 1


def test_previewing_an_upstream_node_compiles_only_the_steps_that_feed_it():
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
            "predicate": {"t": "op", "op": "eq",
                          "args": [col("region"), lit("string", "us")]}}},
        {"id": "n2", "kind": "aggregate", "inputs": ["n1"], "params": {
            "group_by": ["region"],
            "aggs": [{"fn": "count_star", "as": "n"}]}},
    ]
    flow = _flow(nodes, "n2")
    upto = compile_flow(flow, SCHEMAS, upto="n1")
    assert upto.schema == SCHEMAS["orders"]
    assert "count(*)" not in upto.sql


def test_a_flow_over_a_schema_that_has_drifted_fails_with_our_sentence_not_a_binder_error():
    """Tier B runs against the LIVE schema, immediately before execution.

    A flow authored when `orders` had a `status` column must not silently
    compile after that column is dropped upstream — and must not surface
    DuckDB's binder message either.
    """
    flow = _flow(base_nodes(), "n1")  # filters on `region`
    assert compile_flow(flow, SCHEMAS).sql  # compiles today

    drifted = {"orders": ["status", "amount"]}  # `region` dropped upstream
    with pytest.raises(FlowRefused) as exc:
        compile_flow(flow, drifted)
    message = str(exc.value)
    assert "region" in message          # names the column that went missing
    assert "n1" in message              # and the step that wanted it
    assert "Binder Error" not in message


def test_a_compiled_flow_round_trips_through_json_unchanged():
    flow = _flow(base_nodes(), "n1")
    again = FlowDef.from_json(json.loads(json.dumps(flow.as_json())), name="out")
    assert compile_flow(again, SCHEMAS).sql == compile_flow(flow, SCHEMAS).sql
    assert again.as_json() == flow.as_json()


# ---------------------------------------------------------------------------
# The structural claim, guarded structurally
# ---------------------------------------------------------------------------

_COMPILER = "laurelin/transforms/flow_compile.py"


def _code_only(path: str) -> str:
    """The module's source with every docstring and comment removed.

    Needed because this module's *prose* is largely about escapers, dialects
    and `literal()` — explaining at length why none of them is present. A grep
    over the raw file would match the explanation and never the thing.
    """
    import ast
    import io
    import pathlib
    import tokenize

    source = pathlib.Path(path).read_text()
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)

    # Blank out the offending spans *in place* rather than re-joining tokens:
    # joining would glue or split identifiers and invent matches that are not
    # in the file (`.replace(` is three tokens).
    lines = source.splitlines(keepends=True)
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        drop = tok.type == tokenize.COMMENT
        if tok.type == tokenize.STRING and not drop:
            try:
                drop = ast.literal_eval(tok.string) in docstrings
            except (ValueError, SyntaxError):
                drop = False
        if not drop:
            continue
        (r1, c1), (r2, c2) = tok.start, tok.end
        for row in range(r1 - 1, r2):
            line = lines[row]
            start = c1 if row == r1 - 1 else 0
            end = c2 if row == r2 - 1 else len(line.rstrip("\n"))
            lines[row] = line[:start] + " " * (end - start) + line[end:]
    return "".join(lines)


def _all_leaves_are_constants(node) -> bool:
    """True for a constant, or a conditional whose branches are all constants.

    `out.add(" IN (" if op == "in" else " NOT IN (")` is exactly as safe as two
    separate literal calls; the test should not force it to be written that way.
    """
    import ast

    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.IfExp):
        return (_all_leaves_are_constants(node.body)
                and _all_leaves_are_constants(node.orelse))
    return False


def test_the_compiler_contains_no_function_that_turns_a_value_into_sql_text():
    """The property the module docstring claims, checked rather than asserted.

    "Our escaper is correct" is a claim that needs re-checking every time
    someone edits the file. "There is no escaper" is a claim a grep can hold.
    It is also what makes this compiler safe under a dialect like StarRocks,
    whose `literal()` raises by design: there is nothing here to call it from.

    The single `.replace` allowed is the identifier quoter, which runs only
    *after* `resolve_column` has established schema membership — so it never
    sees a string the data did not already contain.
    """
    code = _code_only(_COMPILER)

    assert code.count(".replace(") == 1, (
        "flow_compile grew a second string-rewriting call; if it is an "
        "escaper, bind the value instead."
    )
    assert 'replace(\'"\', \'""\')' in code  # …and it is the identifier quoter
    # Names an escaper would have. Checked against code with every docstring
    # and comment removed, because the module *prose* discusses `literal()` and
    # dialects at length — explaining why they are absent is not importing one.
    for forbidden in ("dialect", "literal(", "quote_value", "escape"):
        assert forbidden not in code, forbidden


def test_every_sql_fragment_is_either_a_literal_a_resolved_identifier_or_an_enum():
    """`_Sql.add` must never receive an author-supplied string.

    Every call site is either a string literal in the source, the output of
    `resolve_column` (schema membership), or a positional CTE name. A new call
    site that passes anything else is the defect this test exists to catch.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(_COMPILER).read_text())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "add"):
            continue
        # Only `_Sql.add` — `set.add` shares the name.
        if not (isinstance(fn.value, ast.Name) and fn.value.id in ("out", "sql")):
            continue
        arg = node.args[0]
        if _all_leaves_are_constants(arg):
            continue                                   # a literal in the source
        if isinstance(arg, ast.Call) and getattr(
            arg.func, "id", ""
        ) == "resolve_column":
            continue                                   # schema membership
        if isinstance(arg, ast.Subscript):
            continue                                   # a closed-enum lookup
        if isinstance(arg, ast.JoinedStr):
            # An f-string is allowed only if every interpolation is a CTE name.
            names = {
                n.id for v in arg.values
                if isinstance(v, ast.FormattedValue)
                for n in ast.walk(v) if isinstance(n, ast.Name)
            }
            if names <= {"cte_of", "terminal"}:
                continue
        offenders.append(ast.dump(arg)[:120])
    assert offenders == [], (
        "these `_Sql.add` calls pass something that is not a source literal, a "
        "resolved identifier or a closed enum: " + "; ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Identifiers a database cannot tell apart
#
# Every test below is a regression: each names a defect that was measured on
# this tree, reached through the builder's own dropdowns, and produced a
# *silently wrong answer* rather than an error.
# ---------------------------------------------------------------------------


def test_a_derived_column_cannot_take_a_name_the_engine_reads_as_an_existing_one():
    """A new column name must be new *to the database*, not to a Python list.

    Quoting does not make an identifier case-sensitive in DuckDB. Measured
    before this check: with a redact mask on `payroll.pay`, the flow
    `derive PAY = 'x'` then `select keep [PAY]` compiled to

        _f1 AS (SELECT "pay", "who", ? AS "PAY" FROM _f0),
        _f2 AS (SELECT "PAY" FROM _f1)

    `referenced_columns` saw `{'PAY'}`, the mask named `'pay'`, so governance
    allowed it at save, at preview and in the Builder — and DuckDB bound `"PAY"`
    to the first case-insensitive match, which is the masked source column. The
    build succeeded and published real salaries, world-readable.
    """
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "payroll"}},
        {"id": "n1", "kind": "derive", "inputs": ["n0"],
         "params": {"name": "PAY", "expr": lit("string", "x")}},
    ]
    with pytest.raises(FlowRefused) as exc:
        compile_flow(_flow(nodes, "n1"), {"payroll": ["pay", "who"]})
    assert "'PAY'" in str(exc.value) and "'pay'" in str(exc.value)


def test_a_rename_cannot_produce_two_columns_the_engine_reads_as_one_name():
    """The same defect through `rename`, which reaches it without inventing
    anything: rename an unrelated column to a case variant of a masked one."""
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "payroll"}},
        {"id": "n1", "kind": "rename", "inputs": ["n0"],
         "params": {"pairs": [{"from": "pay_date", "to": "Pay"}]}},
    ]
    with pytest.raises(FlowRefused) as exc:
        compile_flow(_flow(nodes, "n1"), {"payroll": ["pay", "pay_date"]})
    assert "'pay'" in str(exc.value) and "'Pay'" in str(exc.value)


@pytest.mark.parametrize("spelling", ["PAY", "Pay", "pay ", "ｐay"])
def test_confusable_new_names_are_refused_under_case_space_and_unicode_form(spelling):
    """One fold, shared with `permissions.confusable_identifier`.

    Case is the obvious one; trailing space and a compatibility-equivalent
    character are the same mistake wearing a different hat, and `permissions.py`
    had already paid for that lesson in `_reject_case_mismatch`. Using its fold
    here is what stops the compiler and the mask checker holding two different
    opinions of "the same name".
    """
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "payroll"}},
        {"id": "n1", "kind": "derive", "inputs": ["n0"],
         "params": {"name": spelling, "expr": lit("string", "x")}},
    ]
    with pytest.raises(FlowRefused):
        compile_flow(_flow(nodes, "n1"), {"payroll": ["pay", "who"]})


def test_a_source_holding_two_confusable_column_names_is_refused_not_guessed():
    """The invariant every reference below rests on: within one schema, a name
    resolves to exactly one column. A dataset genuinely holding both `pay` and
    `PAY` is refused — DuckDB cannot address the second one either."""
    nodes = [{"id": "n0", "kind": "source", "inputs": [],
              "params": {"dataset": "payroll"}}]
    with pytest.raises(FlowRefused) as exc:
        compile_flow(_flow(nodes, "n0"), {"payroll": ["pay", "PAY"]})
    assert "same name" in str(exc.value)


def test_an_aggregate_alias_cannot_collide_with_a_group_by_column_case_insensitively():
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "aggregate", "inputs": ["n0"], "params": {
            "group_by": ["region"],
            "aggs": [{"fn": "sum", "column": "amount", "as": "Region"}]}},
    ]
    with pytest.raises(FlowRefused):
        compile_flow(_flow(nodes, "n1"), SCHEMAS)


def test_dedupes_row_number_column_is_unique_against_a_case_variant_in_the_data():
    """Whoever supplies the data must not get to choose which row survives.

    `_unique_helper_column` extended only while the name matched *exactly*, so a
    real column called `_Laurelin_Rn` did not force an extension — and DuckDB
    then resolved the outer `WHERE "_laurelin_rn" = 1` to that data column
    instead of to `row_number()`. Measured, over rows (us,1,'real-cheapest'),
    (us,2,'ATTACKER-CHOSEN'), (us,3,'z') with `_Laurelin_Rn` = [7, 1, 7] and a
    keep-first-by-amount-ascending dedupe: the flow returned the row the data
    supplier had marked with a 1, and with 7s everywhere it published an empty
    dataset. Build status succeeded, no error anywhere.
    """
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "d"}},
        {"id": "n1", "kind": "dedupe", "inputs": ["n0"], "params": {
            "keys": ["region"],
            "order_by": [{"column": "amount", "dir": "asc"}],
            "keep": "first"}},
    ]
    schema = ["region", "amount", "note", "_Laurelin_Rn"]
    compiled = compile_flow(_flow(nodes, "n1"), {"d": schema})
    assert '"_laurelin_rn_"' in compiled.sql
    assert '"_laurelin_rn"' not in compiled.sql.replace('"_laurelin_rn_"', "")

    # And it really picks the cheapest row, not the one the data nominated.
    con = duckdb.connect()
    con.register("d", pa.table({
        "region": ["us", "us", "us"], "amount": [1, 2, 3],
        "note": ["real-cheapest", "ATTACKER-CHOSEN", "z"],
        "_Laurelin_Rn": [7, 1, 7],
    }))
    assert con.execute(compiled.sql, compiled.params).fetchall() == [
        ("us", 1, "real-cheapest", 7)
    ]


def test_a_join_of_a_step_to_itself_is_refused_with_our_sentence():
    """Nothing structural forbade `inputs: [n0, n0]` — arity is 2 and both key
    sides resolve — so it compiled to `FROM _f0 INNER JOIN _f0` and reached the
    author as DuckDB's `Ambiguous reference to table "_f0"` through the generic
    failure pipe."""
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "regions"}},
        {"id": "n1", "kind": "join", "inputs": ["n0", "n0"], "params": {
            "how": "inner",
            "keys": [{"left": "region_code", "right": "region_code"},
                     {"left": "region_name", "right": "region_name"}]}},
    ]
    with pytest.raises(FlowRefused) as exc:
        compile_flow(_flow(nodes, "n1"), SCHEMAS)
    assert "'n1'" in str(exc.value) and "itself" in str(exc.value)


def test_a_join_collision_names_every_clashing_column_and_offers_dropping_them():
    """Measured on this repo's own demo: joining `clean_flights` to
    `clean_aircraft` on `tail_number` — the shape `demo.py` writes in SQL — was
    refused over `status`, a column the pipeline never uses, and the remedy it
    named (rename) produces a worse result than dropping it. One collision at a
    time also meant one round trip per shared column."""
    schemas = {
        "l": ["tail_number", "status", "delay_minutes", "carrier"],
        "r": ["tail_number", "status", "carrier", "model"],
    }
    nodes = [
        {"id": "l", "kind": "source", "inputs": [], "params": {"dataset": "l"}},
        {"id": "r", "kind": "source", "inputs": [], "params": {"dataset": "r"}},
        {"id": "j", "kind": "join", "inputs": ["l", "r"], "params": {
            "how": "inner",
            "keys": [{"left": "tail_number", "right": "tail_number"}]}},
    ]
    with pytest.raises(FlowRefused) as exc:
        compile_flow(_flow(nodes, "j"), schemas)
    message = str(exc.value)
    assert "'status'" in message and "'carrier'" in message
    assert "'select' step" in message


# ---------------------------------------------------------------------------
# Types: the mistakes analysts actually make, caught before the build
# ---------------------------------------------------------------------------

KINDS_ORDERS = {"orders": {
    "region": "text", "status": "text", "amount": "number",
    "customer": "text", "placed_at": "time",
}}


def _typed(nodes, terminal):
    return compile_flow(
        _flow(nodes, terminal), SCHEMAS, column_kinds=KINDS_ORDERS,
    )


def test_totalling_a_column_of_text_is_refused_at_compile_time_naming_the_column():
    """The single commonest first mistake, and it used to SAVE.

    Measured before this check: `PUT` returned 200, `GET /flows/{name}` reported
    `error: null` — the flow declared itself healthy — and the build then failed
    with `{"code": "transform_failed", "driver": "python", "exc_class":
    "BinderException"}` and no message field at all, which the UI renders as
    "Laurelin's own code raised … the traceback is in the server log". To an
    analyst who has never written code, about their own mistake, with no access
    to a server log.
    """
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "aggregate", "inputs": ["n0"], "params": {
            "group_by": [], "aggs": [{"fn": "sum", "column": "status",
                                      "as": "total"}]}},
    ]
    with pytest.raises(FlowRefused) as exc:
        _typed(nodes, "n1")
    message = str(exc.value)
    assert "'status'" in message and "text" in message
    assert "Number of rows with a value" in message


def test_counting_a_column_of_text_is_not_refused():
    """Precision matters as much as the refusal: `count`, `min`, `max` and
    `any_value` work on text, and narrowing them would refuse something the
    engine happily runs."""
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "aggregate", "inputs": ["n0"], "params": {
            "group_by": [], "aggs": [{"fn": "count", "column": "status",
                                      "as": "n"}]}},
    ]
    assert _typed(nodes, "n1").schema == ["n"]


def test_comparing_a_text_column_with_a_date_is_refused_naming_the_conversion():
    """The case seen live in the browser: "scheduled_departure is at least
    <Date>" over a VARCHAR column. It previewed as `400 A column referenced does
    not exist on the remote system` — false in three ways at once."""
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
            "predicate": {"t": "op", "op": "gte", "args": [
                col("status"), lit("date", "2026-01-10")]}}},
    ]
    with pytest.raises(FlowRefused) as exc:
        _typed(nodes, "n1")
    assert "'status'" in str(exc.value) and "'cast' step" in str(exc.value)


def test_a_comparison_against_the_right_kind_of_value_is_not_refused():
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
            "predicate": {"t": "op", "op": "gte", "args": [
                col("placed_at"), lit("date", "2026-01-10")]}}},
    ]
    assert _typed(nodes, "n1").params == [__import__("datetime").date(2026, 1, 10)]


def test_arithmetic_on_a_text_column_is_refused():
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "derive", "inputs": ["n0"], "params": {
            "name": "z", "expr": {"t": "op", "op": "add", "args": [
                col("region"), lit("bigint", 1)]}}},
    ]
    with pytest.raises(FlowRefused) as exc:
        _typed(nodes, "n1")
    assert "'region'" in str(exc.value)


def test_a_cast_updates_the_kind_so_the_step_after_it_is_allowed():
    """The remedy the refusals name has to actually work."""
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "cast", "inputs": ["n0"],
         "params": {"column": "status", "to": "bigint"}},
        {"id": "n2", "kind": "aggregate", "inputs": ["n1"], "params": {
            "group_by": [], "aggs": [{"fn": "sum", "column": "status",
                                      "as": "total"}]}},
    ]
    assert _typed(nodes, "n2").schema == ["total"]


def test_median_is_an_aggregate_and_is_refused_over_a_text_column_at_compile():
    """Parity with the ontology aggregate, which has offered `median` since
    aggregations shipped — an object panel could chart one and a dataset panel
    could not. Behind the same numeric gate as `sum`/`avg`: a median over text
    is a BinderException at run, and this is a sentence at compile instead."""
    def agg(column):
        return [
            {"id": "n0", "kind": "source", "inputs": [],
             "params": {"dataset": "orders"}},
            {"id": "n1", "kind": "aggregate", "inputs": ["n0"], "params": {
                "group_by": ["region"],
                "aggs": [{"fn": "median", "column": column, "as": "mid"}]}},
            {"id": "n2", "kind": "sort", "inputs": ["n1"], "params": {
                "by": [{"column": "region", "dir": "asc", "nulls": "last"}]}},
        ]

    with pytest.raises(FlowRefused) as exc:
        _typed(agg("status"), "n2")
    message = str(exc.value)
    assert "'status'" in message and "median" in message and "text" in message

    # And over a number it compiles, runs, and answers as DuckDB's median.
    assert _typed(agg("amount"), "n2").kinds["mid"] == "number"
    _compiled, rows = _run(_flow(agg("amount"), "n2"), {"orders": ORDERS})
    assert rows == [("eu", 12.5), ("us", 54.5)]


def test_floor_compiles_from_the_closed_vocabulary_and_takes_exactly_one_argument():
    def derive(args):
        return [
            {"id": "n0", "kind": "source", "inputs": [],
             "params": {"dataset": "orders"}},
            {"id": "n1", "kind": "derive", "inputs": ["n0"], "params": {
                "name": "f", "expr": {"t": "op", "op": "floor", "args": args}}},
        ]

    with pytest.raises(FlowRefused) as exc:  # Tier A arity, our sentence
        _flow(derive([col("amount"), lit("bigint", 2)]), "n1")
    assert "1 argument" in str(exc.value)

    with pytest.raises(FlowRefused):  # the numeric gate `abs`/`round` share
        _typed(derive([col("status")]), "n1")

    compiled = compile_flow(_flow(derive([col("amount")]), "n1"), SCHEMAS)
    assert "floor(" in compiled.sql  # the compiler's own keyword, never a value
    assert compiled.kinds["f"] == "number"


def test_a_histogram_bin_is_a_pure_function_of_the_flow_shape_and_bins_correctly():
    """The Explore "Histogram" preset: bin = floor(col / width) * width as a
    `derive`, grouped. The width is an author value, bound in both positions —
    two widths compile to byte-identical SQL — and the binning happens inside
    the governed statement, never in a client over raw rows."""
    def hist(width):
        return _flow([
            {"id": "n0", "kind": "source", "inputs": [],
             "params": {"dataset": "orders"}},
            {"id": "n1", "kind": "derive", "inputs": ["n0"], "params": {
                "name": "bin",
                "expr": {"t": "op", "op": "mul", "args": [
                    {"t": "op", "op": "floor", "args": [
                        {"t": "op", "op": "div",
                         "args": [col("amount"), lit("bigint", width)]}]},
                    lit("bigint", width)]}}},
            {"id": "n2", "kind": "aggregate", "inputs": ["n1"], "params": {
                "group_by": ["bin"],
                "aggs": [{"fn": "count_star", "as": "n"}]}},
            {"id": "n3", "kind": "sort", "inputs": ["n2"], "params": {
                "by": [{"column": "bin", "dir": "asc", "nulls": "last"}]}},
        ], "n3")

    ten, fifty = compile_flow(hist(10), SCHEMAS), compile_flow(hist(50), SCHEMAS)
    assert ten.sql == fifty.sql
    assert ten.params == [10, 10] and fifty.params == [50, 50]

    _compiled, rows = _run(hist(50), {"orders": ORDERS})
    # amounts [10, 99, 20, 5] with width 50 -> bins 0 (three) and 50 (one).
    assert rows == [(0, 3), (50, 1)]


def test_a_flow_compiled_without_type_information_keeps_every_other_check():
    """`column_kinds` is optional, and omitting it must only *lose* type checks
    — never change the SQL, and never skip an identifier check."""
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [],
         "params": {"dataset": "orders"}},
        {"id": "n1", "kind": "aggregate", "inputs": ["n0"], "params": {
            "group_by": [], "aggs": [{"fn": "sum", "column": "status",
                                      "as": "total"}]}},
    ]
    untyped = compile_flow(_flow(nodes, "n1"), SCHEMAS)
    assert untyped.schema == ["total"]


# ---------------------------------------------------------------------------
# Three-valued logic, in a product whose users do not know it exists
# ---------------------------------------------------------------------------


def test_is_not_keeps_rows_whose_value_is_empty():
    """"is not" is what the screen says, and it must mean what it says.

    Measured over a column holding [null, null, null, 5]: "keep rows where v is
    not 5" returned **0 rows** — correct SQL's `<>`, and the opposite of the
    sentence on screen to anyone who does not write SQL. Silently: an empty
    published dataset with a succeeded build.
    """
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [], "params": {"dataset": "d"}},
        {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
            "predicate": {"t": "op", "op": "ne", "args": [
                col("v"), lit("bigint", 5)]}}},
    ]
    compiled = compile_flow(_flow(nodes, "n1"), {"d": ["v"]})
    con = duckdb.connect()
    con.register("d", pa.table({"v": pa.array([None, None, None, 5, 7],
                                              type=pa.int64())}))
    assert con.execute(compiled.sql, compiled.params).fetchall() == [
        (None,), (None,), (None,), (7,)
    ]


def test_is_not_one_of_keeps_rows_whose_value_is_empty():
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [], "params": {"dataset": "d"}},
        {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
            "predicate": {"t": "op", "op": "not_in", "args": [
                col("v"), lit("bigint", 5), lit("bigint", 7)]}}},
    ]
    compiled = compile_flow(_flow(nodes, "n1"), {"d": ["v"]})
    # The column is emitted once, so its parameters are bound once.
    assert compiled.params == [5, 7]
    con = duckdb.connect()
    con.register("d", pa.table({"v": pa.array([None, 5, 7, 9], type=pa.int64())}))
    assert con.execute(compiled.sql, compiled.params).fetchall() == [(None,), (9,)]


def test_is_still_excludes_rows_whose_value_is_empty():
    """The other direction is left alone: nobody expects "v is 5" to return the
    empties, and "is empty" is the explicit way to ask."""
    nodes = [
        {"id": "n0", "kind": "source", "inputs": [], "params": {"dataset": "d"}},
        {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
            "predicate": {"t": "op", "op": "eq", "args": [
                col("v"), lit("bigint", 5)]}}},
    ]
    compiled = compile_flow(_flow(nodes, "n1"), {"d": ["v"]})
    con = duckdb.connect()
    con.register("d", pa.table({"v": pa.array([None, 5, 7], type=pa.int64())}))
    assert con.execute(compiled.sql, compiled.params).fetchall() == [(5,)]
