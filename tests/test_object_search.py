"""Accelerated object search.

Object search is a case-insensitive *substring* match, and that definition is
shared with the DuckDB scan path. So this is trigram indexing, which makes
``LIKE '%needle%'`` fast while preserving exactly what it matches — not
full-text search, which would quietly redefine search itself: token matching
finds "minas" in "Minas Tirith" but never "inas Ti".

The tests that matter here are the ones asserting the two paths still agree,
and that everything still works when the acceleration is unavailable.
"""

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import SEARCH_TOTAL_CAP, MetadataStore
from laurelin.ontology import OntologyService, load_ontology

ONTOLOGY = """
object_types:
  - api_name: place
    backing_dataset: places
    primary_key: name
    title_property: name
    properties:
      name: {type: string}
      realm: {type: string}
"""

PLACES = ["Minas Tirith", "Minas Morgul", "Osgiliath", "Edoras", "minas-anor"]


def places(names=None) -> pa.Table:
    names = names or PLACES
    return pa.table({
        "name": names,
        "realm": [["gondor", "mordor"][i % 2] for i in range(len(names))],
    })


def make(tmp_path, table=None, name="s"):
    ws = Workspace.init(tmp_path / name, name=name)
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("places", table if table is not None else places())
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    return OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))


@pytest.fixture()
def svc(tmp_path):
    return make(tmp_path)


# -- semantics ---------------------------------------------------------------

@pytest.mark.parametrize("needle, expected", [
    ("minas", {"Minas Tirith", "Minas Morgul", "minas-anor"}),  # case-insensitive
    ("inas Ti", {"Minas Tirith"}),      # mid-token: FTS would miss this entirely
    ("liath", {"Osgiliath"}),           # suffix
    ("MINAS-", {"minas-anor"}),         # punctuation is not a token boundary here
    ("zzz", set()),
])
def test_search_is_substring_matching_indexed_or_not(svc, needle, expected):
    """The whole point of trigram over FTS: `inas Ti` must still match."""
    scan = {o["__pk"] for o in svc.query("place", search=needle, limit=50)["objects"]}
    assert scan == expected, "scan path"

    svc.reindex("place")
    indexed = {o["__pk"] for o in svc.query("place", search=needle, limit=50)["objects"]}
    assert indexed == expected, "index path must not redefine search"


def test_the_index_and_the_scan_agree_on_totals(svc):
    before = svc.query("place", search="minas", limit=2)
    svc.reindex("place")
    after = svc.query("place", search="minas", limit=2)
    assert before["total"] == after["total"] == 3
    assert before["total_capped"] is after["total_capped"] is False


# -- graceful degradation ----------------------------------------------------

def test_search_still_works_without_the_trigram_index(tmp_path):
    """An old SQLite without FTS5, or a Postgres that won't grant CREATE
    EXTENSION, must return the same answers — just slower."""
    svc = make(tmp_path)
    svc.store.backend._has_fts = False  # simulate the unaccelerated build
    svc.reindex("place")

    got = {o["__pk"] for o in svc.query("place", search="inas Mo", limit=50)["objects"]}
    assert got == {"Minas Morgul"}


def test_dropping_the_index_clears_the_search_mirror(svc):
    svc.reindex("place")
    svc.store.drop_object_index("place")
    with svc.store._conn() as c:
        if svc.store.backend.ensure_search_index(c):
            left = c.execute(
                "SELECT count(*) AS n FROM object_search WHERE object_type = 'place'"
            ).fetchone()["n"]
            assert left == 0, "a mirror outliving its index would answer for a ghost"
    assert svc.query("place", search="minas", limit=50)["total"] == 3


# -- the saturating total ----------------------------------------------------

def test_a_broad_search_saturates_its_total(tmp_path):
    """Counting every match is the one thing no index makes cheap, so a search
    stops counting at the cap. Browsing is never capped."""
    n = SEARCH_TOTAL_CAP + 500
    svc = make(tmp_path, places([f"Minas {i}" for i in range(n)]), name="big")

    scan = svc.query("place", search="minas", limit=5)
    assert scan["total"] == SEARCH_TOTAL_CAP
    assert scan["total_capped"] is True

    svc.reindex("place")
    indexed = svc.query("place", search="minas", limit=5)
    assert indexed["total"] == scan["total"], "the cap must apply to both paths"
    assert indexed["total_capped"] is True

    # …but browsing the same type reports the truth.
    browse = svc.query("place", limit=5)
    assert browse["total"] == n
    assert browse["total_capped"] is False


def test_a_narrow_search_reports_an_exact_total(svc):
    svc.reindex("place")
    got = svc.query("place", search="osgil", limit=5)
    assert got["total"] == 1
    assert got["total_capped"] is False


# -- ranking ------------------------------------------------------------------
#
# Results used to come back in object order, so the best match could be
# anywhere on the page. Ranking reorders them; it must never change *which*
# ones match, and the index and the scan must rank identically — an indexed
# type that paged differently from an unindexed one is the bug this file's
# ordering assertions exist to catch.

RANKED = ["Riverside Depot", "Tirith Works", "Minas Tirith", "Old Mill"]
RANKED_NOTES = ["near tirith road", "", "", "tirith adjacent"]

ONTOLOGY_RANK = """
object_types:
  - api_name: place
    backing_dataset: places
    primary_key: name
    title_property: name
    properties:
      name: {type: string}
      notes: {type: string}
"""


@pytest.fixture()
def ranked(tmp_path):
    ws = Workspace.init(tmp_path / "rank", name="rank")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("places", pa.table({"name": RANKED, "notes": RANKED_NOTES}))
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY_RANK)
    return OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))


def test_title_matches_rank_above_body_matches(ranked):
    got = [o["__pk"] for o in ranked.query("place", search="tirith", limit=10)["objects"]]
    assert got == [
        "Tirith Works",     # title, position 1
        "Minas Tirith",     # title, position 7
        "Riverside Depot",  # notes only — object order from here
        "Old Mill",
    ]


def test_the_index_ranks_identically(ranked):
    before = [o["__pk"] for o in ranked.query("place", search="tirith", limit=10)["objects"]]
    ranked.reindex("place")
    after = [o["__pk"] for o in ranked.query("place", search="tirith", limit=10)["objects"]]
    assert after == before


def test_ranking_does_not_change_what_matches(ranked):
    """The whole safety property: an ordering change must not become a
    filtering change."""
    unranked = {o["__pk"] for o in ranked.query("place", search="tirith", limit=10)["objects"]}
    assert unranked == {"Tirith Works", "Minas Tirith", "Riverside Depot", "Old Mill"}
    assert ranked.query("place", search="tirith", limit=10)["total"] == 4


def test_ranking_pages_consistently(ranked):
    """Paging a ranked search must not repeat or drop a result."""
    ranked.reindex("place")
    page1 = [o["__pk"] for o in ranked.query("place", search="tirith", limit=2)["objects"]]
    page2 = [o["__pk"] for o in ranked.query("place", search="tirith", limit=2, offset=2)["objects"]]
    assert page1 + page2 == ["Tirith Works", "Minas Tirith", "Riverside Depot", "Old Mill"]


def test_browsing_is_unaffected_by_ranking(ranked):
    """No search term, no ranking — object order stands."""
    ranked.reindex("place")
    assert [o["__pk"] for o in ranked.query("place", limit=10)["objects"]] == RANKED
