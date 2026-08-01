"""Object order, which is the property paging is built on.

Objects are ordered by the backing dataset's file order, with created objects
after it. Three paths compute that order — the materialization, the DuckDB
pushdown and the in-memory replay — and if any two of them disagree, paging is
silently broken in the way nobody notices until an object is missing from page
1 and duplicated on page 2.

The ordinal rule, in full:

    ord(base row i)  = i                         # file order
    ord(created obj) = ORD_CREATED_BASE + edit_seq

and a create for a key that already exists keeps that key's existing ordinal,
because it is a replacement rather than a new object.
"""

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.ontology import OntologyService, load_ontology
from laurelin.ontology.store import ORD_CREATED_BASE

ONTOLOGY = """
object_types:
  - api_name: city
    backing_dataset: cities
    primary_key: name
    title_property: name
    properties:
      name: {type: string}
      realm: {type: string}
actions:
  - api_name: found
    object_type: city
    kind: create
    parameters:
      name: {type: string, required: true}
      realm: {type: string, required: true}
  - api_name: rename
    object_type: city
    kind: update
    parameters:
      realm: {type: string, required: true}
  - api_name: raze
    object_type: city
    kind: delete
    parameters: {}
"""

# Deliberately NOT in key order: when file order happens to match key order — as
# it does in most fixtures — an implementation that wrongly sorted by key looks
# perfectly correct.
NAMES = ["city-c", "city-a", "city-d", "city-b"]


def make(tmp_path, name="ord"):
    ws = Workspace.init(tmp_path / name, name=name)
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("cities", pa.table({"name": NAMES, "realm": ["valinor"] * 4}))
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    return OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))


@pytest.fixture()
def svc(tmp_path):
    return make(tmp_path)


def order(svc, **kw) -> list[str]:
    return [o["__pk"] for o in svc.query("city", limit=50, **kw)["objects"]]


def scan_order(svc) -> list[str]:
    """The order the pushdown computes, with the materialization out of the way."""
    ot = svc.ontology.object_type("city")
    return [o["__pk"] for o in svc._sql_query(ot, None, None, 50, 0)["objects"]]


def memory_order(svc) -> list[str]:
    ot = svc.ontology.object_type("city")
    return [o["__pk"] for o in svc._materialize(ot)]


def test_all_three_paths_agree_on_order(svc):
    svc.reindex("city")
    svc.apply_action("found", pk=None, parameters={"name": "new-1", "realm": "r"})
    svc.apply_action("found", pk=None, parameters={"name": "new-2", "realm": "r"})
    svc.apply_action("rename", pk="city-a", parameters={"realm": "x"})

    assert order(svc) == scan_order(svc) == memory_order(svc)
    assert order(svc) == [*NAMES, "new-1", "new-2"], "file order, then creates in edit order"


def test_creates_do_not_all_share_one_ordinal(svc):
    """Every create used to carry the same sentinel ordinal, so ORDER BY left
    them tied and their relative order was whatever the engine felt like."""
    svc.reindex("city")
    for i in range(5):
        svc.apply_action("found", pk=None, parameters={"name": f"n{i}", "realm": "r"})
    ords = [r["ord"] for r in svc.store.object_index_rows(
        "city", [f"n{i}" for i in range(5)])]
    assert len(set(ords)) == 5, "distinct positions"
    assert all(o > ORD_CREATED_BASE for o in ords), "after every base row"
    assert order(svc)[4:] == ["n0", "n1", "n2", "n3", "n4"]


def test_a_create_over_an_existing_key_keeps_its_position(svc):
    """A create for a key that is already there is a replacement, not a new
    object, so it must not jump to the end of the list."""
    svc.reindex("city")
    svc.apply_action("found", pk=None, parameters={"name": "city-d", "realm": "again"})

    assert order(svc) == NAMES, "position unchanged"
    assert order(svc) == scan_order(svc) == memory_order(svc)
    assert svc.query("city", limit=50)["total"] == 4, "replaced, not duplicated"
    assert [o["realm"] for o in svc.query("city", filters={"name": "city-d"})[
        "objects"]] == ["again"]


def test_the_pushdown_no_longer_duplicates_a_replaced_key(svc):
    """The divergence this fixed: the pushdown emitted the base row *and* the
    created row, so it counted 5 objects where the replay counted 4."""
    svc.apply_action("found", pk=None, parameters={"name": "city-b", "realm": "dup"})
    assert len(scan_order(svc)) == len(memory_order(svc)) == 4
    assert sorted(scan_order(svc)) == sorted(memory_order(svc))


def test_a_rebuild_reproduces_the_same_numbers(svc):
    """Rebuilding must not renumber. An `enumerate` here silently reshuffles
    page 1 — precisely the bug the ordinal column was added to fix."""
    svc.reindex("city")
    for i in range(3):
        svc.apply_action("found", pk=None, parameters={"name": f"m{i}", "realm": "r"})
    incremental = {r["pk"]: r["ord"] for r in svc.store.object_index_rows(
        "city", [*NAMES, "m0", "m1", "m2"])}

    svc.reindex("city")
    rebuilt = {r["pk"]: r["ord"] for r in svc.store.object_index_rows(
        "city", [*NAMES, "m0", "m1", "m2"])}
    assert rebuilt == incremental


def test_deleting_and_recreating_gives_a_new_position(svc):
    svc.reindex("city")
    svc.apply_action("raze", pk="city-a", parameters={})
    svc.apply_action("found", pk=None, parameters={"name": "city-a", "realm": "back"})
    assert order(svc) == ["city-c", "city-d", "city-b", "city-a"]
    assert order(svc) == scan_order(svc) == memory_order(svc)


@pytest.mark.parametrize("page_size", [1, 2, 3, 5])
def test_paging_returns_every_object_exactly_once(svc, page_size):
    svc.reindex("city")
    for i in range(6):
        svc.apply_action("found", pk=None, parameters={"name": f"p{i}", "realm": "r"})

    seen, offset = [], 0
    while True:
        page = svc.query("city", limit=page_size, offset=offset)["objects"]
        if not page:
            break
        seen.extend(o["__pk"] for o in page)
        offset += page_size
    assert len(seen) == len(set(seen)) == 10
    assert seen == order(svc)


def test_two_racing_creates_take_different_positions(tmp_path):
    """Both replicas compute their own ordinal, and it must not be the same
    number. Dense `n, n+1` fails exactly here: n is the base row count, which
    both writers read as identical."""
    svc = make(tmp_path, name="race")
    svc.reindex("city")
    a = svc.apply_action("found", pk=None, parameters={"name": "ra", "realm": "r"})
    b = svc.apply_action("found", pk=None, parameters={"name": "rb", "realm": "r"})
    assert a.edit_seq != b.edit_seq
    rows = {r["pk"]: r["ord"] for r in svc.store.object_index_rows("city", ["ra", "rb"])}
    assert rows["ra"] != rows["rb"]
