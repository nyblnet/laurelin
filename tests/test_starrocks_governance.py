"""Every test here is a threat that was measured, not imagined.

The claim this file defends is the same one ``tests/test_clickhouse_governance``
makes, one engine over: **for every (policy, user, dataset), the StarRocks
renderer returns the same rows and the same values as
``PermissionService.apply_table_policy``, or refuses.** That function is the
reference; the SQL renderers are the things being checked against it.

"or refuses" is load-bearing. A row policy and a hash mask are both defined on
the column's *text*, and no two engines share a stringifier, so on boolean,
float and timestamp columns the same allowlist selects different rows here than
it does in Arrow. Where the renderer cannot prove agreement it refuses;
``tests/test_text_agreement.py`` re-derives the proof from the live engines.

What is specific to *this* engine, and why several tests below have no
ClickHouse counterpart:

1. **Stacked statements execute.** A policy value spliced into SQL is a remote
   INSERT, not merely a wrong SELECT — so the invariant is not "the escaper is
   faithful" but "no value is ever escaped": ``literal`` raises and every value
   travels as a bound ``?``.
2. **A double-quoted name is a string literal.** DuckDB's quoter therefore
   fails *open* here rather than closed.
3. **Boolean is not a portable row key.** It is on both other dialects, so the
   refusal is the surprising behaviour and gets its own test.
4. **The comparison is byte-exact even though the session says otherwise.**
   ``collation_connection`` reports ``utf8_general_ci``, which would make
   ``'us'`` match ``'US'`` — measured, it does not. That is a fact about a
   server version, so it is re-derived here rather than trusted.

The suite needs a real StarRocks (``LAURELIN_TEST_STARROCKS``); there is no
embedded mode to fall back on.
"""

import hashlib
import math
import re

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core import starrocks
from laurelin.core.approvals import ChangeTicket as _ChangeTicket
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.dialects import STARROCKS
from laurelin.core.models import MaskMode, Role, User
from laurelin.core.permissions import (
    PermissionService,
    PolicyDecision,
    PolicyRenderError,
    SqlPolicy,
)
from tests import starrocks_env

_TICKET = _ChangeTicket(kind="local", actor="test")

pytestmark = starrocks_env.needs_starrocks

VIEWER = User(id="1", username="vic", role=Role.viewer)
ADMIN = User(id="2", username="ada", role=Role.admin)

# Deliberately awkward: a NULL in the policy column, a NULL in a masked column,
# and a float column holding the values a mask has historically leaked.
#
# NaN is absent because StarRocks cannot store one: measured, both
# CAST('nan' AS DOUBLE) and 0/0 evaluate to NULL on insert. +Inf *is*
# representable (1e308*10) and is included, and -0.0 arrives as 0.0.
SALES_DDL = ("`id` BIGINT, `region` VARCHAR(16), `ssn` VARCHAR(32), "
             "`amount` DOUBLE, `ok` BOOLEAN, `dec2` DECIMAL(12,2)")
SALES_ROWS = [
    (1, "us", "001-00-1234", 1.0, True, "1.10"),
    (2, "eu", "002-00-1234", None, False, "0.00"),
    (3, "us", None, 0.0, True, "-3.05"),
    (4, None, "004-00-1234", -0.0, None, "2.00"),
    (5, "apac", "005-00-1234", None, True, None),
]


@pytest.fixture()
def env(tmp_path):
    name = starrocks_env.load(SALES_DDL, SALES_ROWS)
    con = starrocks_env.connect()
    try:
        cur = con.cursor()
        # The one value the driver cannot send: an overflowing literal is how
        # an infinity gets into a StarRocks DOUBLE.
        cur.execute(
            f"INSERT INTO `{starrocks_env.database()}`.`{name}` "
            "VALUES (6, 'us', '006-00-1234', 1e308*10, TRUE, '9.99')"
        )
        cur.close()
    finally:
        con.close()

    ws = Workspace.init(tmp_path / "ws", name="srgov")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.register_starrocks("sales", starrocks_env.source(name))
    yield catalog, store, PermissionService(store)
    starrocks_env.drop(name)


def set_policy(store, row_policy=None, masks=None, dataset="sales"):
    store.set_dataset_policy(dataset, {
        "dataset": dataset,
        "row_policy": row_policy,
        "column_masks": masks or [],
    }, ticket=_TICKET)


def ROWS(col, vals, subj="vic"):
    return {"column": col,
            "rules": [{"subject_kind": "user", "subject": subj, "values": vals}]}


def MASK(col, mode):
    return {"column": col, "mode": mode, "exempt": []}


def governed(catalog, perms, user, name="sales", limit=None) -> pa.Table:
    """Read exactly as the server does: one choke point, policy compiled in."""
    return catalog.source_table(
        name, sql_policy_for=perms.sql_policy_fn(user), limit=limit
    )


def reference(catalog, perms, user, name="sales") -> pa.Table:
    """The materializing policy engine — the definition of a correct answer."""
    return perms.apply_table_policy(user, name, catalog.source_table(name))


def same(a: pa.Table, b: pa.Table) -> bool:
    def norm(table):
        return sorted(
            [
                {k: ("nan" if isinstance(v, float) and math.isnan(v) else v)
                 for k, v in row.items()}
                for row in table.to_pylist()
            ],
            key=lambda r: str(sorted(r.items(), key=lambda kv: kv[0])),
        )
    return norm(a) == norm(b)


@pytest.fixture()
def captured(monkeypatch):
    """Record every statement that reaches the engine, still executing it."""
    seen: list[tuple] = []
    real = starrocks.run

    def spy(sql, params=None, con=None, source=None, schema=None):
        seen.append((sql, list(params or [])))
        return real(sql, params, con, source, schema)

    monkeypatch.setattr(starrocks, "run", spy)
    return seen


def _inner_block(sql: str) -> str:
    """The parenthesized sub-select that holds the WHERE clause."""
    assert "(SELECT" in sql, f"the statement is flat — nothing nests the filter: {sql}"
    start = sql.index("(SELECT")
    depth = 0
    for i in range(start, len(sql)):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return sql[start:i + 1]
    raise AssertionError(f"unbalanced statement: {sql}")


# ---------------------------------------------------------------------------
# 1. Alias shadowing — correct today, and not by accident tomorrow
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["redact", "null", "hash"])
def test_masking_the_policy_column_does_not_disable_the_row_filter(env, mode):
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us"]), masks=[MASK("region", mode)])
    assert same(governed(catalog, perms, VIEWER), reference(catalog, perms, VIEWER))
    assert governed(catalog, perms, VIEWER).num_rows == 3


def test_the_mask_value_is_not_a_key_to_the_whole_table(env):
    """The adversarial form: a policy whose allowed value *is* the redaction
    marker. On ClickHouse, flat, this returns every row in the table."""
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["***"]),
               masks=[MASK("region", "redact")])
    assert governed(catalog, perms, VIEWER).num_rows == 0
    assert same(governed(catalog, perms, VIEWER), reference(catalog, perms, VIEWER))


def test_the_filter_is_structurally_below_the_projection(env, captured):
    """Behaviour alone cannot tell correct nesting from inverted nesting for
    any policy that does not mask the filter column.

    On StarRocks the flat form happens to be correct *today* — WHERE resolves
    against the base columns, measured — but ORDER BY resolves against SELECT
    aliases, so a renderer that ever emits one would inherit ClickHouse's
    bypass. The shape is asserted so that correctness does not depend on which
    clauses the renderer happens to emit.
    """
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us"]), masks=[MASK("ssn", "hash")])
    governed(catalog, perms, VIEWER)

    sql = captured[-1][0]
    inner = _inner_block(sql)
    assert "WHERE" in inner, "the filter must live in the inner block"
    select_list = inner[len("(SELECT"):inner.index(" FROM ")].strip()
    assert select_list == "*", f"the filtered block must project nothing: {inner}"
    assert " AS " not in select_list
    assert " AS `ssn`" in sql[:sql.index(inner)]
    # ...and the derived table is named, without which StarRocks refuses the
    # statement outright (error 1248).
    assert inner + " t" in sql


# ---------------------------------------------------------------------------
# 2. Masked means masked, for every value of the type
# ---------------------------------------------------------------------------

def test_a_null_mask_masks_the_infinities_and_negative_zero(env):
    catalog, store, perms = env
    set_policy(store, masks=[MASK("amount", "null")])
    table = governed(catalog, perms, VIEWER)
    assert table.column("amount").to_pylist() == [None] * 6
    assert same(table, reference(catalog, perms, VIEWER))
    assert pa.types.is_floating(table.schema.field("amount").type), (
        "the mask must keep the column's type"
    )
    # The fixture really does contain an infinity, so this is not vacuous.
    assert float("inf") in catalog.read("sales").column("amount").to_pylist()


@pytest.mark.parametrize("column, arrow_type", [
    ("dec2", pa.decimal128(12, 2)), ("ok", pa.bool_()), ("id", pa.int64()),
])
def test_a_null_mask_keeps_every_column_type(env, column, arrow_type):
    catalog, store, perms = env
    set_policy(store, masks=[MASK(column, "null")])
    table = governed(catalog, perms, VIEWER)
    assert table.schema.field(column).type == arrow_type
    assert table.column(column).to_pylist() == [None] * 6


def test_the_hash_digest_is_the_arrow_paths_digest_value_for_value(env):
    catalog, store, perms = env
    set_policy(store, masks=[MASK("ssn", "hash")])
    got = governed(catalog, perms, VIEWER).column("ssn").to_pylist()
    want = reference(catalog, perms, VIEWER).column("ssn").to_pylist()
    assert sorted(v or "" for v in got) == sorted(v or "" for v in want)
    for value in got:
        if value is None:
            continue  # NULL stays NULL — never the digest of the string "None"
        assert re.fullmatch(r"[0-9a-f]{16}", value), value
    assert None in got, "the fixture's NULL ssn must survive as NULL"
    assert hashlib.sha256(b"001-00-1234").hexdigest()[:16] in got


def test_the_digest_is_not_double_hexed(env):
    """The specific way porting ClickHouse's ``lower(hex(...))`` would fail:
    ``sha2`` already returns hex, so wrapping it yields 128 characters and the
    16-character prefix stops matching the Arrow path's — a mask that is still
    a mask but no longer joins with anything."""
    catalog, store, perms = env
    set_policy(store, masks=[MASK("ssn", "hash")])
    for value in governed(catalog, perms, VIEWER).column("ssn").to_pylist():
        if value is not None:
            assert len(value) == 16


# ---------------------------------------------------------------------------
# 3. NULL in the policy column is never admitted
# ---------------------------------------------------------------------------

def test_a_null_policy_column_never_matches(env):
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us", "eu"]))
    assert same(governed(catalog, perms, VIEWER), reference(catalog, perms, VIEWER))
    assert governed(catalog, perms, VIEWER).num_rows == 4  # the NULL row is out


def test_the_empty_string_is_not_a_key_to_null_rows(env, captured):
    """``ifnull``/``coalesce`` around the predicate would render NULL as '' —
    and then a policy allowing '' would admit exactly the rows whose tenant is
    unknown."""
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", [""]))
    assert governed(catalog, perms, VIEWER).num_rows == 0

    inner = _inner_block(captured[-1][0])
    for forbidden in ("ifnull", "coalesce", "nvl", "assumeNotNull"):
        assert forbidden.lower() not in inner.lower(), inner


# ---------------------------------------------------------------------------
# 4. The comparison is byte-exact, whatever the session claims
# ---------------------------------------------------------------------------

def test_the_row_policy_comparison_is_byte_exact(env):
    """``collation_connection`` reports ``utf8_general_ci`` on this server,
    which would make ``'us'`` match ``'US'`` and a tenant policy admit another
    tenant's rows.

    Measured: it does not — ``IN ('us')`` matched only ``'us'``, not ``'US'``,
    ``'Us'``, ``'us '`` or ``' us'``, bound or inlined. So the response is this
    test rather than a forced binary collation (which would be an emitted-SQL
    variation nothing else exercises). A version bump that starts honouring the
    advertised collation fails here.
    """
    catalog, store, perms = env
    name = starrocks_env.load(
        "`id` INT, `region` VARCHAR(16)",
        [(1, "us"), (2, "US"), (3, "Us"), (4, "us "), (5, " us"),
         (6, "us"), (7, "üs")],
    )
    try:
        catalog.register_starrocks("collate", starrocks_env.source(name))
        set_policy(store, row_policy=ROWS("region", ["us"]), dataset="collate")

        got = governed(catalog, perms, VIEWER, name="collate")
        assert sorted(got.column("id").to_pylist()) == [1, 6], (
            "exactly the two byte-identical rows: not 'US', not 'Us', not the "
            "ones with a stray space, not 'üs'"
        )
        assert same(got, reference(catalog, perms, VIEWER, name="collate"))

        # And the session really does advertise a case-insensitive collation,
        # so the assertion above is not vacuous.
        con = starrocks_env.connect()
        try:
            reported = starrocks.run(
                "SELECT @@collation_connection AS c", con=con
            ).column("c").to_pylist()[0]
        finally:
            con.close()
        assert reported.endswith("_ci"), (
            f"collation is now {reported!r}; if it is binary, this test still "
            "passes but its comment is out of date"
        )
    finally:
        starrocks_env.drop(name)


# ---------------------------------------------------------------------------
# 5. Anything the renderer cannot express exactly is a refusal
# ---------------------------------------------------------------------------

def test_column_discovery_failure_refuses_rather_than_reads(env, monkeypatch):
    catalog, store, perms = env
    set_policy(store, masks=[MASK("ssn", "redact")])
    monkeypatch.setattr(
        starrocks, "schema_of", lambda source, con=None: pa.schema([])
    )
    with pytest.raises(PolicyRenderError):
        governed(catalog, perms, VIEWER)


def test_render_never_falls_back_to_star_with_masks_pending():
    with pytest.raises(PolicyRenderError):
        SqlPolicy.render(
            PolicyDecision(masks=[("ssn", MaskMode.redact)]), [], dialect=STARROCKS
        )


def test_a_case_mismatched_mask_is_a_refusal_not_plaintext(env):
    catalog, store, perms = env
    set_policy(store, masks=[MASK("SSN", "redact")])
    with pytest.raises(PolicyRenderError, match="case"):
        governed(catalog, perms, VIEWER)


@pytest.mark.parametrize("column", ["ok", "amount"])
def test_a_row_policy_on_a_type_this_engine_spells_differently_is_refused(env, column):
    """Boolean is the StarRocks-specific one and the reason this is
    parametrized: it is a perfectly good row key on DuckDB *and* ClickHouse, so
    the refusal here looks like a bug until you see that CAST(ok AS STRING) is
    '1' where the Arrow reference says 'true'. Serving those rows would mean
    ``/query`` and ``/datasets/{name}/rows`` disagreeing for the same user.
    """
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS(column, ["true"]))
    with pytest.raises(PolicyRenderError):
        governed(catalog, perms, VIEWER)


def test_a_decimal_row_policy_is_allowed_and_agrees(env):
    """The other direction: refusing a type the engine *does* spell correctly
    costs a working dataset for no safety gain. Decimal is in on this dialect
    and out on ClickHouse, and that difference was measured, not assumed."""
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("dec2", ["1.10"]))
    got = governed(catalog, perms, VIEWER)
    assert got.column("id").to_pylist() == [1]
    assert same(got, reference(catalog, perms, VIEWER))


# ---------------------------------------------------------------------------
# 6. denies_all, through the public route
# ---------------------------------------------------------------------------

def test_denies_all_yields_nothing_through_the_http_path(tmp_path):
    """Asserted at the surface a user actually reaches, over a table that
    contains a column literally named FALSE — because ``where='FALSE'`` and a
    column named ``FALSE`` are the same five characters to a naive renderer."""
    from fastapi.testclient import TestClient

    from laurelin.api import create_app

    name = starrocks_env.load(
        "`id` INT, `FALSE` VARCHAR(8), `region` VARCHAR(8)",
        [(1, "a", "us"), (2, "b", "eu"), (3, "c", "us")],
    )
    try:
        ws = Workspace.init(tmp_path / "ws", name="srdeny")
        app = create_app(ws)
        creds = {"username": "root", "password": "trustno1!"}
        admin = TestClient(app)
        admin.post("/api/v1/auth/setup", json=creds)
        admin.post("/api/v1/auth/login", json=creds)
        admin.put("/api/v1/datasets/awkward/starrocks",
                  json={"source": starrocks_env.source(name)})
        admin.post("/api/v1/users",
                   json={"username": "vic", "password": "password123",
                         "role": "viewer"})
        admin.put("/api/v1/datasets/awkward/policy", json={
            "row_policy": ROWS("region", ["us"], subj="nobody"),
            "column_masks": [],
        })

        viewer = TestClient(app)
        viewer.post("/api/v1/auth/login",
                    json={"username": "vic", "password": "password123"})

        # No rule grants vic anything -> denies_all.
        assert viewer.get("/api/v1/datasets/awkward/rows?limit=100").json()["rows"] == []
        assert viewer.get("/api/v1/datasets/awkward/rows?limit=1").json()["rows"] == []

        # The admin still sees the whole table, so this is a policy result and
        # not a broken read of a table with an awkward column name.
        body = admin.get("/api/v1/datasets/awkward/rows?limit=100").json()
        assert len(body["rows"]) == 3
    finally:
        starrocks_env.drop(name)


def test_denies_all_survives_a_limit(env):
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us"], subj="nobody"))
    assert governed(catalog, perms, VIEWER, limit=100).num_rows == 0
    assert governed(catalog, perms, VIEWER, limit=1).num_rows == 0
    assert governed(catalog, perms, None).num_rows == 0


# ---------------------------------------------------------------------------
# 7. The budget is pinned per query
# ---------------------------------------------------------------------------

def test_the_hint_carries_the_budget_and_nothing_carries_a_value(env, captured):
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us"]))
    governed(catalog, perms, VIEWER)
    sql, params = captured[-1]
    assert sql.startswith("SELECT /*+ SET_VAR(")
    assert "query_mem_limit=" in sql
    assert params == ["us"]
    assert "'us'" not in sql


# ---------------------------------------------------------------------------
# 8. Policy-value fidelity, and the write that must never happen
# ---------------------------------------------------------------------------

HOSTILE_VALUES = ["us\n", "us\\", "a\tb", "o'brien", "us%", "\\\\",
                  "us') OR 1=1 --", "us\\' OR 1=1 --", "us"]


@pytest.mark.parametrize("value", HOSTILE_VALUES, ids=[repr(v) for v in HOSTILE_VALUES])
def test_a_policy_value_selects_exactly_the_rows_it_names(tmp_path, value):
    """A value that arrives shortened or altered silently changes which rows a
    user may see. Rows are compared against the Arrow reference, not counted
    loosely — ``us\\`` must match the row whose region is ``us\\`` and nothing
    else, and the injection payloads must match themselves and nothing else."""
    regions = [*HOSTILE_VALUES, "plain"]
    name = starrocks_env.load(
        "`id` INT, `region` VARCHAR(64)", list(enumerate(regions))
    )
    try:
        ws = Workspace.init(tmp_path / "ws", name="srfid")
        store = MetadataStore(ws.metadata_path)
        catalog = DatasetCatalog(ws, store)
        catalog.register_starrocks("vals", starrocks_env.source(name))
        perms = PermissionService(store)
        set_policy(store, row_policy=ROWS("region", [value]), dataset="vals")

        got = catalog.source_table("vals", sql_policy_for=perms.sql_policy_fn(VIEWER))
        want = perms.apply_table_policy(VIEWER, "vals", catalog.source_table("vals"))
        assert same(got, want)
        assert got.num_rows == 1, "exactly the row named, no more and no fewer"
    finally:
        starrocks_env.drop(name)


def test_an_injection_payload_matches_nothing_and_writes_nothing(env):
    """The consequence that is unique to this engine.

    On ClickHouse a defective escaper leaks a read. Here the same defect is a
    remote INSERT, because stacked statements execute (see
    ``tests/test_dialects.py``). So the assertion is in two parts: the policy
    admits nothing, *and* the table the payload names is untouched.
    """
    catalog, store, perms = env
    victim = starrocks_env.load("`id` INT", [])
    try:
        target = f"`{starrocks_env.database()}`.`{victim}`"
        payloads = [
            f"us') OR 1=1; INSERT INTO {target} VALUES (77) -- ",
            "' OR '1'='1",
            "us'; DROP TABLE sales -- ",
            "us\\'; INSERT INTO x VALUES (1) -- ",
        ]
        for payload in payloads:
            set_policy(store, row_policy=ROWS("region", [payload]))
            assert governed(catalog, perms, VIEWER).num_rows == 0

        con = starrocks_env.connect()
        try:
            landed = starrocks.run(
                f"SELECT count(*) AS n FROM {target}", con=con
            ).column("n").to_pylist()[0]
        finally:
            con.close()
        assert landed == 0, "a policy value must never become a statement"
        assert catalog.read("sales").num_rows == 6, "and must never drop a table"
    finally:
        starrocks_env.drop(victim)


# ---------------------------------------------------------------------------
# 9. The whole point: the two renderers agree, or one of them refuses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row_policy, masks", [
    (None, []),
    (ROWS("region", ["us"]), []),
    (ROWS("region", ["us", "eu", "apac"]), []),
    (ROWS("region", []), []),
    (None, [MASK("ssn", "redact")]),
    (None, [MASK("ssn", "hash")]),
    (None, [MASK("amount", "null")]),
    (None, [MASK("ssn", "redact"), MASK("ssn", "hash")]),
    (ROWS("region", ["us"]), [MASK("ssn", "hash"), MASK("amount", "null")]),
    (ROWS("dec2", ["1.10", "0.00"]), [MASK("region", "redact")]),
    (ROWS("id", ["1", "3"]), [MASK("dec2", "null")]),
])
def test_the_sql_answer_is_the_arrow_answer(env, row_policy, masks):
    catalog, store, perms = env
    set_policy(store, row_policy=row_policy, masks=masks)
    assert same(governed(catalog, perms, VIEWER), reference(catalog, perms, VIEWER))
    # And an admin, who has no policy at all, still sees everything.
    assert governed(catalog, perms, ADMIN).num_rows == 6
