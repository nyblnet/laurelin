"""Row-level security executed inside the scan.

The policy engine operates on materialized tables, which costs 3–4× at
multi-million-row scale. Where a policy is expressible as an Arrow filter +
projection it is pushed into the Parquet scan instead. These tests pin the
only thing that makes that safe: **the pushed plan and the exact table path
must produce identical rows and values.**
"""

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core import clickhouse as _clickhouse
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Role, User
from laurelin.core.permissions import PermissionService

VIEWER = User(id="1", username="vic", role=Role.viewer)
OTHER = User(id="2", username="oth", role=Role.viewer)
ADMIN = User(id="3", username="ada", role=Role.admin)


def sales() -> pa.Table:
    n = 60
    return pa.table({
        "id": pa.array(range(n), type=pa.int64()),
        "region": [["us", "eu", "apac"][i % 3] for i in range(n)],
        "rep": [f"rep-{i % 5}" for i in range(n)],
        "amount": pa.array([round(i * 1.25, 2) for i in range(n)], type=pa.float64()),
        "ssn": [f"{i:03d}-00-1234" for i in range(n)],
        "note": [None if i % 7 == 0 else f"note {i}" for i in range(n)],
    })


@pytest.fixture()
def env(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="rls")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("sales", sales())
    return catalog, store, PermissionService(store)


def set_policy(store, row_policy=None, masks=None):
    store.set_dataset_policy("sales", {
        "dataset": "sales",
        "row_policy": row_policy,
        "column_masks": masks or [],
    })


ROWS = lambda col, vals, subj="vic": {  # noqa: E731
    "column": col,
    "rules": [{"subject_kind": "user", "subject": subj, "values": vals}],
}


def pushed(catalog, perms, user) -> pa.Table:
    """Read through the plan-based scan (lazy where possible)."""
    scan = catalog.scan_for("sales", plan_for=perms.arrow_policy_fn(user))
    if isinstance(scan, pa.Table):
        return scan
    return scan.to_table() if hasattr(scan, "to_table") else scan.to_table()


def exact(catalog, perms, user) -> pa.Table:
    """Read through the materializing policy engine."""
    return perms.apply_table_policy(user, "sales", catalog.read("sales"))


def same(a: pa.Table, b: pa.Table) -> bool:
    return a.to_pylist() == b.to_pylist()


# -- equivalence --------------------------------------------------------------

CASES = [
    ("row filter", dict(row_policy=ROWS("region", ["us"]))),
    ("row filter multi", dict(row_policy=ROWS("region", ["us", "apac"]))),
    ("row filter no match", dict(row_policy=ROWS("region", ["nowhere"]))),
    ("row filter other user", dict(row_policy=ROWS("region", ["us"], subj="someone"))),
    ("row filter on nullable", dict(row_policy=ROWS("note", ["note 1", "note 2"]))),
    ("mask null", dict(masks=[{"column": "ssn", "mode": "null", "exempt": []}])),
    ("mask redact", dict(masks=[{"column": "ssn", "mode": "redact", "exempt": []}])),
    ("mask hash", dict(masks=[{"column": "ssn", "mode": "hash", "exempt": []}])),
    ("mask exempt", dict(masks=[{"column": "ssn", "mode": "redact",
                                 "exempt": [{"subject_kind": "user", "subject": "vic"}]}])),
    ("mask missing column", dict(masks=[{"column": "nope", "mode": "redact", "exempt": []}])),
    ("row + mask", dict(row_policy=ROWS("region", ["eu"]),
                        masks=[{"column": "amount", "mode": "null", "exempt": []}])),
    ("row + two masks", dict(row_policy=ROWS("region", ["eu", "us"]),
                             masks=[{"column": "ssn", "mode": "redact", "exempt": []},
                                    {"column": "amount", "mode": "null", "exempt": []}])),
    ("policy column missing", dict(row_policy=ROWS("ghost", ["x"]))),
]


@pytest.mark.parametrize("label, policy", CASES, ids=[c[0] for c in CASES])
@pytest.mark.parametrize("user", [VIEWER, OTHER], ids=["subject", "non-subject"])
def test_pushed_matches_exact(env, label, policy, user):
    catalog, store, perms = env
    set_policy(store, **policy)
    assert same(pushed(catalog, perms, user), exact(catalog, perms, user)), label


def test_admin_and_no_policy_are_unfiltered(env):
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us"]))
    assert perms.arrow_policy_fn(ADMIN)("sales", catalog.arrow_dataset("sales").schema) is None
    assert pushed(catalog, perms, ADMIN).num_rows == 60

    store.set_dataset_policy("sales", None)
    assert perms.arrow_policy_fn(VIEWER)("sales", catalog.arrow_dataset("sales").schema) is None
    assert pushed(catalog, perms, VIEWER).num_rows == 60


def test_anonymous_fails_closed(env):
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us"]))
    assert pushed(catalog, perms, None).num_rows == 0


# -- laziness (the point of the exercise) --------------------------------------

def test_row_policy_stays_lazy_and_hash_does_not(env):
    catalog, store, perms = env
    schema = catalog.arrow_dataset("sales").schema

    set_policy(store, row_policy=ROWS("region", ["us"]))
    plan = perms.arrow_policy_fn(VIEWER)("sales", schema)
    assert plan.lazy, "a row policy must push into the scan"
    assert not isinstance(catalog.scan_for("sales", plan_for=perms.arrow_policy_fn(VIEWER)), pa.Table)

    set_policy(store, masks=[{"column": "ssn", "mode": "redact", "exempt": []}])
    assert perms.arrow_policy_fn(VIEWER)("sales", schema).lazy

    # sha256 has no Arrow compute equivalent, so hashing stays exact.
    set_policy(store, masks=[{"column": "ssn", "mode": "hash", "exempt": []}])
    plan = perms.arrow_policy_fn(VIEWER)("sales", schema)
    assert not plan.lazy
    assert isinstance(catalog.scan_for("sales", plan_for=perms.arrow_policy_fn(VIEWER)), pa.Table)


def test_masked_column_types_match_exact_path(env):
    catalog, store, perms = env
    set_policy(store, masks=[{"column": "amount", "mode": "null", "exempt": []}])
    p, e = pushed(catalog, perms, VIEWER), exact(catalog, perms, VIEWER)
    assert p.schema.field("amount").type == e.schema.field("amount").type

    set_policy(store, masks=[{"column": "amount", "mode": "redact", "exempt": []}])
    p, e = pushed(catalog, perms, VIEWER), exact(catalog, perms, VIEWER)
    assert p.schema.field("amount").type == e.schema.field("amount").type == pa.string()


# -- the SQL renderer ----------------------------------------------------------
#
# Federated tables are scanned by a SQL engine, not by Arrow. The same decision
# drives both renderers, so a policy must mean the same thing either way —
# these tests are what stops the two from drifting.

def sql_read(catalog, perms, user, con=None) -> pa.Table:
    """Read through the SQL renderer, over the same Parquet the Arrow path uses."""
    import duckdb

    schema = catalog.arrow_dataset("sales").schema
    columns = [f.name for f in schema]
    policy = perms.sql_policy_fn(user)(
        "sales", columns, None, {f.name: f.type for f in schema}
    )
    con = con or duckdb.connect()
    con.register("__scan", catalog.arrow_dataset("sales"))
    result = con.execute(
        f"SELECT {policy.select_list} FROM __scan WHERE {policy.where}",
        policy.params,
    ).arrow()
    return result.read_all() if isinstance(result, pa.RecordBatchReader) else result


@pytest.mark.parametrize("label, policy", CASES, ids=[c[0] for c in CASES])
@pytest.mark.parametrize("user", [VIEWER, OTHER], ids=["subject", "non-subject"])
def test_sql_renderer_matches_exact(env, label, policy, user):
    catalog, store, perms = env
    set_policy(store, **policy)
    assert same(sql_read(catalog, perms, user), exact(catalog, perms, user)), label


def test_sql_renderer_handles_hash_masking_natively(env):
    """The Arrow renderer falls back to materializing for hash masks because
    Arrow has no sha256. SQL does, so the federated path expresses it inline —
    and must produce the identical digest."""
    catalog, store, perms = env
    set_policy(store, masks=[{"column": "ssn", "mode": "hash", "exempt": []}])

    assert perms.arrow_policy_fn(VIEWER)(
        "sales", catalog.arrow_dataset("sales").schema
    ).lazy is False, "Arrow must take the exact path for hashing"

    pushed = sql_read(catalog, perms, VIEWER)
    assert same(pushed, exact(catalog, perms, VIEWER))
    schema = catalog.arrow_dataset("sales").schema
    assert "sha256" in perms.sql_policy_fn(VIEWER)(
        "sales", [f.name for f in schema], None, {f.name: f.type for f in schema}
    ).select_list


def test_sql_renderer_denies_all_for_anonymous(env):
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us"]))
    policy = perms.sql_policy_fn(None)("sales", ["id", "region"])
    assert policy.where == "FALSE"
    assert sql_read(catalog, perms, None).num_rows == 0


def test_sql_renderer_is_parameterised(env):
    """Policy values are bound, never interpolated — a value containing a quote
    must not be able to alter the predicate."""
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us' OR 1=1 --"]))
    policy = perms.sql_policy_fn(VIEWER)(
        "sales", ["id", "region"], None, {"id": pa.int64(), "region": pa.string()}
    )
    assert "OR 1=1" not in policy.where
    assert policy.params == ["us' OR 1=1 --"]
    assert sql_read(catalog, perms, VIEWER).num_rows == 0


def test_sql_renderer_quotes_identifiers(env):
    catalog, store, perms = env
    set_policy(store, masks=[{"column": "ssn", "mode": "redact", "exempt": []}])
    policy = perms.sql_policy_fn(VIEWER)("sales", ["id", "ssn"])
    assert '"ssn"' in policy.select_list and '"id"' in policy.select_list


def test_admin_and_no_policy_render_to_passthrough(env):
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us"]))
    admin_policy = perms.sql_policy_fn(ADMIN)("sales", ["id", "region"])
    assert admin_policy.where == "TRUE" and admin_policy.params == []
    assert sql_read(catalog, perms, ADMIN).num_rows == 60


# -- the third renderer --------------------------------------------------------
#
# ClickHouse is not "SQL with different keywords". Its identifier quoting, its
# string literals, its NULL handling and its name resolution all differ from
# DuckDB's, and each difference was a silent leak before it was found. The
# equivalence suite above is the right home for the third renderer because the
# reference it compares against — `apply_table_policy` — is the same one.
#
# Note the reference deliberately is NOT the DuckDB renderer. DuckDB is
# measurably different from Arrow for Float64 and timestamp policy columns
# (CAST(1.0 AS VARCHAR) is '1.0' where the Arrow row key is '1'), so a suite
# that used it as the reference would have certified the divergence. Both SQL
# renderers now refuse the types they cannot spell identically; see
# tests/test_text_agreement.py for the table and tests/test_clickhouse_
# governance.py for what the divergence did before the refusal existed.

clickhouse_only = pytest.mark.skipif(
    not _clickhouse.available(), reason="needs chdb: pip install 'laurelin[clickhouse]'"
)


def clickhouse_read(catalog, perms, user, tmp_path) -> pa.Table:
    """Read through the ClickHouse renderer, over the same rows the Arrow path
    uses — written out once as a Parquet file chdb can open with file()."""
    from laurelin.core.dialects import CLICKHOUSE

    path = tmp_path / "sales.parquet"
    if not path.exists():
        pq.write_table(catalog.read("sales"), path)
    source = {"type": "parquet", "path": str(path)}
    schema = _clickhouse.schema_of(source)
    columns = list(schema.names)
    policy = perms.sql_policy_fn(user, dialect=CLICKHOUSE)(
        "sales", columns, None, {f.name: f.type for f in schema}
    )
    sql = CLICKHOUSE.assemble(
        policy.select_list, _clickhouse.scan_expression(source), policy.where,
        None, "transform_null_in=0",
    )
    return _clickhouse.run(sql)


def _comparable(table: pa.Table):
    """NaN != NaN, so compare it by name rather than by value."""
    return [
        {k: ("nan" if isinstance(v, float) and v != v else v) for k, v in row.items()}
        for row in table.to_pylist()
    ]


@clickhouse_only
@pytest.mark.parametrize("label, policy", CASES, ids=[c[0] for c in CASES])
@pytest.mark.parametrize("user", [VIEWER, OTHER], ids=["subject", "non-subject"])
def test_clickhouse_renderer_matches_exact(env, tmp_path, label, policy, user):
    catalog, store, perms = env
    set_policy(store, **policy)
    got = clickhouse_read(catalog, perms, user, tmp_path)
    assert _comparable(got) == _comparable(exact(catalog, perms, user)), label


@clickhouse_only
def test_clickhouse_hash_mask_equals_the_arrow_digest(env, tmp_path):
    catalog, store, perms = env
    set_policy(store, masks=[{"column": "ssn", "mode": "hash", "exempt": []}])
    got = clickhouse_read(catalog, perms, VIEWER, tmp_path).column("ssn").to_pylist()
    want = exact(catalog, perms, VIEWER).column("ssn").to_pylist()
    assert got == want
    assert all(len(v) == 16 and v.islower() and v.isalnum() for v in got)


@clickhouse_only
def test_clickhouse_renderer_denies_all_for_anonymous(env, tmp_path):
    from laurelin.core.dialects import CLICKHOUSE

    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", ["us"]))
    policy = perms.sql_policy_fn(None, dialect=CLICKHOUSE)("sales", ["id", "region"])
    assert policy.where == "FALSE"
    assert clickhouse_read(catalog, perms, None, tmp_path).num_rows == 0


@clickhouse_only
@pytest.mark.parametrize("payload", ["us') OR 1=1 --", "us\\' OR 1=1 --"])
def test_clickhouse_renderer_cannot_be_escaped_out_of(env, tmp_path, payload):
    """The backslash payload is the one that defeats quote-doubling: ClickHouse
    honours backslash escapes inside string literals and DuckDB does not, so an
    escaper that only doubles quotes lets the second value close the string."""
    catalog, store, perms = env
    set_policy(store, row_policy=ROWS("region", [payload]))
    assert clickhouse_read(catalog, perms, VIEWER, tmp_path).num_rows == 0
