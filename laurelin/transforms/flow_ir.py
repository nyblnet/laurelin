"""The Flow IR: a closed, declarative description of a pipeline.

A *flow* is what a non-programmer authors instead of Python. It is stored as
``pipelines/<name>.flow.json`` and compiled to SQL in memory by
:mod:`laurelin.transforms.flow_compile`. Nothing here generates Python and
nothing here is ``exec``-ed — that is the whole security premise of the
feature, and the reason the IR is closed rather than extensible.

**There is no node, at any nesting depth, that carries raw SQL or a raw
expression string.** Every position an author can fill is one of exactly three
things:

* a **value**, which the compiler *binds* as a query parameter and never
  renders as text;
* an **identifier**, which is checked for membership in a live schema before it
  is quoted (:mod:`flow_compile`);
* a member of a **closed enum**, which selects a keyword the compiler already
  contains.

That taxonomy is exhaustive by construction, and the hostile-value matrix in
``tests/test_flow_compile.py`` sweeps every position in it. It matters because
the build's DuckDB connection historically had external access ON: measured on
this tree, a ``kind="sql"`` transform running
``read_csv_auto('<tmp>/secret.csv')`` succeeded and published the file's
contents as a dataset. A no-code builder exists precisely to serve people who
must not be handed that primitive.

Validation is deliberately split in two (see ``docs`` in ``flow_compile``):

* **Tier A — structural**, here. Enum membership, arity, literal type coercion,
  node-id syntax, DAG shape, invented-identifier syntax. Needs no schema, so it
  runs on every registry collection (which happens per API request) and is
  cheap.
* **Tier B — schema binding**, in ``flow_compile``. Referenced-identifier
  membership against the *live* schema. Runs at PUT, at preview, and inside the
  Builder immediately before execution — never during a bare collection,
  because resolving a source-scanned dataset's schema hits a remote system.

Tier A alone determines ``spec.inputs`` (from the ``source`` nodes), therefore
lineage, therefore marking propagation. That derivation is structural on
purpose: inferring inputs by regex over generated SQL text — which
``generate_sql_transform`` does for the Python path — is how a workspace
acquires markings nobody can explain.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


class FlowRefused(ValueError):
    """A flow is not valid, and Laurelin is saying why in its own words.

    R1/R2: the message of this exception **is** first-party text and is safe to
    serve. It names the offending node and field and echoes the author's own
    identifier where that is the useful thing to say. It must never contain
    generated SQL, a driver's sentence, or a server path — the compiler raises
    this *before* any SQL exists, and ``flow_compile`` keeps it that way.
    """

    def __init__(self, message: str, *, node: str = "", field: str = ""):
        super().__init__(message)
        self.node = node
        self.field = field


def _refuse(message: str, *, node: str = "", field: str = "") -> "FlowRefused":
    return FlowRefused(message, node=node, field=field)


class FlowSourceDenied(FlowRefused):
    """A flow's source exists but the principal it runs as may not read it.

    A subclass, not a flag, so the type itself is the discriminator at catch
    sites that must answer *access* differently from *staleness*: a viewer
    running a stored panel over a dataset withheld from their role was told the
    panel "refers to something that no longer exists" — false three ways (the
    dataset exists, nothing is broken, and an entitled viewer sees a working
    chart). The message still names the dataset, so it is still served only to
    a principal who could have authored the flow; below that level the catch
    site renders an access sentence with no dataset name in it.
    """


# ---------------------------------------------------------------------------
# Closed vocabularies
#
# Every one of these is a *rendering* decision the compiler owns. An author
# picks a key; the compiler emits the value. No author byte reaches the SQL
# through any of them, which is why they are dicts of keyword -> keyword rather
# than pass-through strings.
# ---------------------------------------------------------------------------

NODE_KINDS: tuple[str, ...] = (
    "source", "filter", "select", "rename", "derive",
    "cast", "join", "aggregate", "dedupe", "sort",
)

#: Fixed input arity per node kind. `join` is the only 2-ary kind, and its
#: inputs are ORDERED (left, right) — a left join is not commutative.
NODE_ARITY: dict[str, int] = {
    "source": 0, "filter": 1, "select": 1, "rename": 1, "derive": 1,
    "cast": 1, "join": 2, "aggregate": 1, "dedupe": 1, "sort": 1,
}

#: Cast targets. Closed because `CAST(x AS ?)` is a *parser* error in DuckDB
#: (measured), so the target type can only ever be rendered as text — and a
#: rendered position must come from a vocabulary the compiler owns.
CAST_TYPES: dict[str, str] = {
    "varchar": "VARCHAR",
    "bigint": "BIGINT",
    "double": "DOUBLE",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
}

#: Aggregate functions. `count_star` is separate from `count` because
#: `count(*)` counts rows and `count(c)` counts non-NULLs, and an analyst who
#: cannot see the difference in the UI will pick the wrong one.
AGG_FNS: tuple[str, ...] = (
    "count_star", "count", "count_distinct", "sum", "avg", "min", "max",
    "any_value",
    # Parity with the ontology aggregate (`OntologyService.AGGREGATIONS`),
    # which has offered `median` since aggregations shipped: an object-backed
    # dashboard panel could chart a median while a dataset-backed one could
    # not. Spelled the same in DuckDB, so the aggregate compiler's generic
    # `fn(col)` branch renders it; the numeric gate lives beside sum/avg.
    "median",
)

JOIN_HOWS: dict[str, str] = {"inner": "INNER JOIN", "left": "LEFT JOIN"}
SORT_DIRS: dict[str, str] = {"asc": "ASC", "desc": "DESC"}
#: Reversed directions, for `dedupe(keep="last")` — which is `keep="first"`
#: over the inverted order rather than a second code path.
SORT_DIRS_REVERSED: dict[str, str] = {"asc": "DESC", "desc": "ASC"}
NULLS_PLACEMENT: dict[str, str] = {"first": "NULLS FIRST", "last": "NULLS LAST"}
SELECT_MODES: tuple[str, ...] = ("keep", "drop")
DEDUPE_KEEP: tuple[str, ...] = ("first", "last")

LIT_TYPES: tuple[str, ...] = (
    "string", "bigint", "double", "boolean", "date", "timestamp", "null",
)

DATE_TRUNC_UNITS: tuple[str, ...] = (
    "year", "quarter", "month", "week", "day", "hour",
)

EXPECTATION_KINDS: tuple[str, ...] = (
    "not_null", "unique", "accepted_values", "row_count_between",
)
SEVERITIES: tuple[str, ...] = ("error", "warn")

#: Operators, mapped to their arity spec: (min_args, max_args or None).
OPS: dict[str, tuple[int, Optional[int]]] = {
    "and": (2, None), "or": (2, None), "not": (1, 1),
    "eq": (2, 2), "ne": (2, 2), "lt": (2, 2), "lte": (2, 2),
    "gt": (2, 2), "gte": (2, 2),
    "is_null": (1, 1), "is_not_null": (1, 1),
    "in": (2, None), "not_in": (2, None),
    "like": (2, 2),
    "add": (2, 2), "sub": (2, 2), "mul": (2, 2), "div": (2, 2),
    "if_else": (3, 3), "coalesce": (2, None),
    "upper": (1, 1), "lower": (1, 1), "trim": (1, 1),
    "length": (1, 1), "abs": (1, 1), "round": (1, 2),
    # `floor` exists for numeric histograms: bin = mul(floor(div(col, width)),
    # width) as a `derive`, then group by the derived column — so the binning
    # runs inside the governed, parameter-bound query instead of shipping raw
    # rows to a client to bucket. Rendered from `_FUNCS` like `abs`/`round`.
    "floor": (1, 1),
    "concat": (2, None),
    "date_trunc": (2, 2),
}

#: Ops whose result is a boolean. A `filter` predicate's root and an
#: `if_else` condition must be one of these — otherwise `WHERE "amount"` is a
#: type error surfaced by DuckDB rather than by us, and the author sees a
#: binder message instead of a sentence.
BOOLEAN_OPS: frozenset[str] = frozenset({
    "and", "or", "not", "eq", "ne", "lt", "lte", "gt", "gte",
    "is_null", "is_not_null", "in", "not_in", "like",
})

# ---------------------------------------------------------------------------
# Syntax of names
# ---------------------------------------------------------------------------

#: Node ids. Never rendered into SQL (the compiler names its CTEs positionally
#: — see `flow_compile._cte_name`), but validated anyway so a malformed flow is
#: refused at the door rather than deep inside a topological sort.
_NODE_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")

#: Identifiers an author *invents*: `rename.to`, `derive.name`, `aggregate.as`.
#:
#: Deliberately narrower than what a Parquet column may be called. A column an
#: author *points at* must accept whatever the data really contains (see
#: `flow_compile.resolve_column`), but one they are inventing can be restricted
#: without losing anything — and restricting it here means a flow can never
#: mint a column name that a future dialect renderer would mis-resolve
#: (dialects.py escapes backslashes for ClickHouse and backticks for
#: StarRocks). A flow's output stays portable to every engine in the tree by
#: construction. No quote, no backslash, no backtick, no control character, no
#: dot.
_NEW_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_ ]{0,127}$")

#: Flow name = transform name = output dataset name. Same character rule as
#: `authoring._validate_module_name` and `catalog._NAME_RE`, because it must
#: satisfy all three at once.
_FLOW_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: …but bounded, which those two are not. A flow's name is also a *filename*
#: (`<name>.flow.json`), and most filesystems cap a component at 255 bytes. An
#: unbounded name would surface as an OSError from `os.replace` rather than as
#: our sentence, at the moment of saving work. 128 leaves ample room for the
#: suffix and for the `.{name}-XXXX.json.tmp` the atomic write creates.
_FLOW_NAME_MAX = 128

#: A dataset a flow *reads*. Only the character rule applies: the name already
#: exists in the catalog, so bounding it here would refuse a legitimate source.
_SOURCE_NAME_RE = _FLOW_NAME_RE

#: What a `.flow.json` file is called. `Path.suffix` of "x.flow.json" is
#: ".json", not ".flow.json", which is why the export/import/scan suffix
#: tuples take ".json" rather than this constant.
FLOW_SUFFIX = ".flow.json"


def validate_flow_name(name: Any, *, field: str = "name") -> str:
    if not isinstance(name, str) or not _FLOW_NAME_RE.match(name):
        raise _refuse(
            f"Invalid flow name {name!r}: must match ^[a-z][a-z0-9_]*$ — "
            "lowercase letters, digits and underscores, starting with a "
            "letter. A flow's name is also the name of the dataset it "
            "produces, so it has to be a legal dataset name.",
            field=field,
        )
    if len(name) > _FLOW_NAME_MAX:
        raise _refuse(
            f"Flow name is {len(name)} characters; the limit is "
            f"{_FLOW_NAME_MAX}. The name is also a filename, and a longer one "
            "would fail to save rather than fail to validate.",
            field=field,
        )
    return name


def validate_new_identifier(name: Any, *, node: str, field: str) -> str:
    """A column name the author is *inventing*, not one they are pointing at."""
    if not isinstance(name, str) or not _NEW_IDENT_RE.match(name):
        raise _refuse(
            f"Invalid new column name {name!r} on step {node!r}: use letters, "
            "digits, underscores and spaces, starting with a letter or "
            "underscore (up to 128 characters).",
            node=node, field=field,
        )
    return name


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


def _coerce_literal(type_name: str, value: Any, *, node: str, field: str) -> Any:
    """Turn a JSON value into the typed Python object the compiler will bind.

    Coercion happens *here*, in the validator, so what reaches the parameter
    list is a `date` / `int` / `bool` / `str`, never a string the database
    would be left to reinterpret. Strict on purpose: silently accepting "5"
    where a bigint was declared means the author's filter and their preview
    can disagree about what `>` does.
    """
    if type_name == "null":
        if value is not None:
            raise _refuse(
                f"A null literal on step {node!r} must have value null.",
                node=node, field=field,
            )
        return None
    if value is None:
        raise _refuse(
            f"A {type_name} literal on step {node!r} has no value; use the "
            "null type for a null.",
            node=node, field=field,
        )
    try:
        if type_name == "string":
            if not isinstance(value, str):
                raise TypeError
            return value
        if type_name == "bigint":
            # bool is a subclass of int; a checkbox is not a number.
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError
            return value
        if type_name == "double":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError
            return float(value)
        if type_name == "boolean":
            if not isinstance(value, bool):
                raise TypeError
            return value
        if type_name == "date":
            if not isinstance(value, str):
                raise TypeError
            return _dt.date.fromisoformat(value)
        if type_name == "timestamp":
            if not isinstance(value, str):
                raise TypeError
            return _dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise _refuse(
            f"Value {value!r} on step {node!r} is not a valid {type_name}.",
            node=node, field=field,
        ) from None
    raise _refuse(
        f"Unknown literal type {type_name!r} on step {node!r}; expected one "
        f"of {', '.join(LIT_TYPES)}.",
        node=node, field=field,
    )


def validate_expr(raw: Any, *, node: str, field: str, depth: int = 0) -> dict:
    """Tier A validation of one expression tree; returns a *normalized* copy.

    Normalized means: literal values are already coerced to Python objects, and
    no key survives that the compiler does not read. The compiler therefore
    walks a tree it can trust, and a hostile key in the submitted JSON cannot
    ride along to be picked up by some later reader.
    """
    if depth > 32:
        # Not a security bound (nothing recurses on untrusted depth after
        # this), but a flow nested 32 deep is a bug in a client, and a
        # RecursionError is not a sentence anyone can act on.
        raise _refuse(
            f"Expression on step {node!r} is nested too deeply (limit 32).",
            node=node, field=field,
        )
    if not isinstance(raw, dict):
        raise _refuse(
            f"Expression on step {node!r} must be an object with a 't' key.",
            node=node, field=field,
        )
    t = raw.get("t")
    if t == "col":
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise _refuse(
                f"A column reference on step {node!r} needs a 'name'.",
                node=node, field=field,
            )
        # NOT pattern-matched. Membership against the live schema is Tier B,
        # in flow_compile.resolve_column, because a real Parquet column may be
        # called `total (USD)` or `a"b` and a regex here would reject data the
        # author legitimately has.
        return {"t": "col", "name": name}
    if t == "lit":
        type_name = raw.get("type")
        if type_name not in LIT_TYPES:
            raise _refuse(
                f"Unknown value type {type_name!r} on step {node!r}; expected "
                f"one of {', '.join(LIT_TYPES)}.",
                node=node, field=field,
            )
        return {
            "t": "lit",
            "type": type_name,
            "value": _coerce_literal(type_name, raw.get("value"), node=node, field=field),
        }
    if t == "op":
        op = raw.get("op")
        if op not in OPS:
            raise _refuse(
                f"Unknown operation {op!r} on step {node!r}.",
                node=node, field=field,
            )
        args = raw.get("args")
        if not isinstance(args, list):
            raise _refuse(
                f"Operation {op!r} on step {node!r} needs an 'args' list.",
                node=node, field=field,
            )
        lo, hi = OPS[op]
        if len(args) < lo or (hi is not None and len(args) > hi):
            expected = f"{lo}" if hi == lo else (
                f"at least {lo}" if hi is None else f"{lo} to {hi}"
            )
            raise _refuse(
                f"Operation {op!r} on step {node!r} takes {expected} "
                f"argument(s), got {len(args)}.",
                node=node, field=field,
            )
        out = [
            validate_expr(a, node=node, field=field, depth=depth + 1) for a in args
        ]
        _validate_op_shape(op, out, node=node, field=field)
        return {"t": "op", "op": op, "args": out}
    raise _refuse(
        f"Expression on step {node!r} must have t of 'col', 'lit' or 'op'.",
        node=node, field=field,
    )


def _validate_op_shape(op: str, args: list[dict], *, node: str, field: str) -> None:
    """Per-op constraints that arity alone does not express."""
    if op in ("in", "not_in"):
        for a in args[1:]:
            if a["t"] != "lit":
                raise _refuse(
                    f"The values of {op!r} on step {node!r} must be literal "
                    "values, not columns or expressions.",
                    node=node, field=field,
                )
    elif op == "like":
        if args[1]["t"] != "lit" or args[1]["type"] != "string":
            raise _refuse(
                f"The pattern of 'like' on step {node!r} must be a text "
                "value.",
                node=node, field=field,
            )
    elif op == "date_trunc":
        unit = args[0]
        if unit["t"] != "lit" or unit["type"] != "string":
            raise _refuse(
                f"The unit of 'date_trunc' on step {node!r} must be a text "
                "value.",
                node=node, field=field,
            )
        if unit["value"] not in DATE_TRUNC_UNITS:
            raise _refuse(
                f"Unknown date unit {unit['value']!r} on step {node!r}; "
                f"expected one of {', '.join(DATE_TRUNC_UNITS)}.",
                node=node, field=field,
            )
    elif op == "round":
        if len(args) == 2 and (args[1]["t"] != "lit" or args[1]["type"] != "bigint"):
            raise _refuse(
                f"The number of decimal places for 'round' on step {node!r} "
                "must be a whole number.",
                node=node, field=field,
            )
    elif op == "if_else":
        _require_boolean(args[0], node=node, field=field, what="The condition of 'if_else'")


def _require_boolean(expr: dict, *, node: str, field: str, what: str) -> None:
    if expr["t"] != "op" or expr["op"] not in BOOLEAN_OPS:
        raise _refuse(
            f"{what} on step {node!r} must be a comparison (is, is not, "
            "greater than, contains, and/or, …), not a bare column or value.",
            node=node, field=field,
        )


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowNode:
    id: str
    kind: str
    inputs: tuple[str, ...]
    #: Kind-specific, already normalized by `_validate_params`. Only keys the
    #: compiler reads survive validation.
    params: dict = field(default_factory=dict)


def _str_list(raw: Any, *, node: str, field: str, min_len: int = 0) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise _refuse(
            f"{field!r} on step {node!r} must be a list of column names.",
            node=node, field=field,
        )
    if len(raw) < min_len:
        raise _refuse(
            f"{field!r} on step {node!r} needs at least {min_len} entry(ies).",
            node=node, field=field,
        )
    return list(raw)


def _enum(raw: Any, allowed: Iterable[str], *, node: str, field: str) -> str:
    allowed = tuple(allowed)
    if raw not in allowed:
        raise _refuse(
            f"Invalid {field} {raw!r} on step {node!r}; expected one of "
            f"{', '.join(allowed)}.",
            node=node, field=field,
        )
    return raw  # type: ignore[return-value]


def _dict_list(raw: Any, *, node: str, field: str, min_len: int = 1) -> list[dict]:
    if not isinstance(raw, list) or not all(isinstance(x, dict) for x in raw):
        raise _refuse(
            f"{field!r} on step {node!r} must be a list of objects.",
            node=node, field=field,
        )
    if len(raw) < min_len:
        raise _refuse(
            f"{field!r} on step {node!r} needs at least {min_len} entry(ies).",
            node=node, field=field,
        )
    return raw


def _validate_params(kind: str, node_id: str, raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise _refuse(
            f"Step {node_id!r} must have a 'params' object.", node=node_id,
        )
    if kind == "source":
        dataset = raw.get("dataset")
        # Structural only: existence and view rights are Tier B / governance.
        # Same syntax as any Laurelin dataset, so a name that could never
        # exist is refused without a round trip to the metadata store.
        if not isinstance(dataset, str) or not _FLOW_NAME_RE.match(dataset):
            raise _refuse(
                f"Step {node_id!r} names dataset {dataset!r}, which is not a "
                "valid dataset name.",
                node=node_id, field="dataset",
            )
        return {"dataset": dataset}

    if kind == "filter":
        pred = validate_expr(raw.get("predicate"), node=node_id, field="predicate")
        _require_boolean(pred, node=node_id, field="predicate", what="A filter")
        return {"predicate": pred}

    if kind == "select":
        mode = _enum(raw.get("mode"), SELECT_MODES, node=node_id, field="mode")
        columns = _str_list(raw.get("columns"), node=node_id, field="columns", min_len=1)
        if len(set(columns)) != len(columns):
            raise _refuse(
                f"Step {node_id!r} lists the same column more than once.",
                node=node_id, field="columns",
            )
        return {"mode": mode, "columns": columns}

    if kind == "rename":
        pairs = _dict_list(raw.get("pairs"), node=node_id, field="pairs")
        out = []
        seen_from, seen_to = set(), set()
        for p in pairs:
            src = p.get("from")
            if not isinstance(src, str) or not src:
                raise _refuse(
                    f"A rename on step {node_id!r} needs a 'from' column.",
                    node=node_id, field="pairs",
                )
            dst = validate_new_identifier(p.get("to"), node=node_id, field="pairs")
            if src in seen_from:
                raise _refuse(
                    f"Step {node_id!r} renames {src!r} twice.",
                    node=node_id, field="pairs",
                )
            if dst in seen_to:
                raise _refuse(
                    f"Step {node_id!r} renames two columns to {dst!r}.",
                    node=node_id, field="pairs",
                )
            seen_from.add(src)
            seen_to.add(dst)
            out.append({"from": src, "to": dst})
        return {"pairs": out}

    if kind == "derive":
        name = validate_new_identifier(raw.get("name"), node=node_id, field="name")
        return {
            "name": name,
            "expr": validate_expr(raw.get("expr"), node=node_id, field="expr"),
        }

    if kind == "cast":
        column = raw.get("column")
        if not isinstance(column, str) or not column:
            raise _refuse(
                f"Step {node_id!r} needs a column to convert.",
                node=node_id, field="column",
            )
        return {
            "column": column,
            "to": _enum(raw.get("to"), CAST_TYPES, node=node_id, field="to"),
        }

    if kind == "join":
        how = _enum(raw.get("how"), JOIN_HOWS, node=node_id, field="how")
        keys = _dict_list(raw.get("keys"), node=node_id, field="keys")
        out = []
        for k in keys:
            left, right = k.get("left"), k.get("right")
            if not isinstance(left, str) or not left or not isinstance(right, str) or not right:
                raise _refuse(
                    f"Each join key on step {node_id!r} needs a 'left' and a "
                    "'right' column.",
                    node=node_id, field="keys",
                )
            out.append({"left": left, "right": right})
        return {"how": how, "keys": out}

    if kind == "aggregate":
        group_by = _str_list(raw.get("group_by"), node=node_id, field="group_by")
        if len(set(group_by)) != len(group_by):
            raise _refuse(
                f"Step {node_id!r} groups by the same column more than once.",
                node=node_id, field="group_by",
            )
        aggs = _dict_list(raw.get("aggs"), node=node_id, field="aggs")
        out = []
        for a in aggs:
            fn = _enum(a.get("fn"), AGG_FNS, node=node_id, field="fn")
            column = a.get("column")
            if fn == "count_star":
                # `count(*)` has no column, and accepting one would let the UI
                # show a column picker whose value is silently discarded.
                if column not in (None, ""):
                    raise _refuse(
                        f"'Count rows' on step {node_id!r} does not take a "
                        "column; use 'count' to count non-empty values in one.",
                        node=node_id, field="aggs",
                    )
                column = None
            else:
                if not isinstance(column, str) or not column:
                    raise _refuse(
                        f"Summary {fn!r} on step {node_id!r} needs a column.",
                        node=node_id, field="aggs",
                    )
            alias = validate_new_identifier(a.get("as"), node=node_id, field="aggs")
            out.append({"fn": fn, "column": column, "as": alias})
        names = group_by + [a["as"] for a in out]
        if len(set(names)) != len(names):
            raise _refuse(
                f"Step {node_id!r} would produce two columns with the same "
                "name; give each summary a distinct name.",
                node=node_id, field="aggs",
            )
        return {"group_by": group_by, "aggs": out}

    if kind == "dedupe":
        keys = _str_list(raw.get("keys"), node=node_id, field="keys", min_len=1)
        # order_by is MANDATORY. "Keep the first" without an order is not a
        # definition, it is a coin flip that is stable until the input is
        # compacted — and a nondeterministic dedupe feeding an object type
        # reshuffles object pages under readers, because
        # ontology/service.py assigns ordinals from file order.
        order_by = _order_by(raw.get("order_by"), node=node_id, field="order_by",
                             with_nulls=False)
        return {
            "keys": keys,
            "order_by": order_by,
            "keep": _enum(raw.get("keep"), DEDUPE_KEEP, node=node_id, field="keep"),
        }

    if kind == "sort":
        return {"by": _order_by(raw.get("by"), node=node_id, field="by", with_nulls=True)}

    raise _refuse(f"Unknown step kind {kind!r}.", node=node_id, field="kind")


def _order_by(raw: Any, *, node: str, field: str, with_nulls: bool) -> list[dict]:
    entries = _dict_list(raw, node=node, field=field)
    out = []
    for e in entries:
        column = e.get("column")
        if not isinstance(column, str) or not column:
            raise _refuse(
                f"Each sort entry on step {node!r} needs a column.",
                node=node, field=field,
            )
        item = {
            "column": column,
            "dir": _enum(e.get("dir"), SORT_DIRS, node=node, field="dir"),
        }
        if with_nulls:
            item["nulls"] = _enum(
                e.get("nulls", "last"), NULLS_PLACEMENT, node=node, field="nulls"
            )
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Expectations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowExpectation:
    kind: str
    column: Optional[str] = None
    values: tuple = ()
    min: Optional[int] = None
    max: Optional[int] = None
    severity: str = "error"

    def as_json(self) -> dict:
        out: dict = {"kind": self.kind, "severity": self.severity}
        if self.column is not None:
            out["column"] = self.column
        if self.kind == "accepted_values":
            out["values"] = list(self.values)
        if self.kind == "row_count_between":
            out["min"], out["max"] = self.min, self.max
        return out


def _validate_expectation(raw: Any, index: int) -> FlowExpectation:
    where = f"expectation #{index + 1}"
    if not isinstance(raw, dict):
        raise _refuse(f"{where} must be an object.", field="expectations")
    kind = _enum(raw.get("kind"), EXPECTATION_KINDS, node=where, field="kind")
    severity = _enum(raw.get("severity", "error"), SEVERITIES, node=where, field="severity")
    if kind == "row_count_between":
        lo, hi = raw.get("min"), raw.get("max")
        for label, v in (("min", lo), ("max", hi)):
            if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
                raise _refuse(
                    f"{where}: {label} must be a whole number.",
                    node=where, field=label,
                )
        if lo is None and hi is None:
            raise _refuse(
                f"{where}: set at least one of min or max.",
                node=where, field="min",
            )
        if lo is not None and hi is not None and lo > hi:
            raise _refuse(
                f"{where}: min ({lo}) is greater than max ({hi}).",
                node=where, field="min",
            )
        return FlowExpectation(kind=kind, min=lo, max=hi, severity=severity)

    column = raw.get("column")
    if not isinstance(column, str) or not column:
        raise _refuse(f"{where} needs a column.", node=where, field="column")
    if kind == "accepted_values":
        values = raw.get("values")
        if not isinstance(values, list) or not values:
            raise _refuse(
                f"{where} needs at least one permitted value.",
                node=where, field="values",
            )
        coerced = []
        for v in values:
            if v is None or isinstance(v, (str, int, float, bool)):
                # Compared as text (matching `expectations.accepted_values`),
                # and BOUND rather than rendered.
                coerced.append(str(v))
            else:
                raise _refuse(
                    f"{where}: {v!r} is not a permitted value type.",
                    node=where, field="values",
                )
        return FlowExpectation(
            kind=kind, column=column, values=tuple(coerced), severity=severity
        )
    return FlowExpectation(kind=kind, column=column, severity=severity)


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Params as JSON, undoing the one coercion that JSON cannot represent.

    `_coerce_literal` deliberately turns a declared date or timestamp into a
    real `datetime.date` / `datetime.datetime`, so that what reaches the
    parameter list is a typed object rather than a string the database is left
    to reinterpret. That is right, and it had one unhandled consequence:
    `FlowDef.as_json()` handed those objects straight to `json.dumps` in
    `flow_files.write`, which raised `TypeError: Object of type date is not
    JSON serializable` — not a `FlowRefused`, so `_flow_or_400` did not catch
    it, so `PUT /flows/{name}` returned **500** and the screen said "Error 500:
    Internal Server Error".

    Measured live: filtering on a date is about the most ordinary thing an
    analyst does, the UI offers "Date" and "Date and time" in its own value-type
    dropdown, `POST /flows/preview` returned 200 with correct rows for the very
    same literal — and then Save lost the whole flow, with nothing on screen
    naming the control that caused it.

    ISO 8601 both ways: `_coerce_literal` parses exactly this format back, so a
    flow round-trips through the file byte-for-byte in meaning.
    """
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


@dataclass(frozen=True)
class FlowDef:
    """A validated flow. Constructing one of these is Tier A validation.

    Frozen, because a `FlowDef` is handed to the Builder inside a
    `TransformSpec` and to `check_flow_governance`, and a validated object that
    can be mutated afterwards is a validated object in name only.
    """

    name: str
    output: str
    author: str
    terminal: str
    nodes: tuple[FlowNode, ...]
    expectations: tuple[FlowExpectation, ...] = ()
    description: str = ""

    # -- accessors -----------------------------------------------------------

    def node(self, node_id: str) -> FlowNode:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise _refuse(f"No step named {node_id!r} in this flow.", node=node_id)

    def source_datasets(self) -> list[str]:
        """The datasets this flow reads, in sorted order.

        This is what becomes `spec.inputs`, therefore lineage, therefore
        marking propagation — derived from the IR's structure and never by
        scanning generated SQL text.
        """
        return sorted({
            n.params["dataset"] for n in self.nodes if n.kind == "source"
        })

    def as_json(self) -> dict:
        return {
            "name": self.name,
            "output": self.output,
            "author": self.author,
            "description": self.description,
            "terminal": self.terminal,
            "nodes": [
                {"id": n.id, "kind": n.kind, "inputs": list(n.inputs),
                 "params": _jsonable(n.params)}
                for n in self.nodes
            ],
            "expectations": [e.as_json() for e in self.expectations],
        }

    @classmethod
    def from_json(cls, raw: Any, *, name: Optional[str] = None) -> "FlowDef":
        """Tier A validation. Raises `FlowRefused` and nothing else.

        `name`, when given, is authoritative — it comes from the route path or
        the filename, so a body claiming a different name cannot write to
        someone else's flow.
        """
        if not isinstance(raw, dict):
            raise _refuse("A flow must be a JSON object.")
        flow_name = validate_flow_name(name if name is not None else raw.get("name"))
        output = raw.get("output", flow_name)
        # Flow name == transform name == output dataset name. One name in the
        # UI, and — more importantly — no rename, because `spec.name` is the
        # primary key of lineage_edges, transform_state and build_tasks and
        # nothing in this tree deletes lineage on rename.
        if output != flow_name:
            raise _refuse(
                f"A flow's output dataset must be its own name: this flow is "
                f"{flow_name!r} but names output {output!r}.",
                field="output",
            )
        author = raw.get("author", "")
        if not isinstance(author, str):
            raise _refuse("A flow's author must be a username.", field="author")
        description = raw.get("description", "")
        if not isinstance(description, str):
            raise _refuse("A flow's description must be text.", field="description")

        raw_nodes = raw.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise _refuse("A flow needs at least one step.", field="nodes")

        nodes: list[FlowNode] = []
        seen: set[str] = set()
        for entry in raw_nodes:
            if not isinstance(entry, dict):
                raise _refuse("Each step must be an object.", field="nodes")
            node_id = entry.get("id")
            if not isinstance(node_id, str) or not _NODE_ID_RE.match(node_id):
                raise _refuse(
                    f"Invalid step id {node_id!r}: use lowercase letters, "
                    "digits and underscores (up to 64 characters).",
                    field="id",
                )
            if node_id in seen:
                raise _refuse(f"Two steps share the id {node_id!r}.", node=node_id)
            seen.add(node_id)
            kind = entry.get("kind")
            if kind not in NODE_KINDS:
                raise _refuse(
                    f"Unknown step kind {kind!r} on step {node_id!r}; expected "
                    f"one of {', '.join(NODE_KINDS)}.",
                    node=node_id, field="kind",
                )
            inputs = entry.get("inputs", [])
            if not isinstance(inputs, list) or not all(isinstance(i, str) for i in inputs):
                raise _refuse(
                    f"Step {node_id!r} must list its inputs as step ids.",
                    node=node_id, field="inputs",
                )
            if len(inputs) != NODE_ARITY[kind]:
                raise _refuse(
                    f"A {kind!r} step takes exactly {NODE_ARITY[kind]} "
                    f"input(s); step {node_id!r} has {len(inputs)}.",
                    node=node_id, field="inputs",
                )
            nodes.append(FlowNode(
                id=node_id, kind=kind, inputs=tuple(inputs),
                params=_validate_params(kind, node_id, entry.get("params", {})),
            ))

        terminal = raw.get("terminal")
        if not isinstance(terminal, str) or terminal not in seen:
            raise _refuse(
                f"This flow's last step {terminal!r} is not one of its steps.",
                field="terminal",
            )

        raw_exp = raw.get("expectations", [])
        if not isinstance(raw_exp, list):
            raise _refuse("'expectations' must be a list.", field="expectations")
        expectations = tuple(
            _validate_expectation(e, i) for i, e in enumerate(raw_exp)
        )

        flow = cls(
            name=flow_name, output=flow_name, author=author, terminal=terminal,
            nodes=tuple(nodes), expectations=expectations, description=description,
        )
        _validate_graph(flow, seen)
        return flow


def _validate_graph(flow: FlowDef, ids: set[str]) -> None:
    """DAG shape: edges resolve, no cycles, everything reachable from terminal.

    Checked here rather than left to `Builder.plan` so that a malformed flow is
    refused at PUT — where an author is watching — instead of at build time,
    where the only witness is a failed task.
    """
    for n in flow.nodes:
        for i in n.inputs:
            if i not in ids:
                raise _refuse(
                    f"Step {n.id!r} takes input from {i!r}, which is not a "
                    "step in this flow.",
                    node=n.id, field="inputs",
                )
            if i == n.id:
                raise _refuse(
                    f"Step {n.id!r} takes itself as input.",
                    node=n.id, field="inputs",
                )

    by_id = {n.id: n for n in flow.nodes}
    state: dict[str, int] = {}

    def visit(node_id: str) -> None:
        s = state.get(node_id, 0)
        if s == 2:
            return
        if s == 1:
            raise _refuse(
                f"The steps of this flow form a loop through {node_id!r}.",
                node=node_id, field="inputs",
            )
        state[node_id] = 1
        for i in by_id[node_id].inputs:
            visit(i)
        state[node_id] = 2

    visit(flow.terminal)
    unreachable = sorted(ids - set(state))
    if unreachable:
        # Not merely untidy: an unreachable `source` node would still be
        # collected into `spec.inputs` by a naive derivation and would then
        # propagate that dataset's markings onto an output that never read it.
        # `source_datasets()` reads every node, so this check is what keeps the
        # two consistent.
        raise _refuse(
            "These steps are not connected to the flow's last step: "
            + ", ".join(repr(u) for u in unreachable)
            + ". Remove them or connect them.",
            field="nodes",
        )
