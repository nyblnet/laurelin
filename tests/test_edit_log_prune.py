"""Pruning the ontology edit log.

The edit log is TRUTH: every read path replays it, and it is the only record of
who changed what. Nothing trimmed it, so a workspace using the ontology as an
application database grew this table forever and every rebuild replayed more.

Pruning it is therefore not a retention policy applied to a log, it is a proof
obligation discharged per edit. These tests are that proof, written as the five
conditions in ``OntologyService.prune_plan``:

1. folded only — a live edit is not history, it is the data;
2. the fold's version is still in the backing dataset's history and is a
   ``writeback`` version, which is what ties the edit to *this* dataset;
3. nothing written since the fold could have superseded it;
4. never the row holding ``MAX(edit_seq)``, which the sequence allocator and
   every materialization watermark depend on;
5. outside the operator's retention window.

The failure being defended against is silent: a pruned edit does not error, it
simply stops existing, and if it was still load-bearing the data it described
is gone with it.
"""

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.ontology import OntologyService, load_ontology

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
  - api_name: rename
    object_type: city
    kind: update
    parameters:
      realm: {type: string, required: true}
  - api_name: found
    object_type: city
    kind: create
    parameters:
      name: {type: string, required: true}
      realm: {type: string, required: true}
"""


def cities() -> pa.Table:
    return pa.table({
        "name": ["city-c", "city-a", "city-d", "city-b"],
        "realm": ["valinor", "beleriand", "valinor", "beleriand"],
        "note": ["n0", "n1", "n2", "n3"],
    })


def make(tmp_path, name="prune"):
    ws = Workspace.init(tmp_path / name, name=name)
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("cities", cities())
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    return OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))


@pytest.fixture()
def svc(tmp_path):
    return make(tmp_path)


def objects(svc) -> list[dict]:
    return svc.query("city", limit=50)["objects"]


def edit_ids(svc) -> list[str]:
    return [e.id for e in svc.store.list_object_edits("city", live_only=False)]


def three_edits(svc) -> None:
    svc.apply_action("rename", pk="city-a", parameters={"realm": "one"})
    svc.apply_action("rename", pk="city-b", parameters={"realm": "two"})
    svc.apply_action("found", pk=None, parameters={"name": "new", "realm": "three"})


def reasons(plan) -> str:
    return " | ".join(r["reason"] for r in plan["retained"])


# -- 1. a live edit is data, not history -------------------------------------

def test_an_unfolded_edit_is_never_prunable(svc):
    """The overlay *is* the current value of those objects. Deleting one is
    not trimming a log, it is deleting the edit."""
    three_edits(svc)
    plan = svc.prune_plan("city", keep=0)
    assert plan["live"] == 3 and plan["folded"] == 0
    assert plan["prunable"] == 0
    assert svc.prune_object_edits("city", keep=0)["pruned"] == 0
    assert len(edit_ids(svc)) == 3


def test_the_store_refuses_to_delete_a_live_edit_even_when_told_to(svc):
    """The condition is enforced twice on purpose. This is the second one: the
    layer that owns the table refuses, whatever the caller decided."""
    three_edits(svc)
    ids = edit_ids(svc)
    assert svc.store.delete_object_edits("city", ids) == 0
    assert edit_ids(svc) == ids


# -- 2/3. the fold has to still be in the dataset -----------------------------

def test_folded_edits_go_and_the_objects_are_unchanged(svc):
    """The definition of safe: the rows already carry the edits, so the log
    entries are history — and history is what pruning costs."""
    three_edits(svc)
    svc.writeback("city")
    before = objects(svc)

    result = svc.prune_object_edits("city", keep=0, actor="op")

    assert result["pruned"] == 2, "all but the sequence anchor"
    assert objects(svc) == before
    assert [o["__pk"] for o in objects(svc)] == [o["__pk"] for o in before], (
        "paging order survives a prune"
    )


def test_an_edit_folded_into_a_version_a_build_overwrote_is_kept(svc):
    """The case the architecture doc calls "automatic unfolding, NOT
    IMPLEMENTED". A transform build after the fold may have written the rows
    from upstream again, without the hand edits — and then these log rows are
    the only surviving record of them. Pruning here is the silent data loss
    this whole design exists to prevent."""
    three_edits(svc)
    svc.writeback("city")
    svc.catalog.write("cities", cities(), source="transform")

    plan = svc.prune_plan("city", keep=0)
    assert plan["prunable"] == 0
    assert "may have overwritten" in reasons(plan)
    assert svc.prune_object_edits("city", keep=0)["pruned"] == 0
    assert len(edit_ids(svc)) == 3


def test_a_compaction_after_the_fold_does_not_block_pruning(svc):
    """Compaction rewrites the same rows into a tidier layout, so it carries
    the fold forward rather than superseding it. Treating every later version
    as a threat would mean a compacted workspace could never prune."""
    three_edits(svc)
    svc.writeback("city")
    svc.catalog.compact("cities")

    assert svc.prune_object_edits("city", keep=0)["pruned"] == 2


def test_an_edit_whose_fold_version_is_gone_is_kept(svc):
    """`folded_into_version` naming a version that is not in the dataset's
    history means the fold cannot be located, and an edit whose fold cannot be
    located has not been shown to be redundant."""
    three_edits(svc)
    version = svc.writeback("city")["version"]
    with svc.store._conn() as c:
        c.execute("DELETE FROM dataset_versions WHERE dataset = ? AND version = ?",
                  ("cities", version))

    plan = svc.prune_plan("city", keep=0)
    assert plan["prunable"] == 0
    assert "no longer in the dataset's history" in reasons(plan)


def test_rebinding_the_type_to_another_dataset_keeps_its_history(tmp_path):
    """Version numbers are per dataset. Rebind an object type and the fold's
    version number means something else in the new dataset — so the check is
    "version N of *this* dataset is a writeback version", not "N <= latest"."""
    svc = make(tmp_path)
    three_edits(svc)
    svc.writeback("city")

    svc.catalog.write("towns", cities())
    svc.catalog.write("towns", cities())  # v2 exists, but nothing folded into it
    ws = svc.workspace
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY.replace("cities", "towns"))
    rebound = OntologyService(ws, svc.catalog, svc.store, load_ontology(ws.ontology_dir))

    plan = rebound.prune_plan("city", keep=0)
    assert plan["prunable"] == 0
    assert "is not a writeback version" in reasons(plan)


# -- 4. the sequence anchor ---------------------------------------------------

def test_the_highest_edit_is_kept_so_the_sequence_cannot_repeat_itself(svc):
    """`MAX(edit_seq) + 1` allocates positions and `max_edit_seq` is what a
    materialization's watermark is compared against. Delete the top row and the
    next edit re-uses a number the store already claims to have applied:
    `catch_up` skips it forever while the freshness check says "fresh"."""
    three_edits(svc)
    svc.writeback("city")
    high = svc.store.max_edit_seq("city")

    svc.prune_object_edits("city", keep=0)

    assert svc.store.max_edit_seq("city") == high, "the watermark's meaning is preserved"
    plan = svc.prune_plan("city", keep=0)
    assert "anchors the edit sequence" in reasons(plan)


def test_the_store_refuses_to_delete_the_sequence_anchor(svc):
    three_edits(svc)
    svc.writeback("city")
    top = max(svc.store.list_folded_edits("city"), key=lambda e: e["edit_seq"])
    assert svc.store.delete_object_edits("city", [top["id"]]) == 0


def test_a_materialization_stays_correct_across_a_prune(svc):
    """The whole point of keeping the anchor, end to end: an indexed type is
    still answered from the index after a prune, and the next edit lands."""
    svc.reindex("city")
    three_edits(svc)
    svc.writeback("city")
    svc.prune_object_edits("city", keep=0)

    ot = svc.ontology.object_type("city")
    assert svc.store_is_caught_up(ot)
    svc.apply_action("rename", pk="city-a", parameters={"realm": "after-prune"})
    assert svc.store_is_caught_up(ot), "the new edit was applied, not skipped"
    indexed = svc._index_query(ot, None, {"name": "city-a"}, 10, 0)
    assert indexed is not None and indexed["objects"][0]["realm"] == "after-prune"
    assert [o["realm"] for o in objects(svc) if o["__pk"] == "city-a"] == ["after-prune"]


def test_a_live_edit_above_the_folded_ones_frees_them_all(svc):
    """The anchor is whichever row holds the highest position — when a live
    edit does, every folded edit below it can go."""
    three_edits(svc)
    svc.writeback("city")
    svc.apply_action("rename", pk="city-b", parameters={"realm": "later"})

    assert svc.prune_object_edits("city", keep=0)["pruned"] == 3
    assert len(edit_ids(svc)) == 1, "only the live edit is left"
    assert [o["realm"] for o in objects(svc) if o["__pk"] == "city-b"] == ["later"]


# -- 5. the retention window --------------------------------------------------

def test_the_retention_window_keeps_the_newest_folded_edits(svc):
    three_edits(svc)
    svc.apply_action("rename", pk="city-c", parameters={"realm": "four"})
    svc.writeback("city")

    plan = svc.prune_plan("city", keep=2)
    assert plan["folded"] == 4
    assert plan["prunable"] == 2
    assert "inside the 2-edit retention window" in reasons(plan)
    assert svc.prune_object_edits("city", keep=2)["pruned"] == 2
    assert len(edit_ids(svc)) == 2


def test_pruning_is_idempotent(svc):
    three_edits(svc)
    svc.writeback("city")
    assert svc.prune_object_edits("city", keep=0)["pruned"] == 2
    assert svc.prune_object_edits("city", keep=0)["pruned"] == 0


def test_a_negative_retention_is_refused(svc):
    with pytest.raises(ValueError, match="keep must be"):
        svc.prune_plan("city", keep=-1)


# -- accounting ---------------------------------------------------------------

def test_the_plan_reports_what_pruning_would_reclaim_without_doing_it(svc):
    """A dry run is the only honest way to offer this in a UI: the operator
    sees the number before deciding, and the same code path produces it."""
    three_edits(svc)
    svc.writeback("city")
    before = edit_ids(svc)

    plan = svc.prune_object_edits("city", keep=0, dry_run=True)

    assert plan["dry_run"] is True and plan["pruned"] == 0
    assert plan["prunable"] == 2 and plan["prunable_bytes"] > 0
    assert plan["prunable_bytes"] < plan["payload_bytes"]
    assert edit_ids(svc) == before, "a dry run deletes nothing"


def test_the_log_reports_its_size_per_object_type(svc):
    three_edits(svc)
    stats = svc.store.object_edit_stats("city")[0]
    assert stats["edits"] == 3 and stats["live"] == 3 and stats["folded"] == 0
    assert stats["payload_bytes"] > 0
    assert stats["oldest"] <= stats["newest"]
    assert stats["max_edit_seq"] == 3


def test_pruning_is_audited(svc):
    """Deleting history is exactly the operation whose own record must survive
    it — including how much went, so the trail explains a shrunken log."""
    three_edits(svc)
    svc.writeback("city")
    svc.prune_object_edits("city", keep=0, actor="steward")

    entry = next(e for e in svc.store.list_audit(limit=50)
                 if e.action == "object_edits_pruned")
    assert entry.actor == "steward"
    assert entry.details["edits"] == 2
    assert entry.details["object_type"] == "city"
    assert entry.details["payload_bytes"] > 0


# -- the automatic hook -------------------------------------------------------

def test_a_fold_prunes_nothing_unless_asked(svc, monkeypatch):
    monkeypatch.delenv(svc.PRUNE_ENV, raising=False)
    three_edits(svc)
    assert svc.writeback("city")["pruned"] == 0
    assert len(edit_ids(svc)) == 3


def test_a_fold_prunes_when_the_workspace_asks_for_it(svc, monkeypatch):
    """A fold is the only moment that creates prunable history, so it is the
    natural hook — and off by default, because deleting history by default is
    not a default anyone chose."""
    monkeypatch.setenv(svc.PRUNE_ENV, "1")
    three_edits(svc)
    result = svc.writeback("city")
    assert result["folded"] == 3 and result["pruned"] == 2
    assert len(edit_ids(svc)) == 1


def test_a_failing_prune_does_not_fail_the_fold(svc, monkeypatch):
    """The fold is committed and the dataset already holds the edits. Raising
    here would tell the caller their write failed after it succeeded."""
    monkeypatch.setenv(svc.PRUNE_ENV, "1")

    def boom(*a, **k):
        raise RuntimeError("metadata store unavailable")

    monkeypatch.setattr(svc.store, "delete_object_edits", boom)
    three_edits(svc)
    result = svc.writeback("city")
    assert result["folded"] == 3 and result["pruned"] == 0
    assert len(edit_ids(svc)) == 3


# -- the endpoints ------------------------------------------------------------

def test_the_edit_log_is_visible_and_prunable_over_http(tmp_path):
    """Reachable in the UI means reachable at all: an operator watching this
    table grow needs the number, and pruning needs a rank above writeback —
    folding rewrites data everyone can still see, pruning deletes the record
    of who wrote it."""
    from fastapi.testclient import TestClient

    from laurelin.api.app import create_app

    ws = Workspace.init(tmp_path / "api", name="api")
    store = MetadataStore(ws.metadata_path)
    DatasetCatalog(ws, store).write("cities", cities())
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    client = TestClient(create_app(ws))

    creds = {"username": "root", "password": "trustno1!"}
    assert client.post("/api/v1/auth/setup", json=creds).status_code == 200
    assert client.post("/api/v1/auth/login", json=creds).status_code == 200
    assert client.post("/api/v1/users", json={
        "username": "scribe", "password": "password123", "role": "editor",
    }).status_code == 200

    apply = client.post("/api/v1/ontology/actions/rename/apply",
                        json={"pk": "city-a", "parameters": {"realm": "changed"}})
    assert apply.status_code == 200
    assert client.post("/api/v1/ontology/object-types/city/writeback").status_code == 200

    report = client.get("/api/v1/ontology/object-types/city/edit-log")
    assert report.status_code == 200
    body = report.json()
    assert body["folded"] == 1 and body["withheld"] is False
    assert body["prunable"] == 0, "the only edit is the sequence anchor"
    assert "prunable_ids" not in body

    editor = TestClient(create_app(ws))
    assert editor.post("/api/v1/auth/login", json={
        "username": "scribe", "password": "password123"}).status_code == 200
    assert editor.get("/api/v1/ontology/object-types/city/edit-log").status_code == 200
    denied = editor.post(
        "/api/v1/ontology/object-types/city/edit-log/prune?keep=0")
    assert denied.status_code == 403, "an editor may fold, but not delete the record"

    dry = client.post(
        "/api/v1/ontology/object-types/city/edit-log/prune?keep=0&dry_run=true")
    assert dry.status_code == 200 and dry.json()["pruned"] == 0


# -- a rebuild that supersedes a fold ------------------------------------------
#
# The other half of condition 3. Pruning already refuses those rows; what
# nothing did was *say* they were superseded — the object silently returned its
# pre-edit value and no surface mentioned it. Reporting is the whole fix:
# replaying the edit is deliberately not implemented, because an edit is an
# absolute assignment and a replay would put a stale hand value back over
# corrected upstream data.

def test_a_rebuild_that_supersedes_a_fold_is_reported_not_replayed(svc):
    three_edits(svc)
    version = svc.writeback("city")["version"]
    before = {o["__pk"]: o["realm"] for o in objects(svc)}
    assert before["city-a"] == "one", "the fold is in the dataset"

    # A transform build writes the rows again from upstream, without the
    # overlay: this is the supersession.
    svc.catalog.write("cities", cities(), source="transform")
    after = {o["__pk"]: o["realm"] for o in objects(svc)}
    assert after["city-a"] == "beleriand", (
        "the hand edit's EFFECT is gone — this is the state being reported"
    )

    report = svc.superseded_folds(svc.ontology.object_type("city"))
    assert report["edits"] == 3
    assert report["version"] == version + 1
    assert report["source"] == "transform"
    assert svc.prune_plan("city", keep=0)["superseded_folds"] == report
    # And on the object type's own status block, which is where someone looking
    # at the type — rather than at the log — would have to be told.
    svc.reindex("city")
    assert svc.index_state(svc.ontology.object_type("city"))["superseded_folds"] == report

    # Reported, NOT replayed: no unfold happened, the objects still read their
    # rebuilt values, and the log still holds every edit as the record.
    assert {o["__pk"]: o["realm"] for o in objects(svc)} == after
    assert len(edit_ids(svc)) == 3
    assert svc.prune_object_edits("city", keep=0)["pruned"] == 0


def test_a_fold_nothing_overwrote_is_not_reported_as_superseded(svc):
    """The report has to be quiet in the ordinary case, or it is noise: a
    writeback carries the fold forward, so there is nothing to say."""
    three_edits(svc)
    svc.writeback("city")
    empty = {"edits": 0, "version": None, "source": None}
    assert svc.superseded_folds(svc.ontology.object_type("city")) == empty
    assert svc.prune_plan("city", keep=0)["superseded_folds"] == empty


def test_edit_log_auto_prune_is_off_unless_an_operator_asks(svc, monkeypatch):
    """Deleting history is something an operator asks for, so every way of
    *not* asking has to mean off — unset, "0", and the empty string a shell
    supplies for an exported-but-blank variable all read the same."""
    three_edits(svc)
    for value in (None, "0", ""):
        if value is None:
            monkeypatch.delenv(svc.PRUNE_ENV, raising=False)
        else:
            monkeypatch.setenv(svc.PRUNE_ENV, value)
        assert svc._prune_after_fold("city", actor="test") == 0
        assert len(edit_ids(svc)) == 3
