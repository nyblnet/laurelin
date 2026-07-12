"""Tests for row-level security and column masking."""

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import (
    ColumnMask,
    DatasetPolicy,
    MaskMode,
    PolicySubject,
    Role,
    RowPolicy,
    RowRule,
    SubjectKind,
    User,
)
from laurelin.core.permissions import PermissionService

ONTOLOGY = """
object_types:
  - api_name: sale
    backing_dataset: sales
    primary_key: id
    properties:
      id: {type: string}
      region: {type: string}
      amount: {type: float}
      ssn: {type: string}
"""


@pytest.fixture()
def ws(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="rls")
    DatasetCatalog(ws, MetadataStore(ws.metadata_path)).write(
        "sales",
        pa.table(
            {
                "id": ["s1", "s2", "s3", "s4"],
                "region": ["us", "eu", "us", None],
                "amount": [10.0, 20.0, 30.0, 40.0],
                "ssn": ["111", "222", "333", "444"],
            }
        ),
    )
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    return ws


@pytest.fixture()
def store(ws):
    return MetadataStore(ws.metadata_path)


@pytest.fixture()
def perms(store):
    return PermissionService(store)


@pytest.fixture()
def catalog(ws, store):
    return DatasetCatalog(ws, store)


VIEWER = User(id="1", username="vic", role=Role.viewer)
ADMIN = User(id="2", username="ada", role=Role.admin)


def _set_policy(store, row_policy=None, masks=None):
    dp = DatasetPolicy(dataset="sales", row_policy=row_policy, column_masks=masks or [])
    store.set_dataset_policy(
        "sales",
        {
            "row_policy": dp.row_policy.model_dump(mode="json") if dp.row_policy else None,
            "column_masks": [m.model_dump(mode="json") for m in dp.column_masks],
        },
    )


# -- unit: policy engine -----------------------------------------------------

def test_row_policy_filters_by_allowed_values(perms, store, catalog):
    _set_policy(
        store,
        row_policy=RowPolicy(
            column="region",
            rules=[RowRule(subject_kind=SubjectKind.user, subject="vic", values=["us"])],
        ),
    )
    table = perms.apply_table_policy(VIEWER, "sales", catalog.read("sales"))
    assert table.num_rows == 2  # only the two us rows
    assert set(table.column("region").to_pylist()) == {"us"}
    # admin bypasses
    assert perms.apply_table_policy(ADMIN, "sales", catalog.read("sales")).num_rows == 4


def test_row_policy_default_deny_for_unmatched_user(perms, store, catalog):
    _set_policy(
        store,
        row_policy=RowPolicy(
            column="region",
            rules=[RowRule(subject_kind=SubjectKind.user, subject="someone_else", values=["us"])],
        ),
    )
    # vic matches no rule -> zero rows
    assert perms.apply_table_policy(VIEWER, "sales", catalog.read("sales")).num_rows == 0


def test_null_policy_column_values_are_excluded(perms, store, catalog):
    _set_policy(
        store,
        row_policy=RowPolicy(
            column="region",
            rules=[RowRule(subject_kind=SubjectKind.everyone, values=["us", "eu"])],
        ),
    )
    table = perms.apply_table_policy(VIEWER, "sales", catalog.read("sales"))
    # s4 has region=None -> not in {us,eu} -> excluded (fail closed)
    assert table.num_rows == 3
    assert None not in table.column("region").to_pylist()


def test_missing_policy_column_fails_closed(perms, store, catalog):
    _set_policy(store, row_policy=RowPolicy(column="nonexistent", rules=[]))
    assert perms.apply_table_policy(VIEWER, "sales", catalog.read("sales")).num_rows == 0


@pytest.mark.parametrize(
    "mode,check",
    [
        (MaskMode.redact, lambda vals: set(vals) == {"***"}),
        (MaskMode.null, lambda vals: all(v is None for v in vals)),
        (MaskMode.hash, lambda vals: all(v != "111" and len(v) == 16 for v in vals if v)),
    ],
)
def test_column_masking_modes(perms, store, catalog, mode, check):
    _set_policy(store, masks=[ColumnMask(column="ssn", mode=mode)])
    table = perms.apply_table_policy(VIEWER, "sales", catalog.read("sales"))
    assert check(table.column("ssn").to_pylist())
    # admin sees the real values
    admin_table = perms.apply_table_policy(ADMIN, "sales", catalog.read("sales"))
    assert admin_table.column("ssn").to_pylist() == ["111", "222", "333", "444"]


def test_mask_exemption(perms, store, catalog):
    _set_policy(
        store,
        masks=[
            ColumnMask(
                column="ssn",
                mode=MaskMode.redact,
                exempt=[PolicySubject(subject_kind=SubjectKind.user, subject="vic")],
            )
        ],
    )
    # vic is exempt -> sees real ssn
    table = perms.apply_table_policy(VIEWER, "sales", catalog.read("sales"))
    assert table.column("ssn").to_pylist() == ["111", "222", "333", "444"]


# -- HTTP: enforcement across every read surface -----------------------------

CREDS = {"username": "root", "password": "trustno1!"}


@pytest.fixture()
def clients(ws):
    app = create_app(ws)
    admin = TestClient(app)
    assert admin.post("/api/v1/auth/setup", json=CREDS).status_code == 200
    assert admin.post("/api/v1/auth/login", json=CREDS).status_code == 200
    admin.post("/api/v1/users", json={"username": "vic", "password": "password123", "role": "viewer"})
    viewer = TestClient(app)
    viewer.post("/api/v1/auth/login", json={"username": "vic", "password": "password123"})
    return admin, viewer


def test_policy_enforced_on_rows_query_and_ontology(clients):
    admin, viewer = clients
    policy = {
        "row_policy": {
            "column": "region",
            "rules": [{"subject_kind": "user", "subject": "vic", "values": ["us"]}],
        },
        "column_masks": [{"column": "ssn", "mode": "redact", "exempt": []}],
    }
    assert admin.put("/api/v1/datasets/sales/policy", json=policy).status_code == 200

    # Row API: viewer sees 2 us rows, ssn redacted; row_count reflects filtered total
    vr = viewer.get("/api/v1/datasets/sales/rows").json()
    assert vr["row_count"] == 2
    assert all(r["region"] == "us" and r["ssn"] == "***" for r in vr["rows"])

    # SQL workbench: aggregate respects RLS (only us rows: 10+30=40)
    q = viewer.post("/api/v1/query", json={"sql": "SELECT sum(amount) AS t FROM sales"}).json()
    assert q["rows"][0]["t"] == 40.0

    # Ontology objects (objects ARE rows) — no bypass
    vo = viewer.get("/api/v1/ontology/objects/sale").json()
    assert vo["total"] == 2
    assert all(o["ssn"] == "***" for o in vo["objects"])

    # Admin is unaffected everywhere
    assert admin.get("/api/v1/datasets/sales/rows").json()["row_count"] == 4
    assert admin.post("/api/v1/query", json={"sql": "SELECT sum(amount) AS t FROM sales"}).json()["rows"][0]["t"] == 100.0


def test_policy_endpoints_admin_only_and_validation(clients):
    admin, viewer = clients
    assert viewer.get("/api/v1/dataset-policies").status_code == 403
    assert viewer.put("/api/v1/datasets/sales/policy", json={}).status_code == 403
    assert admin.get("/api/v1/dataset-policies").status_code == 200
    assert admin.put("/api/v1/datasets/nope/policy", json={}).status_code == 404
    # empty policy clears it
    admin.put("/api/v1/datasets/sales/policy", json={"row_policy": {"column": "region", "rules": []}})
    assert admin.put("/api/v1/datasets/sales/policy", json={}).status_code == 200
    pols = {p["dataset"]: p["policy"] for p in admin.get("/api/v1/dataset-policies").json()}
    assert pols["sales"] is None
