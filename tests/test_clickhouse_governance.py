"""Every test here is a threat that was measured, not imagined.

The claim this file defends is narrow and total: **for every (policy, user,
dataset), the ClickHouse renderer returns the same rows and the same values as
``PermissionService.apply_table_policy``.** That function is the reference, not
the DuckDB renderer — DuckDB is already measurably different for Float64 policy
columns (see the note at the bottom of the file).

Each test below corresponds to a way that claim was found to be false during
investigation, on this exact chdb build:

1. a flat statement lets the WHERE see the SELECT alias, so a mask on the
   policy column disables the row filter — and fails **open**;
2. ``NULLIF(c, c)`` leaves NaN unmasked;
3. ``ifNull``/``coalesce`` around the predicate would admit NULL rows;
4. an empty column list rendered ``select_list='*'``, an unmasked read;
5. a case-typo'd mask silently masked nothing;
6. ``denies_all`` had to survive limits, nesting and a column named ``FALSE``;
7. profile settings could change ``IN`` semantics under the query;
8. a policy value with a backslash in it matched the wrong rows.

The fixture writes a local Parquet file and lets chdb read it through
``file(path, Parquet)``. That is the whole test rig: no ClickHouse server, no
container, no service in CI.
"""

import math
import re

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core import clickhouse
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.dialects import CLICKHOUSE
from laurelin.core.models import MaskMode, Role, User
from laurelin.core.permissions import (
    PermissionService,
    PolicyDecision,
    PolicyRenderError,
    SqlPolicy,
)

pytestmark = pytest.mark.skipif(
    not clickhouse.available(), reason="needs chdb: pip install 'laurelin[clickhouse]'"
)

VIEWER = User(id="1", username="vic", role=Role.viewer)
ADMIN = User(id="2", username="ada", role=Role.admin)


def sales() -> pa.Table:
    """Deliberately awkward: a NULL in the policy column, and a float column
    holding every value that a mask has historically leaked."""
    return pa.table({
        "id": pa.array([1, 2, 3, 4, 5], pa.int64()),
        "region": ["us", "eu", "us", None, "apac"],
        "ssn": ["001-00-1234", "002-00-1234", None, "004-00-1234", "005-00-1234"],
        "amount": pa.array(
            [1.0, float("nan"), float("inf"), -0.0, None], pa.float64()
        ),
    })


@pytest.fixture()
def env(tmp_path):
    remote = tmp_path / "remote" / "sales.parquet"
    remote.parent.mkdir(parents=True)
    pq.write_table(sales(), remote)

    ws = Workspace.init(tmp_path / "ws", name="chgov")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.register_clickhouse("sales", {"type": "parquet", "path": str(remote)})
    return catalog, store, PermissionService(store)


def set_policy(store, row_policy=None, masks=None, dataset="sales"):
    store.set_dataset_policy(dataset, {
        "dataset": dataset,
        "row_policy": row_policy,
        "column_masks": masks or [],
    })


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
        return [
            {k: ("nan" if isinstance(v, float) and math.isnan(v) else v)
             for k, v in row.items()}
            for row in table.to_pylist()
        ]
    return norm(a) == norm(b)


@pytest.fixture()
def captured(monkeypatch):
    """Record every statement that reaches the engine, still executing it."""
    seen: list[str] = []
    real = clickhouse.run

    def spy(sql):
        seen.append(sql)
        return real(sql)

    monkeypatch.setattr(clickhouse, "run", spy)
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
# 1. Alias shadowing — the bypass that fails open
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["redact", "null", "hash"])
def test_masking_the_policy_column_does_not_disable_the_row_filter(env, mode):
    """A policy that masks column X *and* filters on X.

    Flat, ClickHouse resolves ``WHERE toString(region) IN (...)`` against the
    SELECT alias ``region`` — which is now the mask. Measured on a 4-row file:
    ``IN ('***')`` returned all 4 rows and ``IN ('us')`` returned 0 where 2 are
    correct. Both wrong; the first is a complete row-policy bypass.
    """
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us"]), masks=[MASK("region", mode)])
    assert same(governed(catalog, perms, VIEWER), reference(catalog, perms, VIEWER))
    assert governed(catalog, perms, VIEWER).num_rows == 2


def test_the_mask_value_is_not_a_key_to_the_whole_table(env):
    """The adversarial form: a policy whose allowed value *is* the redaction
    marker. Flat this returns every row in the table."""
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["***"]),
               masks=[MASK("region", "redact")])
    assert governed(catalog, perms, VIEWER).num_rows == 0
    assert same(governed(catalog, perms, VIEWER), reference(catalog, perms, VIEWER))


def test_the_filter_is_structurally_below_the_projection(env, captured):
    """Behaviour alone cannot tell correct nesting from inverted nesting for
    any policy that does not mask the filter column — and inverted nesting
    reintroduces the bug on *every* engine. So assert the shape too."""
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us"]), masks=[MASK("ssn", "hash")])
    governed(catalog, perms, VIEWER)

    sql = captured[-1]
    inner = _inner_block(sql)
    assert "WHERE" in inner, "the filter must live in the inner block"
    select_list = inner[len("(SELECT"):inner.index(" FROM ")].strip()
    assert select_list == "*", f"the filtered block must project nothing: {inner}"
    assert " AS " not in select_list
    # ...and the masks are strictly outside it.
    assert " AS `ssn`" in sql[:sql.index(inner)]


# ---------------------------------------------------------------------------
# 2. Masked means masked, for every value of the type
# ---------------------------------------------------------------------------

def test_a_null_mask_masks_nan_and_the_infinities(env):
    """``NULLIF(amount, amount)`` fails this: NaN <> NaN in ClickHouse, so the
    NaN row ships its real value through the mask. Measured
    ``[None, nan, None, None, None]``. ``if(0, c, NULL)`` gives all null."""
    catalog, store, perms = env
    set_policy(store, masks=[MASK("amount", "null")])
    table = governed(catalog, perms, VIEWER)
    assert table.column("amount").to_pylist() == [None] * 5
    assert same(table, reference(catalog, perms, VIEWER))
    assert pa.types.is_floating(table.schema.field("amount").type), (
        "the mask must keep the column's type"
    )


def test_the_hash_digest_is_the_arrow_paths_digest_value_for_value(env):
    catalog, store, perms = env
    set_policy(store, masks=[MASK("ssn", "hash")])
    got = governed(catalog, perms, VIEWER).column("ssn").to_pylist()
    want = reference(catalog, perms, VIEWER).column("ssn").to_pylist()
    assert got == want
    for value in got:
        if value is None:
            continue  # NULL stays NULL — never the digest of the string "None"
        assert re.fullmatch(r"[0-9a-f]{16}", value), value
        value.encode("utf-8")  # valid UTF-8, not a raw FixedString(32)
    assert None in got, "the fixture's NULL ssn must survive as NULL"


# ---------------------------------------------------------------------------
# 3. NULL in the policy column is never admitted
# ---------------------------------------------------------------------------

def test_a_null_policy_column_never_matches(env):
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us", "eu"]))
    assert same(governed(catalog, perms, VIEWER), reference(catalog, perms, VIEWER))
    assert governed(catalog, perms, VIEWER).num_rows == 3  # the NULL row is out


def test_the_empty_string_is_not_a_key_to_null_rows(env, captured):
    """``ifNull``/``coalesce``/``assumeNotNull`` around the predicate would
    render NULL as '' — and then a policy allowing '' would admit exactly the
    rows whose tenant is unknown."""
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", [""]))
    assert governed(catalog, perms, VIEWER).num_rows == 0

    inner = _inner_block(captured[-1])
    for forbidden in ("ifNull", "coalesce", "assumeNotNull"):
        assert forbidden not in inner, inner


# ---------------------------------------------------------------------------
# 4/5. Anything the renderer cannot express exactly is a refusal
# ---------------------------------------------------------------------------

def test_column_discovery_failure_refuses_rather_than_reads(env, monkeypatch):
    catalog, store, perms = env
    set_policy(store, masks=[MASK("ssn", "redact")])
    monkeypatch.setattr(clickhouse, "schema_of", lambda source: pa.schema([]))
    with pytest.raises(PolicyRenderError):
        governed(catalog, perms, VIEWER)


def test_render_never_falls_back_to_star_with_masks_pending():
    """The unit half of the same invariant, kept next to it: before dialects
    this returned ``select_list='*'``."""
    with pytest.raises(PolicyRenderError):
        SqlPolicy.render(
            PolicyDecision(masks=[("ssn", MaskMode.redact)]), [], dialect=CLICKHOUSE
        )


def test_a_case_mismatched_mask_is_a_refusal_not_plaintext(env):
    """ClickHouse identifiers are case-sensitive (``REGION`` -> code 47), so a
    policy authored as ``SSN`` against a column ``ssn`` would be a silent
    no-op — and the column would be served in the clear."""
    catalog, store, perms = env
    set_policy(store, masks=[MASK("SSN", "redact")])
    with pytest.raises(PolicyRenderError, match="case"):
        governed(catalog, perms, VIEWER)


# ---------------------------------------------------------------------------
# 6. denies_all, through the public route
# ---------------------------------------------------------------------------

def test_denies_all_yields_nothing_through_the_http_path(tmp_path):
    """Asserted at the surface a user actually reaches, over a source that
    contains a column literally named FALSE — because ``where='FALSE'`` and a
    column named ``FALSE`` are the same three characters to a naive renderer."""
    from fastapi.testclient import TestClient

    from laurelin.api import create_app

    remote = tmp_path / "awkward.parquet"
    pq.write_table(
        pa.table({"FALSE": ["a", "b", "c"], "region": ["us", "eu", "us"]}), remote
    )
    ws = Workspace.init(tmp_path / "ws", name="deny")
    app = create_app(ws)
    creds = {"username": "root", "password": "trustno1!"}
    admin = TestClient(app)
    admin.post("/api/v1/auth/setup", json=creds)
    admin.post("/api/v1/auth/login", json=creds)
    admin.put("/api/v1/datasets/awkward/clickhouse",
              json={"source": {"type": "parquet", "path": str(remote)}})
    admin.post("/api/v1/users",
               json={"username": "vic", "password": "password123", "role": "viewer"})
    admin.put("/api/v1/datasets/awkward/policy", json={
        "row_policy": ROWS("region", ["us"], subj="nobody"),
        "column_masks": [],
    })

    viewer = TestClient(app)
    viewer.post("/api/v1/auth/login",
                json={"username": "vic", "password": "password123"})

    # No rule grants vic anything -> denies_all.
    body = viewer.get("/api/v1/datasets/awkward/rows?limit=100").json()
    assert body["rows"] == []
    body = viewer.get("/api/v1/datasets/awkward/rows?limit=1").json()
    assert body["rows"] == []

    # The admin still sees the whole table, so this is a policy result and not
    # a broken read of a file with an awkward column name.
    assert len(admin.get("/api/v1/datasets/awkward/rows?limit=100").json()["rows"]) == 3


def test_denies_all_survives_a_limit(env):
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us"], subj="nobody"))
    assert governed(catalog, perms, VIEWER, limit=100).num_rows == 0
    assert governed(catalog, perms, VIEWER, limit=1).num_rows == 0
    assert governed(catalog, perms, None).num_rows == 0


# ---------------------------------------------------------------------------
# 7. Semantics are pinned per query
# ---------------------------------------------------------------------------

def test_query_settings_beat_whatever_the_profile_says(env, monkeypatch):
    """``transform_null_in`` decides whether NULL matches NULL inside ``IN``.
    Query-level settings override the profile — assert it by forcing the unsafe
    value into the statement *after* ours and requiring the result to hold."""
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us"]))
    before = governed(catalog, perms, VIEWER)

    real = clickhouse.run
    monkeypatch.setattr(
        clickhouse,
        "run",
        lambda sql: real(sql + (", transform_null_in=1" if "SETTINGS" in sql else "")),
    )
    after = governed(catalog, perms, VIEWER)
    assert same(before, after)
    assert same(after, reference(catalog, perms, VIEWER))


def test_the_settings_clause_carries_the_pins_we_rely_on(env, captured):
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us"]))
    governed(catalog, perms, VIEWER)
    sql = captured[-1]
    assert "SETTINGS" in sql and "transform_null_in=0" in sql
    assert "max_memory_usage=" in sql
    # Never this one: it turns NULL into '', which then hashes to
    # e3b0c44298fc1c14 — a real-looking digest for data that is absent.
    assert "schema_inference_make_columns_nullable" not in sql


# ---------------------------------------------------------------------------
# 8. Policy-value fidelity
# ---------------------------------------------------------------------------

HOSTILE_VALUES = ["us\n", "us\\", "a\tb", "o'brien", "us%", "\\\\", "us') OR 1=1 --",
                  "us\\' OR 1=1 --", "us"]


@pytest.mark.parametrize("value", HOSTILE_VALUES, ids=[repr(v) for v in HOSTILE_VALUES])
def test_a_policy_value_selects_exactly_the_rows_it_names(tmp_path, value):
    """The escaper is the only thing turning a policy value into SQL text, so
    a value that arrives shortened or altered silently changes which rows a
    user may see. Rows are compared against the Arrow reference, not counted
    loosely — ``us\\`` must match the row whose region is ``us\\`` and nothing
    else, and the injection payloads must match nothing at all."""
    remote = tmp_path / "vals.parquet"
    regions = [*HOSTILE_VALUES, "plain"]
    pq.write_table(
        pa.table({"id": pa.array(range(len(regions)), pa.int64()), "region": regions}),
        remote,
    )
    ws = Workspace.init(tmp_path / "ws", name="fid")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.register_clickhouse("vals", {"type": "parquet", "path": str(remote)})
    perms = PermissionService(store)
    set_policy(store, row_policy=ROWS("region", [value]), dataset="vals")

    got = catalog.source_table("vals", sql_policy_for=perms.sql_policy_fn(VIEWER))
    want = perms.apply_table_policy(VIEWER, "vals", catalog.source_table("vals"))
    assert got.to_pylist() == want.to_pylist()
    assert got.num_rows == 1, "exactly the row named, no more and no fewer"


def test_injection_payloads_that_are_not_present_match_nothing(env):
    catalog, store, perms = env
    for payload in ("us') OR 1=1 --", "us\\' OR 1=1 --", "' OR '1'='1"):
        set_policy(store, row_policy=ROWS("region", [payload]))
        assert governed(catalog, perms, VIEWER).num_rows == 0, payload


# ---------------------------------------------------------------------------
# Cross-renderer equivalence over the whole policy space
# ---------------------------------------------------------------------------

CASES = [
    ("row filter", dict(row_policy=ROWS("region", ["us"]))),
    ("row filter multi", dict(row_policy=ROWS("region", ["us", "apac"]))),
    ("row filter no match", dict(row_policy=ROWS("region", ["nowhere"]))),
    ("row filter other user", dict(row_policy=ROWS("region", ["us"], subj="someone"))),
    ("mask null", dict(masks=[MASK("ssn", "null")])),
    ("mask redact", dict(masks=[MASK("ssn", "redact")])),
    ("mask hash", dict(masks=[MASK("ssn", "hash")])),
    ("mask float null", dict(masks=[MASK("amount", "null")])),
    ("mask missing column", dict(masks=[MASK("nope", "redact")])),
    ("row + mask", dict(row_policy=ROWS("region", ["eu"]),
                        masks=[MASK("amount", "null")])),
    ("row + two masks", dict(row_policy=ROWS("region", ["eu", "us"]),
                             masks=[MASK("ssn", "redact"), MASK("amount", "null")])),
    ("policy column missing", dict(row_policy=ROWS("ghost", ["x"]))),
]


@pytest.mark.parametrize("label, policy", CASES, ids=[c[0] for c in CASES])
@pytest.mark.parametrize("user", [VIEWER, ADMIN, None], ids=["viewer", "admin", "anon"])
def test_clickhouse_matches_the_reference_policy_engine(env, label, policy, user):
    catalog, store, perms = env
    set_policy(store, **policy)
    assert same(governed(catalog, perms, user), reference(catalog, perms, user)), label


# ---------------------------------------------------------------------------
# 9. A policy defined on text is only a policy where the text agrees
# ---------------------------------------------------------------------------
#
# Both a row filter and a hash mask compare the column's *string* rendering,
# and the three renderers do not share a stringifier: Arrow's row key is
# `pc.cast(col, string)`, its digest input is `str(value)`, DuckDB says
# `CAST(c AS VARCHAR)` and ClickHouse says `toString(c)`. On String, Bool,
# integer and Date columns they agree. On Float64, Decimal and every temporal
# type at least one of them does not — so the same allowlist selects a
# different set of rows depending on which engine runs it, and the SQL side
# was the permissive one. These tests pin the refusal that replaced it.

def test_a_float_row_policy_is_refused_rather_than_meaning_two_things(env):
    """Measured before the guard existed, on this fixture: a policy value of
    ``'1.0'`` matched 0 rows through ClickHouse and 1 row through DuckDB, and a
    value of ``'1'`` matched the other way round. Nobody authoring the policy
    can tell which they are getting, and at 1e10 the divergence stops being
    cosmetic — ``toString`` gives ``'10000000000'`` where the Arrow cast gives
    ``'1e+10'``, so an allowlist naming one tenant admits another.

    So the renderer refuses. The Arrow row API keeps working (it *is* the
    reference), which is the asymmetry we want: the path that cannot prove it
    agrees is the path that stops.
    """
    catalog, store, perms = env
    for value in ("1", "1.0", "10000000000"):
        set_policy(store, row_policy=ROWS("amount", [value]))
        with pytest.raises(PolicyRenderError, match="renders it the way Arrow does"):
            governed(catalog, perms, VIEWER)
        # The reference is unaffected: refusing here is not "float policies are
        # banned", it is "this engine may not be the one to enforce them".
        assert reference(catalog, perms, VIEWER) is not None


def test_a_hash_mask_is_refused_where_the_digest_would_not_join(env):
    """A hash mask exists so the same value yields the same token everywhere;
    a token that only matches itself is a mask that quietly does not do its
    job. ``toString(1.0)`` is ``'1'`` and ``str(1.0)`` is ``'1.0'``, so a
    ClickHouse-hashed float never equals the Arrow digest of the same row.

    Refused with a message that names the way out — ``null`` and ``redact``
    need no text rendering, so they stay available on every column of every
    type. That escape hatch is asserted here, not just described.
    """
    catalog, store, perms = env
    set_policy(store, masks=[MASK("amount", "hash")])
    with pytest.raises(PolicyRenderError, match="'null' and 'redact'"):
        governed(catalog, perms, VIEWER)

    for mode in ("null", "redact"):
        set_policy(store, masks=[MASK("amount", mode)])
        assert same(governed(catalog, perms, VIEWER), reference(catalog, perms, VIEWER))

    # String columns hash exactly, so the common case is untouched.
    set_policy(store, masks=[MASK("ssn", "hash")])
    got = governed(catalog, perms, VIEWER).column("ssn").to_pylist()
    assert same(governed(catalog, perms, VIEWER), reference(catalog, perms, VIEWER))
    assert all(v is None or re.fullmatch(r"[0-9a-f]{16}", v) for v in got)


def test_the_types_that_do_agree_still_work(env):
    """The guard must not be a blanket ban on non-string policy columns: an
    integer tenant key is the ordinary case and all three renderers spell it
    identically."""
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("id", ["1", "3"]))
    got = governed(catalog, perms, VIEWER)
    assert got.column("id").to_pylist() == [1, 3]
    assert same(got, reference(catalog, perms, VIEWER))
