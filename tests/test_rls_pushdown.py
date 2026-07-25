"""Row-level security executed inside the scan.

The policy engine operates on materialized tables, which costs 3–4× at
multi-million-row scale. Where a policy is expressible as an Arrow filter +
projection it is pushed into the Parquet scan instead. These tests pin the
only thing that makes that safe: **the pushed plan and the exact table path
must produce identical rows and values.**
"""

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
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
