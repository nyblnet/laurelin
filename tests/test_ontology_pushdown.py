"""Ontology query pushdown.

Object queries run inside DuckDB over the backing Parquet instead of
materializing the whole dataset in Python. These tests pin the property that
makes that safe: **the pushdown path and the exact in-memory path must return
identical results** — including ordering, the edit overlay, search, filters,
and paging.
"""

import uuid

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import EditKind, ObjectEdit
from laurelin.ontology import OntologyService, load_ontology

ONTOLOGY = """
object_types:
  - api_name: part
    backing_dataset: parts
    primary_key: sku
    title_property: label
    properties:
      sku:    {type: string}
      label:  {type: string}
      family: {type: string}
      qty:    {type: integer}
      price:  {type: float}
"""


def table(n: int = 40) -> pa.Table:
    return pa.table({
        "sku": [f"SKU-{i:03d}" for i in range(n)],
        "label": [f"Widget {i}" for i in range(n)],
        "family": [["alpha", "beta", "gamma"][i % 3] for i in range(n)],
        "qty": pa.array([i * 2 for i in range(n)], type=pa.int64()),
        "price": pa.array([round(i * 1.5, 2) for i in range(n)], type=pa.float64()),
        "ignored": [f"not-a-property-{i}" for i in range(n)],
    })


@pytest.fixture()
def svc(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="pd")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("parts", table())
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    return OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))


def slow(svc, **kw):
    """The exact in-memory path, forced by making the dataset look policied."""
    original = svc.policy_for
    svc.policy_for = lambda ds: (lambda t: t)   # identity: no filtering, but not None
    try:
        return svc.query("part", **kw)
    finally:
        svc.policy_for = original


def fast(svc, **kw):
    assert svc._sql_query(
        svc.ontology.object_type("part"),
        kw.get("search"), kw.get("filters"),
        kw.get("limit", 100), kw.get("offset", 0),
    ) is not None, "expected the pushdown path to be taken"
    return svc.query("part", **kw)


CASES = [
    {},
    {"limit": 5},
    {"limit": 5, "offset": 10},
    {"limit": 3, "offset": 38},
    {"search": "widget 1"},
    {"search": "WIDGET 2", "limit": 4},
    {"search": "nothing-matches-this"},
    {"filters": {"family": "beta"}},
    {"filters": {"family": "beta"}, "limit": 2, "offset": 1},
    {"filters": {"qty": "10"}},
    {"filters": {"sku": "SKU-007"}},
    {"search": "widget", "filters": {"family": "gamma"}, "limit": 3},
]


@pytest.mark.parametrize("kw", CASES)
def test_pushdown_matches_in_memory(svc, kw):
    assert fast(svc, **kw) == slow(svc, **kw)


def test_projection_drops_undeclared_columns(svc):
    obj = svc.query("part", limit=1)["objects"][0]
    assert "ignored" not in obj
    assert set(obj) == {"sku", "label", "family", "qty", "price", "__pk", "__title"}
    assert obj["__title"] == "Widget 0"
    # Types survive the round trip through DuckDB.
    assert isinstance(obj["qty"], int) and isinstance(obj["price"], float)


# -- the edit overlay ---------------------------------------------------------

def edit(svc, pk: str, kind: str, payload: dict):
    svc.store.add_object_edit(
        ObjectEdit(id=uuid.uuid4().hex, object_type="part", pk_value=pk,
                   kind=EditKind(kind), payload=payload, actor="tester")
    )


def apply_edits(svc):
    edit(svc, "SKU-001", "update", {"label": "Renamed"})
    edit(svc, "SKU-002", "delete", {})
    edit(svc, "SKU-900", "create",
         {"sku": "SKU-900", "label": "Invented", "family": "beta",
          "qty": 7, "price": 1.0})


@pytest.mark.parametrize("kw", CASES)
def test_pushdown_matches_in_memory_with_overlay(svc, kw):
    apply_edits(svc)
    assert fast(svc, **kw) == slow(svc, **kw)


def test_overlay_semantics_through_pushdown(svc):
    apply_edits(svc)
    result = svc.query("part", limit=100)
    by_pk = {o["__pk"]: o for o in result["objects"]}

    assert by_pk["SKU-001"]["label"] == "Renamed"      # update applies
    assert by_pk["SKU-001"]["qty"] == 2                # untouched fields survive
    assert "SKU-002" not in by_pk                      # delete removes
    assert by_pk["SKU-900"]["label"] == "Invented"     # create adds
    assert result["total"] == 40 - 1 + 1

    # An updated object keeps its position; a created one is appended last.
    pks = [o["__pk"] for o in result["objects"]]
    assert pks.index("SKU-001") == 1
    assert pks[-1] == "SKU-900"


def test_get_uses_point_lookup(svc):
    apply_edits(svc)
    assert svc.get("part", "SKU-003")["label"] == "Widget 3"
    assert svc.get("part", "SKU-001")["label"] == "Renamed"
    assert svc.get("part", "SKU-002") is None
    assert svc.get("part", "SKU-900")["label"] == "Invented"
    assert svc.get("part", "nope") is None


def test_duplicate_primary_keys_last_wins(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="dup")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("parts", pa.table({
        "sku": ["A", "B", "A"],
        "label": ["first", "only-b", "second"],
        "family": ["x", "y", "z"],
        "qty": pa.array([1, 2, 3], type=pa.int64()),
        "price": pa.array([1.0, 2.0, 3.0], type=pa.float64()),
    }))
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    svc = OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))
    result = svc.query("part", limit=10)
    assert result["total"] == 2
    assert {o["__pk"]: o["label"] for o in result["objects"]} == {
        "A": "second", "B": "only-b",
    }


def test_rls_falls_back_to_exact_path(svc):
    """A dataset needing per-user filtering must not take the pushdown."""
    svc.policy_for = lambda ds: (lambda t: t.slice(0, 3))
    assert svc._sql_query(svc.ontology.object_type("part"), None, None, 10, 0) is None
    assert svc.query("part", limit=10)["total"] == 3


def test_appended_multi_part_dataset(svc):
    """Pushdown reads a version's whole manifest, not just one file."""
    svc.catalog.append("parts", pa.table({
        "sku": ["SKU-500"], "label": ["Appended"], "family": ["alpha"],
        "qty": pa.array([1], type=pa.int64()),
        "price": pa.array([9.0], type=pa.float64()),
        "ignored": ["x"],
    }))
    result = svc.query("part", limit=100)
    assert result["total"] == 41
    assert result["objects"][-1]["__pk"] == "SKU-500"
