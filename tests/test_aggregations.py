"""Aggregating ontology objects.

A dashboard could always chart the *backing dataset* with SQL. The reason not
to is that it sees raw rows rather than objects, so it misses the edit overlay
entirely — it answers from data an action has already changed, confidently and
wrongly. These aggregate the same object set that `query` pages: overlay,
policy and all.

The tests that matter are the ones asserting agreement with the object list,
because "faster way to the same answer" stops being true silently.
"""

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.approvals import ChangeTicket as _ChangeTicket
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import EditKind, ObjectEdit
from laurelin.ontology import OntologyService, load_ontology

_TICKET = _ChangeTicket(kind="local", actor="test")

ONTOLOGY = """
object_types:
  - api_name: order
    backing_dataset: orders
    primary_key: order_id
    title_property: order_id
    properties:
      order_id: {type: string}
      region: {type: string}
      status: {type: string}
      amount: {type: float}
"""

REGIONS = ["us", "eu", "us", "apac", "eu", "us"]
STATUSES = ["open", "shipped", "shipped", "open", "shipped", "open"]
AMOUNTS = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]


def orders() -> pa.Table:
    return pa.table({
        "order_id": [f"o{i}" for i in range(len(REGIONS))],
        "region": REGIONS,
        "status": STATUSES,
        "amount": pa.array(AMOUNTS, type=pa.float64()),
    })


@pytest.fixture()
def svc(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="agg")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("orders", orders())
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    return OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))


def by(result, key="region"):
    return {g[key]: g for g in result["groups"]}


# -- the basics ---------------------------------------------------------------

def test_count_by_group(svc):
    got = svc.aggregate("order", group_by=["region"],
                        metrics=[{"op": "count", "alias": "n"}])
    assert by(got) == {
        "us": {"region": "us", "n": 3},
        "eu": {"region": "eu", "n": 2},
        "apac": {"region": "apac", "n": 1},
    }
    assert got["group_count"] == 3
    assert got["truncated"] is False


def test_several_metrics_at_once(svc):
    got = by(svc.aggregate("order", group_by=["region"], metrics=[
        {"op": "sum", "property": "amount", "alias": "total"},
        {"op": "avg", "property": "amount", "alias": "mean"},
        {"op": "min", "property": "amount", "alias": "lo"},
        {"op": "max", "property": "amount", "alias": "hi"},
        {"op": "count", "alias": "n"},
    ]))
    assert got["us"]["total"] == 100.0        # 10 + 30 + 60
    assert got["us"]["mean"] == pytest.approx(100 / 3)
    assert got["us"]["lo"] == 10.0
    assert got["us"]["hi"] == 60.0
    assert got["us"]["n"] == 3


def test_no_group_by_aggregates_everything(svc):
    got = svc.aggregate("order", metrics=[
        {"op": "count", "alias": "n"},
        {"op": "sum", "property": "amount", "alias": "total"},
    ])
    assert got["groups"] == [{"n": 6, "total": 210.0}]


def test_group_by_several_properties(svc):
    got = svc.aggregate("order", group_by=["region", "status"],
                        metrics=[{"op": "count", "alias": "n"}])
    keyed = {(g["region"], g["status"]): g["n"] for g in got["groups"]}
    assert keyed[("us", "open")] == 2
    assert keyed[("us", "shipped")] == 1
    assert keyed[("eu", "shipped")] == 2


def test_count_distinct(svc):
    got = svc.aggregate("order", metrics=[
        {"op": "count_distinct", "property": "region", "alias": "regions"},
    ])
    assert got["groups"][0]["regions"] == 3


def test_biggest_group_first(svc):
    """A chart wants the interesting rows, and the row cap should keep them."""
    got = svc.aggregate("order", group_by=["region"],
                        metrics=[{"op": "count", "alias": "n"}])
    assert [g["n"] for g in got["groups"]] == [3, 2, 1]


# -- agreement with the object list -------------------------------------------

def test_the_count_matches_the_object_list(svc):
    listed = svc.query("order", limit=1000)["total"]
    agg = svc.aggregate("order", metrics=[{"op": "count", "alias": "n"}])
    assert agg["groups"][0]["n"] == listed


def test_filters_and_search_apply(svc):
    filtered = svc.aggregate("order", group_by=["region"],
                             filters={"status": "open"},
                             metrics=[{"op": "count", "alias": "n"}])
    assert by(filtered) == {
        "us": {"region": "us", "n": 2}, "apac": {"region": "apac", "n": 1},
    }
    searched = svc.aggregate("order", metrics=[{"op": "count", "alias": "n"}],
                             search="apac")
    assert searched["groups"][0]["n"] == 1


def test_the_edit_overlay_is_included(svc):
    """The whole reason not to just chart the backing dataset."""
    svc.store.add_object_edit(ObjectEdit(
        id="e1", object_type="order", pk_value="o0",
        kind=EditKind.update, payload={"region": "apac"}, actor="t",
    ))
    got = by(svc.aggregate("order", group_by=["region"],
                           metrics=[{"op": "count", "alias": "n"}]))
    assert got["apac"]["n"] == 2, "an updated object must move groups"
    assert got["us"]["n"] == 2

    svc.store.add_object_edit(ObjectEdit(
        id="e2", object_type="order", pk_value="o1",
        kind=EditKind.delete, payload={}, actor="t",
    ))
    total = svc.aggregate("order", metrics=[{"op": "count", "alias": "n"}])
    assert total["groups"][0]["n"] == 5, "a deleted object must not be counted"


def test_a_row_policy_still_applies(svc):
    """Aggregates must not become a way to read rows you can't list."""
    unpoliced = svc.aggregate("order", metrics=[{"op": "count", "alias": "n"}])
    assert unpoliced["groups"][0]["n"] == 6

    # This user sees only the first two rows.
    svc.policy_for = lambda ds: (lambda t: t.slice(0, 2))
    policed = svc.aggregate("order", metrics=[
        {"op": "count", "alias": "n"},
        {"op": "sum", "property": "amount", "alias": "total"},
    ])
    assert policed["groups"][0]["n"] == 2
    assert policed["groups"][0]["total"] == 30.0
    assert policed["groups"][0]["n"] == svc.query("order", limit=99)["total"]


def test_the_pushdown_is_what_normally_runs(svc, monkeypatch):
    """Both paths return the same answers, which is exactly why a regression
    that quietly routed everything through Python would go unnoticed — until
    someone aggregated a million objects."""
    used = []
    monkeypatch.setattr(
        OntologyService, "_aggregate_python",
        lambda self, *a, **k: used.append("python") or {"groups": [], "group_count": 0,
                                                        "truncated": False},
    )
    got = svc.aggregate("order", group_by=["region"],
                        metrics=[{"op": "count", "alias": "n"}])
    assert used == [], "an ordinary aggregation must push into DuckDB"
    assert len(got["groups"]) == 3

    # …and a policy the scan can't express still falls back rather than
    # skipping enforcement.
    svc.policy_for = lambda ds: (lambda t: t.slice(0, 2))
    svc.aggregate("order", metrics=[{"op": "count", "alias": "n"}])
    assert used == ["python"]


# -- limits and validation ----------------------------------------------------

def test_too_many_groups_is_truncated_and_says_so(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="many")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    n = 300
    catalog.write("orders", pa.table({
        "order_id": [f"o{i}" for i in range(n)],
        "region": [f"r{i}" for i in range(n)],
        "status": ["open"] * n,
        "amount": pa.array([1.0] * n, type=pa.float64()),
    }))
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    svc = OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))

    got = svc.aggregate("order", group_by=["region"],
                        metrics=[{"op": "count", "alias": "n"}], limit=10)
    assert len(got["groups"]) == 10
    assert got["group_count"] == n
    assert got["truncated"] is True, "a capped result must not look complete"


@pytest.mark.parametrize("kwargs, message", [
    ({"group_by": ["nope"]}, "Unknown group_by property"),
    ({"metrics": [{"op": "hack", "property": "amount"}]}, "Unknown aggregation"),
    ({"metrics": [{"op": "sum", "property": "nope"}]}, "Unknown property"),
    ({"metrics": [{"op": "sum"}]}, "requires a property"),
])
def test_invalid_requests_are_rejected(svc, kwargs, message):
    with pytest.raises(ValueError, match=message):
        svc.aggregate("order", **kwargs)


def test_the_op_is_an_allowlist_not_a_passthrough(svc):
    """The op becomes a SQL function name, so anything unlisted must be
    refused rather than interpolated."""
    with pytest.raises(ValueError, match="Unknown aggregation"):
        svc.aggregate("order", metrics=[
            {"op": "count(*) FROM x --", "property": "amount"},
        ])


def test_unknown_object_type(svc):
    with pytest.raises(KeyError):
        svc.aggregate("ghost", metrics=[{"op": "count"}])


# -- HTTP ---------------------------------------------------------------------

def test_aggregate_over_http(tmp_path):
    from fastapi.testclient import TestClient

    from laurelin.api import create_app

    ws = Workspace.init(tmp_path / "ws", name="http")
    store = MetadataStore(ws.metadata_path)
    DatasetCatalog(ws, store).write("orders", orders())
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    client = TestClient(create_app(ws, no_auth=True))

    r = client.post("/api/v1/ontology/objects/order/aggregate", json={
        "group_by": ["region"],
        "metrics": [{"op": "sum", "property": "amount", "alias": "total"}],
    })
    assert r.status_code == 200
    assert {g["region"]: g["total"] for g in r.json()["groups"]} == {
        "us": 100.0, "eu": 70.0, "apac": 40.0,
    }

    bad = client.post("/api/v1/ontology/objects/order/aggregate",
                      json={"metrics": [{"op": "nope"}]})
    assert bad.status_code == 400
    assert "Unknown aggregation" in bad.json()["detail"]


def test_the_aggregate_names_the_callers_masked_properties(tmp_path):
    """Grouping by a masked property is allowed — the mask holds and every
    object lands in one "***" group — but that chart is inexplicable unless
    the picker can say "masked for you" the way the dataset path's pickers
    do. `masked_properties` is disclosure only: which of *your* masks cover
    declared properties. Enforcement stays in the scan, and a caller with no
    masks gets an empty list, not an enumeration of anyone else's policy."""
    from fastapi.testclient import TestClient

    from laurelin.api import create_app
    from laurelin.core.auth import hash_password
    from laurelin.core.models import Role, User

    ws = Workspace.init(tmp_path / "ws", name="mask")
    store = MetadataStore(ws.metadata_path)
    DatasetCatalog(ws, store).write("orders", orders())
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    store.create_user(User(id="a", username="ana", role=Role.editor),
                      hash_password("pw"))
    app = create_app(ws)
    ana = TestClient(app)
    assert ana.post("/api/v1/auth/login",
                    json={"username": "ana", "password": "pw"}).status_code == 200

    body = {"group_by": ["region"], "metrics": [{"op": "count", "alias": "n"}]}
    r = ana.post("/api/v1/ontology/objects/order/aggregate", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["masked_properties"] == []  # no policy, nothing to report

    store.set_dataset_policy("orders", {
        "row_policy": None,
        "column_masks": [{"column": "region", "mode": "redact"}],
    }, ticket=_TICKET)
    r = ana.post("/api/v1/ontology/objects/order/aggregate", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["masked_properties"] == ["region"]
    # The mask itself still holds: one group, the sentinel.
    assert {g["region"] for g in out["groups"]} == {"***"}
