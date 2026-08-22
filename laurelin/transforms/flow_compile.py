"""Compile a validated :class:`FlowDef` into one SQL statement plus parameters.

The invariant this module exists to hold, in one sentence:

    **The compiled SQL text is a pure function of the flow's structure — node
    kinds, enum choices, and identifiers that were resolved against a live
    schema — and contains not one byte derived from any author-supplied value.
    Every value travels in the parameter list.**

``tests/test_flow_compile.py`` asserts exactly that, by compiling two flows that
differ only in their literal values and requiring the two SQL strings to be
*identical* while the parameter lists differ. Everything else in that file is
coverage; that one assertion is the invariant.

Why binding and not escaping
----------------------------
The brief asks for a compiler that is safe under ``StarRocksDialect``, whose
``literal()`` raises by design because stacked statements execute there and an
escaper defect is a remote write primitive. Measured on this tree, transform
SQL never reaches ``laurelin/core/dialects.py`` at all — it executes only in
DuckDB, via ``duckdb.connect()`` + ``con.register(alias, scan)``. So the
constraint as literally stated guards a path that does not exist today.

This module honours its *intent* instead, and more strictly: **it contains no
function that converts a value to SQL text.** No ``literal()``, no escaper, no
quote-doubling on a value. There is nothing here for a StarRocks-style dialect
to be unsafe *with*, which is a stronger property than "our escaper is
correct". If flow SQL is ever pushed down to a remote engine, the IR is already
dialect-neutral (structure + enums + typed values) and only this file grows a
second renderer — the seam ``dialects.py`` exists for.

Three positions cannot be bound, all measured against DuckDB on this tree:

* ``SELECT ? AS c FROM r`` with ``["a"]`` returns the **constant** ``'a'`` on
  every row, not column ``a``. A silent wrong answer, not an error — which is
  why identifier positions get *schema membership*, never a placeholder and
  never a regex.
* ``SELECT CAST(a AS ?)`` → ``ParserException``. Cast targets come from
  ``CAST_TYPES``.
* ``SELECT a FROM r ORDER BY ?`` → ``Parameter not supported in ORDER BY
  clause``. Sort columns are identifier positions.

``LIMIT ?`` and ``HAVING … > ?`` *do* bind, and are bound.

Every non-bindable position is therefore either a **closed enum** (a keyword
this module already contains) or an **identifier** (resolved by
:func:`resolve_column`). There is no third category.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from laurelin.core.permissions import confusable_identifier
from laurelin.transforms.flow_ir import (
    AGG_FNS,
    BOOLEAN_OPS,
    CAST_TYPES,
    JOIN_HOWS,
    NULLS_PLACEMENT,
    SORT_DIRS,
    SORT_DIRS_REVERSED,
    FlowDef,
    FlowNode,
    FlowRefused,
)


@dataclass
class CompiledFlow:
    sql: str
    params: list
    #: Dataset names this statement reads, as registered table aliases.
    inputs: list[str]
    #: Column names the statement produces, in order.
    schema: list[str]
    #: Column name -> one of `KINDS`, for the columns whose kind is known.
    #: Consumed by the builder's column pickers so that "Total of" is not
    #: offered for a column of names.
    kinds: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Types, to the coarse degree an analyst reasons in
# ---------------------------------------------------------------------------
#
# The IR carried no type model at all, and the bill for that was measured: an
# analyst who asked for the total of a text column got a flow that SAVED
# cleanly, previewed as `400 A column referenced does not exist on the remote
# system` — naming nothing, about a column visibly present in the picker, and
# on a query that ran in embedded DuckDB with no remote system anywhere — and
# then failed its build as "Laurelin's own code raised … the traceback is in
# the server log", to a person who has neither code nor a server.
#
# Four kinds is the whole model, deliberately. It is enough to catch every
# mistake in that paragraph before the flow is saved, it is expressible in the
# UI ("Total of" is greyed out for text), and it is small enough that an
# unknown type simply yields "" and no opinion — the compiler never refuses a
# flow because it failed to understand a type.

KINDS: tuple[str, ...] = ("number", "text", "boolean", "time")

#: Plain words for a refusal sentence. An analyst does not know what
#: `large_utf8` is, and does not need to.
KIND_WORDS: dict[str, str] = {
    "number": "numbers", "text": "text", "boolean": "true/false values",
    "time": "dates or times",
}

_NUMBER_PREFIXES = (
    "int", "uint", "float", "double", "decimal", "halffloat", "half_float",
)
_TEXT_PREFIXES = ("string", "large_string", "utf8", "large_utf8")
_TIME_PREFIXES = ("date", "timestamp", "time", "duration", "interval")


def kind_of(arrow_type: Optional[str]) -> str:
    """Coarse kind of an Arrow type name, or ``""`` when we have no opinion.

    ``""`` is not a failure mode, it is the design: a nested, dictionary or
    extension type yields no kind, every type check below is skipped for it,
    and the flow behaves exactly as it did before this model existed.
    """
    t = (arrow_type or "").strip().lower()
    if not t:
        return ""
    if t.startswith("bool"):
        return "boolean"
    if t.startswith(_NUMBER_PREFIXES):
        return "number"
    if t.startswith(_TEXT_PREFIXES):
        return "text"
    if t.startswith(_TIME_PREFIXES):
        return "time"
    return ""


#: `cast.to` -> the kind the column has afterwards. Keyed on `CAST_TYPES`.
_CAST_KIND: dict[str, str] = {
    "varchar": "text", "bigint": "number", "double": "number",
    "boolean": "boolean", "date": "time", "timestamp": "time",
}

#: Literal type -> kind, for comparing a value against a column.
_LIT_KIND: dict[str, str] = {
    "string": "text", "bigint": "number", "double": "number",
    "boolean": "boolean", "date": "time", "timestamp": "time",
    "null": "",  # NULL compares against anything
}


def _refuse_kind(
    *, node: str, field: str, column: str, actual: str, wanted: str, doing: str,
    remedy: str,
) -> "FlowRefused":
    return FlowRefused(
        f"Step {node!r} {doing} the column {column!r}, which holds "
        f"{KIND_WORDS[actual]}, not {KIND_WORDS[wanted]}. {remedy}",
        node=node, field=field,
    )


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def _quote(name: str) -> str:
    """DuckDB's exact identifier rule: wrap in double quotes, double any inner.

    Only ever called on a string that has *already* passed a membership check
    (`resolve_column`) or an invented-identifier pattern check (Tier A). The
    quoter never sees a string the schema did not already contain, so it is not
    a security boundary and is not asked to be one.
    """
    return '"' + name.replace('"', '""') + '"'


def _collides(name: str, schema: list[str]) -> Optional[str]:
    """The column of ``schema`` a query engine would confuse ``name`` with.

    Quoting an identifier does **not** make it case-sensitive in DuckDB:
    ``SELECT "pay", ? AS "PAY"`` produces two columns, and a later
    ``SELECT "PAY"`` binds to the *first* one — silently, with no error. So a
    name this compiler believes is new can be a name the engine believes it
    already has.

    Measured, and this is why the function exists: with a redact mask on
    ``payroll.pay``, a flow of ``derive PAY = 'x'`` then ``select keep [PAY]``
    passed every governance check (`referenced_columns` saw ``{'PAY'}``, the
    mask named ``'pay'``) and built a world-readable dataset of real salaries.
    Two dropdowns, no hostile string, no admin involved.

    Folded with `permissions.confusable_identifier` rather than `str.lower`, so
    the compiler and the mask checker cannot drift into two opinions of what
    "the same name" means — see that constant for why a fold wider than
    DuckDB's own is the safe direction.
    """
    folded = confusable_identifier(name)
    for existing in schema:
        if confusable_identifier(existing) == folded:
            return existing
    return None


def _reject_confusable_schema(schema: list[str], *, node: str, field: str) -> None:
    """Refuse a schema holding two names an engine cannot tell apart.

    Enforced on every schema the compiler carries — at each `source` and after
    every node that introduces a name — so that the invariant every other rule
    here rests on holds by construction: **within one schema, a column name
    resolves to exactly one column.** Without it, `resolve_column`'s membership
    check is answering a question about a Python list rather than about what
    the engine will bind.

    A dataset that genuinely contains both ``pay`` and ``PAY`` is refused. That
    is not a loss: DuckDB cannot address the second one either.
    """
    seen: dict[str, str] = {}
    for column in schema:
        folded = confusable_identifier(column)
        if folded in seen:
            raise FlowRefused(
                f"Step {node!r} would produce two columns, {seen[folded]!r} and "
                f"{column!r}, that a database reads as the same name. Rename or "
                "drop one of them — a query cannot tell them apart, so which "
                "one you got would be luck.",
                node=node, field=field,
            )
        seen[folded] = column


def resolve_column(
    name: str, schema: list[str], *, node: str, field: str
) -> str:
    """Tier B: an identifier is legal iff the data really has that column.

    Membership, **not** a pattern. A regex would fail in both directions:

    * it would admit names that do not exist in the data, so a typo becomes a
      DuckDB binder error rather than our sentence; and
    * it would reject real column names that do — ``a"b``, ``a`b``, and
      ``total (USD)`` are all legal Parquet column names, and an analyst whose
      spreadsheet upload produced one would simply be unable to use the
      builder.

    Quoting happens only after membership succeeds. That ordering is the whole
    safety argument for identifier positions, and
    ``test_a_column_whose_real_name_contains_a_double_quote_is_addressed_exactly``
    is the case that must *work* rather than be refused.
    """
    if name not in schema:
        raise FlowRefused(
            f"Step {node!r} refers to a column named {name!r}, which its input "
            f"does not have. Available columns: "
            f"{', '.join(repr(c) for c in schema[:20])}"
            + (f" (and {len(schema) - 20} more)" if len(schema) > 20 else "")
            + ".",
            node=node, field=field,
        )
    return _quote(name)


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


class _Sql:
    """Accumulates SQL text and its parameters together.

    Keeping them in one object is what makes ordinal alignment *structural*
    rather than something a reviewer has to verify: a placeholder cannot be
    emitted without appending its value in the same call.

    ``add`` takes text this module authored — a literal in the source below, a
    keyword out of a closed enum, or the output of ``resolve_column``. It never
    takes an author-supplied string. ``bind`` is the only way an author's value
    enters a statement, and it enters as a parameter.
    """

    __slots__ = ("_parts", "params")

    def __init__(self) -> None:
        self._parts: list[str] = []
        self.params: list = []

    def add(self, fragment: str) -> "_Sql":
        self._parts.append(fragment)
        return self

    def bind(self, value: Any) -> "_Sql":
        self._parts.append("?")
        self.params.append(value)
        return self

    def join(self, fragments: list[str], sep: str) -> "_Sql":
        self._parts.append(sep.join(fragments))
        return self

    def text(self) -> str:
        return "".join(self._parts)


def _cte_name(index: int) -> str:
    """CTE names are positional, never the author's node id.

    Node ids are pattern-validated in Tier A, but naming CTEs positionally
    means that even a defect in that pattern could not put an author byte into
    the statement. Cheap, and it removes a whole class of position from the
    hostile matrix.
    """
    return f"_f{index}"


def _unique_helper_column(schema: list[str], base: str) -> str:
    """A column name for the compiler's own use that cannot collide.

    ``dedupe`` needs a row-number column, and a real Parquet column may
    genuinely be called ``_rn``. Extending until unique is deterministic and
    keeps the compiled SQL a pure function of (structure, schema).

    Uniqueness is `_collides`, not ``in``, and the difference was measured: a
    dataset with a column called ``_Laurelin_Rn`` is not an exact match for
    ``_laurelin_rn``, so no extension happened, and DuckDB then bound the outer
    ``WHERE "_laurelin_rn" = 1`` to the *data* column instead of to
    ``row_number()``. Whoever supplied the data chose which row survived the
    dedupe — putting a 1 in that column elected a row, and putting 7 everywhere
    published an empty dataset. Build status: succeeded, silently, both times.
    """
    name = base
    while _collides(name, schema) is not None:
        name += "_"
    return name


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------

#: Infix operators: op -> the SQL token. Compiler-owned text, selected by a
#: key that Tier A already checked against `OPS`.
_BINARY: dict[str, str] = {
    "eq": " = ",
    # `IS DISTINCT FROM`, not `<>`, and this is a deliberate departure from
    # SQL's three-valued logic.
    #
    # The UI renders this operator as the words "is not". Measured over a
    # column holding [null, null, null, 5], "keep rows where v is not 5"
    # returned **0 rows** — correct SQL, and the opposite of what the sentence
    # on the screen says to everyone who is not a SQL programmer. It is also
    # silent: an empty dataset with a succeeded build, not an error.
    #
    # This product's premise is that its users do not know SQL, so the operator
    # matches its own label: `NULL IS DISTINCT FROM 5` is true, and the three
    # empty rows come back. "is" (`eq`) is left alone — nobody expects "v is 5"
    # to return the empties — and "is empty" / "is not empty" remain the
    # explicit way to ask about them.
    "ne": " IS DISTINCT FROM ",
    "lt": " < ", "lte": " <= ",
    "gt": " > ", "gte": " >= ",
    "add": " + ", "sub": " - ", "mul": " * ", "div": " / ",
}
_VARIADIC: dict[str, str] = {"and": " AND ", "or": " OR "}
_POSTFIX: dict[str, str] = {"is_null": " IS NULL", "is_not_null": " IS NOT NULL"}
_FUNCS: dict[str, str] = {
    "coalesce": "coalesce", "upper": "upper", "lower": "lower",
    "trim": "trim", "length": "length", "abs": "abs", "round": "round",
    "concat": "concat", "floor": "floor",
}


#: Ops that require every `col` operand to hold numbers, and the verb used in
#: the refusal. Chosen from the mistakes that were actually measured, not from
#: everything DuckDB is fussy about: an over-eager checker that refuses a flow
#: the engine would have run is a worse product than one that lets a rare
#: oddity through to a build error.
_NUMBER_OPS: dict[str, str] = {
    "add": "adds", "sub": "subtracts from", "mul": "multiplies",
    "div": "divides", "abs": "takes the size of", "round": "rounds",
    "floor": "rounds down",
}


def _expr_kind(expr: dict, kinds: dict[str, str]) -> str:
    """The kind an expression evaluates to, or ``""`` when unknown."""
    t = expr["t"]
    if t == "col":
        return kinds.get(expr["name"], "")
    if t == "lit":
        return _LIT_KIND.get(expr["type"], "")
    op = expr["op"]
    if op in BOOLEAN_OPS:
        return "boolean"
    if op in _NUMBER_OPS or op == "length":
        return "number"
    if op in ("upper", "lower", "trim", "concat"):
        return "text"
    if op == "date_trunc":
        return "time"
    if op in ("if_else", "coalesce"):
        # Whatever the branches agree on; nothing if they disagree.
        branches = expr["args"][1:] if op == "if_else" else expr["args"]
        seen = {_expr_kind(a, kinds) for a in branches}
        seen.discard("")
        return seen.pop() if len(seen) == 1 else ""
    return ""


def _check_expr_types(
    expr: dict, kinds: dict[str, str], *, node: str, field: str
) -> None:
    """Refuse the type mistakes an analyst actually makes, before any SQL runs.

    Every one of these was reachable through the builder's own dropdowns and
    surfaced as driver text or as a failed build. Checking here means the
    author is told, at the moment they choose the column, which column and
    what to do instead.

    Only `col` operands are judged. A literal's type was declared by the author
    and coerced by Tier A, so a mismatch between two literals is not a thing
    that can happen; a mismatch between a column and a literal is the case
    below.
    """
    if expr["t"] != "op":
        return
    op, args = expr["op"], expr["args"]
    for a in args:
        _check_expr_types(a, kinds, node=node, field=field)

    if op in _NUMBER_OPS:
        for a in args:
            k = _expr_kind(a, kinds)
            if a["t"] == "col" and k in ("text", "boolean"):
                raise _refuse_kind(
                    node=node, field=field, column=a["name"], actual=k,
                    wanted="number", doing=_NUMBER_OPS[op],
                    remedy=(
                        "Add a 'cast' step converting it to a number first, "
                        "or pick a different column."
                    ),
                )
        return

    if op == "like":
        target = args[0]
        k = _expr_kind(target, kinds)
        if target["t"] == "col" and k and k != "text":
            raise _refuse_kind(
                node=node, field=field, column=target["name"], actual=k,
                wanted="text", doing="matches text against",
                remedy="'Contains' only works on text columns.",
            )
        return

    if op == "date_trunc":
        target = args[1]
        k = _expr_kind(target, kinds)
        if target["t"] == "col" and k and k != "time":
            raise _refuse_kind(
                node=node, field=field, column=target["name"], actual=k,
                wanted="time", doing="rounds to a date unit",
                remedy=(
                    "Add a 'cast' step converting it to a date or a date "
                    "and time first."
                ),
            )
        return

    if op in ("eq", "ne", "lt", "lte", "gt", "gte") and len(args) == 2:
        left, right = args
        for a, b in ((left, right), (right, left)):
            if a["t"] != "col":
                continue
            ka, kb = _expr_kind(a, kinds), _expr_kind(b, kinds)
            if not ka or not kb or ka == kb:
                continue
            # A number and a date are both orderable and DuckDB will not cast
            # one to the other; text against either is the case people hit.
            raise FlowRefused(
                f"Step {node!r} compares the column {a['name']!r}, which holds "
                f"{KIND_WORDS[ka]}, with {KIND_WORDS[kb]}. "
                + (
                    "Add a 'cast' step converting the column first, or "
                    "compare it with a text value."
                    if ka == "text" else
                    f"Give it a value that is {KIND_WORDS[ka]}."
                ),
                node=node, field=field,
            )


def _compile_expr(
    expr: dict, out: _Sql, schema: list[str], *, node: str, field: str
) -> None:
    t = expr["t"]
    if t == "col":
        out.add(resolve_column(expr["name"], schema, node=node, field=field))
        return
    if t == "lit":
        # The one place an author's value enters a statement, and it enters as
        # a parameter. Already coerced to a typed Python object by Tier A, so
        # DuckDB is never handed a string to reinterpret.
        out.bind(expr["value"])
        return

    op = expr["op"]
    args = expr["args"]

    def arg(i: int) -> None:
        _compile_expr(args[i], out, schema, node=node, field=field)

    if op in _VARIADIC:
        out.add("(")
        for i in range(len(args)):
            if i:
                out.add(_VARIADIC[op])
            arg(i)
        out.add(")")
        return
    if op == "not":
        out.add("(NOT ")
        arg(0)
        out.add(")")
        return
    if op in _BINARY:
        out.add("(")
        arg(0)
        out.add(_BINARY[op])
        arg(1)
        out.add(")")
        return
    if op in _POSTFIX:
        out.add("(")
        arg(0)
        out.add(_POSTFIX[op])
        out.add(")")
        return
    if op in ("in", "not_in"):
        # "is not one of" keeps empty values, for the same reason `ne` does
        # above. Written as `NOT coalesce(x IN (…), false)` rather than
        # `x IS NULL OR x NOT IN (…)` so that `x` is emitted — and therefore
        # its parameters bound — exactly once.
        out.add("(NOT coalesce(" if op == "not_in" else "(")
        arg(0)
        out.add(" IN (")
        for i in range(1, len(args)):
            if i > 1:
                out.add(", ")
            arg(i)  # each element is a `lit`, so each is bound individually
        out.add("), false))" if op == "not_in" else "))")
        return
    if op == "like":
        # The pattern's % and _ are DATA. Binding it means a wildcard is a
        # wildcard and nothing else in the pattern is ever interpreted as SQL.
        out.add("(")
        arg(0)
        out.add(" LIKE ")
        arg(1)
        out.add(")")
        return
    if op == "if_else":
        out.add("(CASE WHEN ")
        arg(0)
        out.add(" THEN ")
        arg(1)
        out.add(" ELSE ")
        arg(2)
        out.add(" END)")
        return
    if op == "date_trunc":
        # The unit is a DuckDB *string literal*, so it is bound — and Tier A
        # also restricted it to DATE_TRUNC_UNITS. Belt and braces: bound so a
        # value cannot become syntax, enum-checked so a typo is our sentence.
        out.add("date_trunc(")
        arg(0)
        out.add(", ")
        arg(1)
        out.add(")")
        return
    if op in _FUNCS:
        out.add(_FUNCS[op])
        out.add("(")
        for i in range(len(args)):
            if i:
                out.add(", ")
            arg(i)
        out.add(")")
        return
    # Unreachable: Tier A rejects any op outside OPS, and every OPS member is
    # handled above. Fail closed rather than emit nothing.
    raise FlowRefused(
        f"Operation {op!r} on step {node!r} cannot be compiled.",
        node=node, field=field,
    )


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


@dataclass
class _NodeSql:
    body: str
    schema: list[str]
    #: Column -> kind, for the columns whose kind is known. Propagated through
    #: every node so a type check late in a flow still knows what an early
    #: `cast` or `derive` produced.
    kinds: dict[str, str] = field(default_factory=dict)
    #: Set only by `sort`. Re-emitted at the outer SELECT when the terminal
    #: node is a sort, because an ORDER BY inside a CTE is not a guaranteed
    #: total order once the CTE is selected from again.
    order_by: str = ""


def _compile_node(
    node: FlowNode,
    inputs: list[_NodeSql],
    input_ctes: list[str],
    out: _Sql,
    schemas: Mapping[str, list[str]],
    column_kinds: Mapping[str, Mapping[str, str]],
) -> _NodeSql:
    kind = node.kind
    nid = node.id
    p = node.params

    if kind == "source":
        dataset = p["dataset"]
        if dataset not in schemas:
            # Tier B for a dataset name. Membership in the *live* dataset list,
            # exactly as for a column, so a source name is never pattern-
            # matched into a table reference.
            raise FlowRefused(
                f"Step {nid!r} reads dataset {dataset!r}, which does not exist "
                "or is not readable here.",
                node=nid, field="dataset",
            )
        schema = list(schemas[dataset])
        # Establishes the invariant every reference below depends on: within
        # one schema, a name resolves to exactly one column.
        _reject_confusable_schema(schema, node=nid, field="dataset")
        # Alias == dataset name is the mechanism that lets preview and build
        # run the SAME string: Builder registers inputs by alias, and
        # catalog.query registers datasets by name.
        return _NodeSql(
            body=f"SELECT * FROM {_quote(dataset)}", schema=schema,
            kinds=dict(column_kinds.get(dataset, {})),
        )

    src = inputs[0]
    src_cte = input_ctes[0]
    schema = src.schema
    kinds = src.kinds

    if kind == "filter":
        _check_expr_types(p["predicate"], kinds, node=nid, field="predicate")
        w = _Sql()
        _compile_expr(p["predicate"], w, schema, node=nid, field="predicate")
        out.params.extend(w.params)
        return _NodeSql(
            body=f"SELECT * FROM {src_cte} WHERE {w.text()}", schema=list(schema),
            kinds=dict(kinds),
        )

    if kind == "select":
        wanted = p["columns"]
        for c in wanted:
            resolve_column(c, schema, node=nid, field="columns")
        if p["mode"] == "keep":
            kept = [c for c in schema if c in set(wanted)]
        else:
            kept = [c for c in schema if c not in set(wanted)]
        if not kept:
            raise FlowRefused(
                f"Step {nid!r} would remove every column. Keep at least one.",
                node=nid, field="columns",
            )
        cols = ", ".join(_quote(c) for c in kept)
        return _NodeSql(
            body=f"SELECT {cols} FROM {src_cte}", schema=kept,
            kinds={c: kinds[c] for c in kept if c in kinds},
        )

    if kind == "rename":
        mapping = {pair["from"]: pair["to"] for pair in p["pairs"]}
        for src_col in mapping:
            resolve_column(src_col, schema, node=nid, field="pairs")
        new_schema = [mapping.get(c, c) for c in schema]
        _reject_confusable_schema(new_schema, node=nid, field="pairs")
        parts = [
            f"{_quote(c)} AS {_quote(mapping[c])}" if c in mapping else _quote(c)
            for c in schema
        ]
        return _NodeSql(
            body=f"SELECT {', '.join(parts)} FROM {src_cte}", schema=new_schema,
            kinds={mapping.get(c, c): k for c, k in kinds.items()},
        )

    if kind == "derive":
        name = p["name"]
        clash = _collides(name, schema)
        if clash is not None:
            raise FlowRefused(
                f"Step {nid!r} would add a column named {name!r}, but its "
                f"input already has one called {clash!r}"
                + (
                    " — and a database reads those two as the same name."
                    if clash != name else "."
                )
                + " Pick another name.",
                node=nid, field="name",
            )
        _check_expr_types(p["expr"], kinds, node=nid, field="expr")
        w = _Sql()
        _compile_expr(p["expr"], w, schema, node=nid, field="expr")
        out.params.extend(w.params)
        cols = ", ".join(_quote(c) for c in schema)
        return _NodeSql(
            body=f"SELECT {cols}, {w.text()} AS {_quote(name)} FROM {src_cte}",
            schema=[*schema, name],
            kinds=dict(kinds) | {name: _expr_kind(p["expr"], kinds)},
        )

    if kind == "cast":
        column = p["column"]
        quoted = resolve_column(column, schema, node=nid, field="column")
        target = CAST_TYPES[p["to"]]  # closed enum -> a keyword we own
        parts = [
            f"CAST({quoted} AS {target}) AS {quoted}" if c == column else _quote(c)
            for c in schema
        ]
        return _NodeSql(
            body=f"SELECT {', '.join(parts)} FROM {src_cte}", schema=list(schema),
            kinds=dict(kinds) | {column: _CAST_KIND[p["to"]]},
        )

    if kind == "aggregate":
        group_by = p["group_by"]
        for c in group_by:
            resolve_column(c, schema, node=nid, field="group_by")
        select_parts = [_quote(c) for c in group_by]
        new_kinds = {c: kinds[c] for c in group_by if c in kinds}
        for a in p["aggs"]:
            alias = _quote(a["as"])
            if a["fn"] == "count_star":
                select_parts.append(f"count(*) AS {alias}")
                new_kinds[a["as"]] = "number"
                continue
            col = resolve_column(a["column"], schema, node=nid, field="aggs")
            source_kind = kinds.get(a["column"], "")
            if a["fn"] in ("sum", "avg", "median") and source_kind in ("text", "boolean"):
                # The single most common first mistake, and until this check
                # existed the flow SAVED and then failed its build with
                # "Laurelin's own code raised". `count` is named in the remedy
                # because it is almost always what was meant. `median` sits
                # behind the same gate as `sum`/`avg`: a median over text is a
                # BinderException at run, and a sentence here instead.
                raise _refuse_kind(
                    node=nid, field="aggs", column=a["column"], actual=source_kind,
                    wanted="number",
                    doing={"sum": "totals", "avg": "averages",
                           "median": "takes the median of"}[a["fn"]],
                    remedy=(
                        "Use 'Number of rows with a value' to count them "
                        "instead, or add a 'cast' step converting the column "
                        "to a number first."
                    ),
                )
            if a["fn"] == "count_distinct":
                select_parts.append(f"count(DISTINCT {col}) AS {alias}")
                new_kinds[a["as"]] = "number"
            else:
                # `fn` is in AGG_FNS and every remaining member is spelled the
                # same in DuckDB; asserting it keeps the f-string honest.
                assert a["fn"] in AGG_FNS
                select_parts.append(f"{a['fn']}({col}) AS {alias}")
                new_kinds[a["as"]] = (
                    "number" if a["fn"] in ("count", "sum", "avg") else source_kind
                )
        new_schema = group_by + [a["as"] for a in p["aggs"]]
        _reject_confusable_schema(new_schema, node=nid, field="aggs")
        body = f"SELECT {', '.join(select_parts)} FROM {src_cte}"
        if group_by:
            body += " GROUP BY " + ", ".join(_quote(c) for c in group_by)
        return _NodeSql(body=body, schema=new_schema, kinds=new_kinds)

    if kind == "dedupe":
        keys = p["keys"]
        for c in keys:
            resolve_column(c, schema, node=nid, field="keys")
        dirs = SORT_DIRS if p["keep"] == "first" else SORT_DIRS_REVERSED
        order_parts = []
        for e in p["order_by"]:
            col = resolve_column(e["column"], schema, node=nid, field="order_by")
            order_parts.append(f"{col} {dirs[e['dir']]}")
        rn = _unique_helper_column(schema, "_laurelin_rn")
        cols = ", ".join(_quote(c) for c in schema)
        inner = (
            f"SELECT {cols}, row_number() OVER ("
            f"PARTITION BY {', '.join(_quote(c) for c in keys)} "
            f"ORDER BY {', '.join(order_parts)}) AS {_quote(rn)} FROM {src_cte}"
        )
        return _NodeSql(
            body=f"SELECT {cols} FROM ({inner}) WHERE {_quote(rn)} = 1",
            schema=list(schema),
        )

    if kind == "sort":
        order_parts = []
        for e in p["by"]:
            col = resolve_column(e["column"], schema, node=nid, field="by")
            order_parts.append(
                f"{col} {SORT_DIRS[e['dir']]} {NULLS_PLACEMENT[e['nulls']]}"
            )
        clause = ", ".join(order_parts)
        return _NodeSql(
            body=f"SELECT * FROM {src_cte} ORDER BY {clause}",
            schema=list(schema),
            kinds=dict(kinds),
            order_by=clause,
        )

    if kind == "join":
        left, right = inputs[0], inputs[1]
        lcte, rcte = input_ctes[0], input_ctes[1]
        if lcte == rcte:
            # Both slots fed by the same step. Nothing structural forbade it —
            # arity is 2 and both key sides resolve — and it compiled to
            # `FROM _f0 INNER JOIN _f0`, which reaches the author as DuckDB's
            # `Ambiguous reference to table "_f0"` through the generic failure
            # pipe. Every other malformed flow in this module is refused with a
            # sentence naming the step, so this one is too.
            raise FlowRefused(
                f"Step {nid!r} joins a step to itself. Pick two different "
                "steps — to compare a dataset with a summary of itself, add "
                "the summary as its own step first and join to that.",
                node=nid, field="inputs",
            )
        on_parts = []
        right_key_cols = set()
        for k in p["keys"]:
            lc = resolve_column(k["left"], left.schema, node=nid, field="keys")
            rc = resolve_column(k["right"], right.schema, node=nid, field="keys")
            on_parts.append(f"{lcte}.{lc} = {rcte}.{rc}")
            right_key_cols.add(k["right"])
        # The right side's key columns are dropped from the output: they are
        # equal to the left's by construction on an inner join, and carrying
        # both would collide whenever the two sides spell the key the same way.
        right_cols = [c for c in right.schema if c not in right_key_cols]
        collisions = [c for c in right_cols if _collides(c, left.schema)]
        if collisions:
            # Names every collision, not just the first, and names dropping
            # before renaming. Measured on this repo's own demo: joining
            # `clean_flights` to `clean_aircraft` on `tail_number` — the
            # obvious shape, and what `demo.py` writes in SQL — was refused
            # over `status`, a column the pipeline never uses. Being told to
            # rename it produces a worse result than dropping it, and being
            # told about one collision at a time means as many round trips as
            # there are shared columns.
            listed = ", ".join(repr(c) for c in collisions[:8])
            more = (
                f" (and {len(collisions) - 8} more)" if len(collisions) > 8 else ""
            )
            raise FlowRefused(
                f"Step {nid!r} joins two inputs that both have these columns: "
                f"{listed}{more}. Add a 'select' step to one side first "
                "and drop the ones you do not need, or a 'rename' step so "
                "every column in the result has a distinct name.",
                node=nid, field="keys",
            )
        parts = [f"{lcte}.{_quote(c)}" for c in left.schema]
        parts += [f"{rcte}.{_quote(c)}" for c in right_cols]
        how = JOIN_HOWS[p["how"]]
        return _NodeSql(
            body=(
                f"SELECT {', '.join(parts)} FROM {lcte} {how} {rcte} ON "
                + " AND ".join(on_parts)
            ),
            schema=[*left.schema, *right_cols],
            kinds=(
                dict(left.kinds)
                | {c: k for c, k in right.kinds.items() if c in set(right_cols)}
            ),
        )

    # Unreachable — Tier A rejects unknown kinds. Fail closed.
    raise FlowRefused(f"Step kind {kind!r} cannot be compiled.", node=nid, field="kind")


# ---------------------------------------------------------------------------
# Whole-flow compilation
# ---------------------------------------------------------------------------


def _topological(flow: FlowDef, terminal: str) -> list[FlowNode]:
    by_id = {n.id: n for n in flow.nodes}
    order: list[FlowNode] = []
    seen: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        node = by_id[node_id]
        for i in node.inputs:
            visit(i)
        order.append(node)

    visit(terminal)
    return order


def compile_flow(
    flow: FlowDef,
    schemas: Mapping[str, list[str]],
    *,
    column_kinds: Optional[Mapping[str, Mapping[str, str]]] = None,
    upto: Optional[str] = None,
    limit: Optional[int] = None,
) -> CompiledFlow:
    """Compile ``flow`` to one statement. This is Tier B validation.

    ``column_kinds`` maps dataset name -> {column: one of `KINDS`}, and is what
    turns "sum a column of names" from a failed build into a sentence at save
    time. It is optional and omitting it only *loses* checks — a caller with no
    type information compiles exactly what it compiled before the type model
    existed, which is why adding it did not have to change every caller at once.

    ``schemas`` maps dataset name -> column names, and is the *live* schema:
    membership in it is what makes an identifier legal. Passing a stale or
    over-broad mapping is the only way to weaken this module, which is why the
    Builder rebuilds it immediately before execution rather than trusting one
    captured at authoring time.

    ``upto`` compiles only the sub-graph feeding one node (preview). ``limit``
    appends ``LIMIT ?`` **at the terminal node only** — never pushed into an
    upstream CTE, because an aggregate over a limited input is not the build's
    aggregate, and a preview that quietly answers a different question is worse
    than a slow one.
    """
    terminal = upto if upto is not None else flow.terminal
    if terminal not in {n.id for n in flow.nodes}:
        raise FlowRefused(f"No step named {terminal!r} in this flow.", node=terminal)

    order = _topological(flow, terminal)
    out = _Sql()
    compiled: dict[str, _NodeSql] = {}
    cte_of: dict[str, str] = {}
    cte_defs: list[str] = []

    for index, node in enumerate(order):
        node_inputs = [compiled[i] for i in node.inputs]
        node_ctes = [cte_of[i] for i in node.inputs]
        result = _compile_node(
            node, node_inputs, node_ctes, out, schemas, column_kinds or {}
        )
        name = _cte_name(index)
        compiled[node.id] = result
        cte_of[node.id] = name
        cte_defs.append(f"{name} AS ({result.body})")

    last = compiled[terminal]
    sql = _Sql()
    sql.add("WITH ").join(cte_defs, ", ")
    sql.add(f" SELECT * FROM {cte_of[terminal]}")
    if last.order_by:
        # Re-emitted at the outer level so the result has a guaranteed total
        # order. An ORDER BY that only exists inside a CTE is advisory.
        sql.add(" ORDER BY ").add(last.order_by)
    # Parameters accumulated by the node compilers come first, in node order,
    # and the limit is appended last — matching the placement of `?` in the
    # text, which is what keeps the ordinals aligned.
    params = list(out.params)
    if limit is not None:
        sql.add(" LIMIT ")
        sql.bind(int(limit))
        params.extend(sql.params)

    return CompiledFlow(
        sql=sql.text(),
        params=params,
        inputs=flow.source_datasets(),
        schema=list(last.schema),
        kinds={c: k for c, k in last.kinds.items() if k},
    )


def flow_expectations(flow: FlowDef, schema: list[str]) -> list:
    """Build the flow's expectations against its *output* schema.

    A flow exposes a closed subset of the expectation API, authored as
    dropdowns. ``expectations.expression()`` — a raw predicate interpolated
    into ``WHERE NOT (...)`` — is never reachable from a flow, at any level. It
    stays for the Python path.
    """
    from laurelin.transforms import expectations as exp

    out = []
    for e in flow.expectations:
        if e.kind == "row_count_between":
            out.append(exp.row_count(min=e.min, max=e.max, severity=e.severity))
            continue
        if e.column not in schema:
            raise FlowRefused(
                f"This flow checks column {e.column!r}, which its result does "
                f"not have. Its columns are: "
                f"{', '.join(repr(c) for c in schema)}.",
                field="expectations",
            )
        if e.kind == "not_null":
            out.append(exp.not_null(e.column, severity=e.severity))
        elif e.kind == "unique":
            out.append(exp.unique(e.column, severity=e.severity))
        elif e.kind == "accepted_values":
            out.append(
                exp.accepted_values(e.column, list(e.values), severity=e.severity)
            )
    return out
