"""The ontology object index.

Object queries push into DuckDB, which is fast but still linear. An index
materializes objects into the metadata store so lookups stop touching Parquet
at all — at the cost of storage and a refresh, which is why it is opt-in.

The property that makes it safe: a stale index is never used. It answers only
when it provably reflects both the dataset version and the edit overlay.
"""

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import EditKind, ObjectEdit
from laurelin.ontology import OntologyService, load_ontology
import uuid

ONTOLOGY = """
object_types:
  - api_name: city
    backing_dataset: cities
    primary_key: name
    title_property: name
    properties:
      name: {type: string}
      realm: {type: string}
      pop: {type: integer}
"""


def cities(n: int = 6) -> pa.Table:
    return pa.table({
        "name": [f"city-{i}" for i in range(n)],
        "realm": [["valinor", "beleriand"][i % 2] for i in range(n)],
        "pop": pa.array([100 + i for i in range(n)], type=pa.int64()),
    })


@pytest.fixture()
def svc(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="idx")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("cities", cities())
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    return OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))


def test_index_is_opt_in(svc):
    assert svc.store.object_index_state("city") is None
    assert svc.query("city", limit=10)["total"] == 6, "unindexed queries still work"


def test_indexed_results_match_the_scan(svc):
    """The index must be a faster path to the same answer, never a different
    one."""
    cases = [
        {},
        {"limit": 2},
        {"limit": 2, "offset": 3},
        {"search": "city-1"},
        {"filters": {"name": "city-3"}},   # primary key -> served by the index
        {"filters": {"realm": "valinor"}},  # other property -> served by the scan
        {"filters": {"pop": "103"}},
    ]
    before = {i: svc.query("city", **kw) for i, kw in enumerate(cases)}
    assert svc.reindex("city") == 6
    assert svc.index_is_fresh(svc.ontology.object_type("city"))

    for i, kw in enumerate(cases):
        indexed = svc.query("city", **kw)
        assert {o["__pk"] for o in indexed["objects"]} == {
            o["__pk"] for o in before[i]["objects"]
        }, kw
        assert indexed["total"] == before[i]["total"], kw


def test_a_new_dataset_version_invalidates_the_index(svc):
    svc.reindex("city")
    ot = svc.ontology.object_type("city")
    assert svc.index_is_fresh(ot)

    svc.catalog.append("cities", pa.table({
        "name": ["new-city"], "realm": ["valinor"],
        "pop": pa.array([999], type=pa.int64()),
    }))
    assert not svc.index_is_fresh(ot), "stale index must not be trusted"
    # …and the query still returns the truth, via the scan.
    assert svc.query("city", limit=20)["total"] == 7


def test_an_edit_invalidates_the_index(svc):
    """The overlay is part of the answer, so an edit makes the index stale."""
    svc.reindex("city")
    ot = svc.ontology.object_type("city")
    svc.store.add_object_edit(ObjectEdit(
        id=uuid.uuid4().hex, object_type="city", pk_value="city-0",
        kind=EditKind.update, payload={"realm": "changed"}, actor="t",
    ))
    assert not svc.index_is_fresh(ot)
    assert svc.query("city", filters={"name": "city-0"})["objects"][0]["realm"] == "changed"

    svc.reindex("city")
    assert svc.index_is_fresh(ot)
    assert svc.query("city", filters={"name": "city-0"})["objects"][0]["realm"] == "changed"


def test_only_the_primary_key_is_served_from_the_index(svc):
    """The index accelerates paging, search and key lookups — not arbitrary
    filters.

    ``pk`` is a real indexed column, so a lookup is a b-tree probe. Every other
    property lives in a JSON blob, and extracting it per row measured *slower*
    than the DuckDB scan the index was meant to beat. So those queries are
    deliberately handed back to the scan, which prunes Parquet row groups.
    """
    svc.reindex("city")
    ot = svc.ontology.object_type("city")

    assert svc._index_query(ot, None, {"name": "city-2"}, 10, 0) is not None
    assert svc._index_query(ot, None, {"realm": "valinor"}, 10, 0) is None
    assert svc._index_query(ot, None, {"name": "city-2", "realm": "valinor"}, 10, 0) is None


def test_rls_users_never_read_the_index(svc):
    """The index is shared; a per-user view must never be served from it."""
    svc.reindex("city")
    svc.policy_for = lambda ds: (lambda t: t.slice(0, 2))
    ot = svc.ontology.object_type("city")
    assert svc._index_query(ot, None, None, 10, 0) is None
    assert svc.query("city", limit=10)["total"] == 2, "policy still applies"


def test_dropping_the_index_falls_back(svc):
    svc.reindex("city")
    svc.store.drop_object_index("city")
    assert svc.store.object_index_state("city") is None
    assert svc.query("city", limit=10)["total"] == 6


def test_federated_types_are_not_indexed(svc, tmp_path):
    """Nothing stable to index against, and no version to check freshness on."""
    import pyarrow.parquet as pq

    remote = tmp_path / "remote.parquet"
    pq.write_table(cities(3), remote)
    svc.catalog.store.set_dataset_source(
        "cities", "federated", {"type": "parquet", "path": str(remote)}
    )
    assert svc.reindex("city") == 0
    assert svc.store.object_index_state("city") is None
