"""Writeback: folding the edit overlay into a new dataset version.

The overlay bounds *read* cost only while it is small. Writeback makes it small
again by minting a version whose rows already have the edits applied — nothing
is mutated in place, and the folded edits are *marked*, not deleted, so the
version stays reproducible ("version 2 differs from 1 because of these edits by
these people").

Most of the tests here are about the three races, because the failure modes are
asymmetric and only one direction is recoverable:

* an edit arriving mid-build must not be marked folded (it was never written);
* the dataset moving under the fold must abort, not merge;
* marking before writing must be impossible.
"""

import json

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.approvals import ChangeTicket as _ChangeTicket
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import LineageEdge, Role, User
from laurelin.core.permissions import PermissionService
from laurelin.ontology import OntologyService, load_ontology

_TICKET = _ChangeTicket(kind="local", actor="test")

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
  - api_name: raze
    object_type: city
    kind: delete
    parameters: {}
  - api_name: clear_realm
    object_type: city
    kind: update
    parameters:
      realm: {type: string, required: false}
"""


def cities() -> pa.Table:
    # `note` is deliberately NOT a declared property: an object type almost
    # never declares every column, and a fold that wrote back only the declared
    # ones would delete the rest.
    return pa.table({
        "name": ["city-c", "city-a", "city-d", "city-b"],
        "realm": ["valinor", "beleriand", "valinor", "beleriand"],
        "note": ["n0", "n1", "n2", "n3"],
        "pop": pa.array([1, 2, 3, 4], type=pa.int64()),
    })


def make(tmp_path, name="wb"):
    ws = Workspace.init(tmp_path / name, name=name)
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("cities", cities())
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    return OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))


@pytest.fixture()
def svc(tmp_path):
    return make(tmp_path)


def order(svc) -> list[str]:
    return [o["__pk"] for o in svc.query("city", limit=50)["objects"]]


def objects(svc) -> dict:
    return {o["__pk"]: {k: v for k, v in o.items() if not k.startswith("__")}
            for o in svc.query("city", limit=50)["objects"]}


# -- the mechanism -----------------------------------------------------------

def test_a_fold_produces_the_same_objects_from_fewer_edits(svc):
    svc.apply_action("rename", pk="city-a", parameters={"realm": "changed"})
    svc.apply_action("found", pk=None, parameters={"name": "new", "realm": "n"})
    svc.apply_action("raze", pk="city-d", parameters={})
    before, before_order = objects(svc), order(svc)

    result = svc.writeback("city")

    assert result["folded"] == 3
    assert svc.store.list_object_edits("city") == [], "the overlay is empty now"
    assert objects(svc) == before
    assert order(svc) == before_order, "paging is byte-identical across a fold"


def test_paging_is_identical_before_and_after(svc):
    """Row order, not row count: a fold that reordered the dataset would move
    objects between pages without changing any total."""
    for i, pk in enumerate(["city-c", "city-b"]):
        svc.apply_action("rename", pk=pk, parameters={"realm": f"r{i}"})
    svc.apply_action("found", pk=None, parameters={"name": "z", "realm": "q"})
    pages_before = [order(svc)[i:i + 2] for i in range(0, 5, 2)]

    svc.writeback("city")
    pages_after = [order(svc)[i:i + 2] for i in range(0, 5, 2)]
    assert pages_after == pages_before


def test_undeclared_columns_survive_the_fold(svc):
    """`note` and `pop` are not declared properties. Folding through the object
    projection would write back a dataset with them deleted — silent data loss,
    landing on downstream transforms rather than the ontology."""
    svc.apply_action("rename", pk="city-a", parameters={"realm": "changed"})
    svc.writeback("city")

    table = svc.catalog.read("cities")
    assert set(table.column_names) == {"name", "realm", "note", "pop"}
    rows = {r["name"]: r for r in svc.catalog.table_to_rows(table)}
    assert rows["city-a"]["note"] == "n1" and rows["city-a"]["pop"] == 2
    assert rows["city-a"]["realm"] == "changed"


def test_a_created_object_lands_with_nulls_for_undeclared_columns(svc):
    svc.apply_action("found", pk=None, parameters={"name": "new", "realm": "n"})
    svc.writeback("city")
    rows = {r["name"]: r for r in svc.catalog.table_to_rows(svc.catalog.read("cities"))}
    assert rows["new"] == {"name": "new", "realm": "n", "note": None, "pop": None}


def test_a_deleted_object_is_gone_from_the_dataset(svc):
    svc.apply_action("raze", pk="city-d", parameters={})
    svc.writeback("city")
    names = [r["name"] for r in svc.catalog.table_to_rows(svc.catalog.read("cities"))]
    assert names == ["city-c", "city-a", "city-b"]


def test_folding_nothing_writes_nothing(svc):
    before = svc.catalog.store.get_dataset("cities").latest_version
    assert svc.writeback("city")["folded"] == 0
    assert svc.catalog.store.get_dataset("cities").latest_version == before


def test_the_fold_rebuilds_the_materialization(svc):
    svc.reindex("city")
    svc.apply_action("rename", pk="city-a", parameters={"realm": "changed"})
    result = svc.writeback("city")

    ot = svc.ontology.object_type("city")
    assert result["objects"] == 4
    assert svc.store_is_caught_up(ot), "a fold must not leave reads on the scan"
    assert svc._index_query(ot, None, None, 10, 0)["objects"]


# -- edit disposition --------------------------------------------------------

def test_folded_edits_are_marked_not_deleted(svc):
    """Deletion would make the new version's *cause* unrecordable, and a
    catalog whose value is inspectable history must not have a version that
    changed for no recorded reason."""
    edit = svc.apply_action("rename", pk="city-a", parameters={"realm": "changed"})
    result = svc.writeback("city")

    assert svc.store.list_object_edits("city", live_only=True) == []
    history = svc.store.list_object_edits("city", live_only=False)
    assert [e.id for e in history] == [edit.id]
    assert svc.edits("city", live_only=False)[0].actor == edit.actor

    with svc.store._conn() as c:
        row = c.execute("SELECT folded_at, folded_into_version FROM object_edits "
                        "WHERE id = ?", (edit.id,)).fetchone()
    assert row["folded_at"] is not None
    assert row["folded_into_version"] == result["version"]


def test_folded_edits_are_not_replayed_again(svc):
    """The definition of "folded": the rows already carry the edit, so applying
    it a second time would be applying it twice."""
    svc.apply_action("found", pk=None, parameters={"name": "new", "realm": "n"})
    svc.writeback("city")
    assert svc.query("city", limit=50)["total"] == 5
    svc.writeback("city")  # a second fold has nothing to do
    assert svc.query("city", limit=50)["total"] == 5


# -- race 1: an edit arrives mid-build ---------------------------------------

def test_an_edit_arriving_mid_fold_is_not_marked_and_applies_on_top(svc, monkeypatch):
    """The invariant: exactly the edits captured at read time are marked. An
    edit we did not read is an edit we do not mark."""
    first = svc.apply_action("rename", pk="city-a", parameters={"realm": "first"})
    late = {}

    original = svc.catalog.write

    def write_then_race(*args, **kwargs):
        # Commit an edit in the window between capturing the id list and
        # publishing the version.
        result = original(*args, **kwargs)
        late["edit"] = svc.apply_action("rename", pk="city-b",
                                        parameters={"realm": "late"})
        return result

    monkeypatch.setattr(svc.catalog, "write", write_then_race)
    result = svc.writeback("city")
    monkeypatch.undo()

    assert result["folded"] == 1, "only the captured edit"
    live = svc.store.list_object_edits("city", live_only=True)
    assert [e.id for e in live] == [late["edit"].id], "the late edit is still live"
    # Applied once, on top of the new version: not lost, not doubled.
    assert objects(svc)["city-a"]["realm"] == "first"
    assert objects(svc)["city-b"]["realm"] == "late"
    assert first.id not in {e.id for e in live}


def test_a_crash_between_writing_and_marking_replays_idempotently(svc, monkeypatch):
    """T2 before T3, never the reverse. Crash after the write and the folded
    edits replay on top of themselves — every edit kind is an absolute
    assignment, so the result is correct, merely not yet cheaper."""
    svc.apply_action("rename", pk="city-a", parameters={"realm": "changed"})
    svc.apply_action("found", pk=None, parameters={"name": "new", "realm": "n"})
    expected = objects(svc)

    monkeypatch.setattr(svc.store, "mark_edits_folded",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        svc.writeback("city")
    monkeypatch.undo()

    assert len(svc.store.list_object_edits("city")) == 2, "still live, not lost"
    assert objects(svc) == expected, "replay on top of the folded rows is a no-op"
    # And a retry completes cleanly.
    svc.writeback("city")
    assert objects(svc) == expected
    assert svc.store.list_object_edits("city") == []


# -- race 2: the dataset moves under the fold --------------------------------

def test_a_version_bump_mid_fold_aborts(svc, monkeypatch):
    """_commit_version arbitrates version *numbers*, not content: a writer that
    loses simply takes the next integer. Publishing V+2 built from V would drop
    V+1's rows silently, so abort — a fold is cheap to retry and there is no
    sound way to rebase one."""
    edit = svc.apply_action("rename", pk="city-a", parameters={"realm": "changed"})
    # Patched on the class: the fold runs on a system view, which is a
    # different instance of the same service.
    original_scan = OntologyService._object_scan

    def scan_then_bump(self, *args, **kwargs):
        ctx = original_scan(self, *args, **kwargs)
        svc.catalog.append("cities", pa.table({
            "name": ["interloper"], "realm": ["x"], "note": ["n"],
            "pop": pa.array([9], type=pa.int64()),
        }))
        return ctx

    monkeypatch.setattr(OntologyService, "_object_scan", scan_then_bump)
    with pytest.raises(ValueError, match="moved from version"):
        svc.writeback("city")
    monkeypatch.undo()

    assert [e.id for e in svc.store.list_object_edits("city")] == [edit.id]
    assert "interloper" in objects(svc), "the interloping rows are still there"
    assert objects(svc)["city-a"]["realm"] == "changed"


def test_a_second_fold_landing_mid_fold_cannot_lose_an_edit(svc, monkeypatch):
    """The version check was a check-then-act, and the gap was reachable by
    anything: two operators, a double click, a client retry, two replicas.

    Demonstrated loss: fold A folds edit 3 into version 2 and marks it folded,
    then fold B — which passed its check while the dataset was still at
    version 1 — publishes version 3 built from version 1, which does not
    contain edit 3. The live log is then empty, so no replay restores it and no
    rebuild finds it. A hand edit destroyed with no error anywhere.
    """
    svc.apply_action("rename", pk="city-a", parameters={"realm": "edit-1"})
    original = DatasetCatalog.write
    fired = []

    def patched(self, name, table, *args, **kwargs):
        if kwargs.get("source") == "writeback" and not fired:
            fired.append(True)
            # Both land in the gap: a new edit, and a complete competing fold
            # that folds and marks it.
            svc.apply_action("rename", pk="city-d", parameters={"realm": "edit-3"})
            OntologyService(svc.workspace, svc.catalog, svc.store,
                            svc.ontology).writeback("city")
        return original(self, name, table, *args, **kwargs)

    monkeypatch.setattr(DatasetCatalog, "write", patched)
    with pytest.raises(ValueError):
        svc.writeback("city")
    monkeypatch.undo()

    assert fired, "the race was never actually run"
    assert objects(svc)["city-d"]["realm"] == "edit-3", "a folded edit was lost"
    assert objects(svc)["city-a"]["realm"] == "edit-1"


def test_a_fold_cannot_discard_a_version_published_while_it_ran(svc, monkeypatch):
    """The same gap, the other victim — and the exact scenario the check was
    written for. A nightly build commits fresh upstream rows including a brand
    new object; the in-flight fold then publishes a version rebuilt from the
    old base, and every one of the build's rows is gone from the latest
    version. No error, and the fold reports success."""
    svc.apply_action("rename", pk="city-a", parameters={"realm": "hand-edit"})
    original = DatasetCatalog.write
    fired = []

    def patched(self, name, table, *args, **kwargs):
        if kwargs.get("source") == "writeback" and not fired:
            fired.append(True)
            built = pa.concat_tables([cities(), pa.table({
                "name": ["city-NEW"], "realm": ["fresh"], "note": ["n9"],
                "pop": pa.array([9], type=pa.int64()),
            })])
            original(self, "cities", built, source="upload")
        return original(self, name, table, *args, **kwargs)

    monkeypatch.setattr(DatasetCatalog, "write", patched)
    with pytest.raises(ValueError):
        svc.writeback("city")
    monkeypatch.undo()

    assert fired
    assert "city-NEW" in objects(svc), "the concurrent build's rows were discarded"
    assert [e.pk_value for e in svc.store.list_object_edits("city")] == ["city-a"], \
        "nothing was marked folded, so the edit still replays"
    assert objects(svc)["city-a"]["realm"] == "hand-edit"


# -- what a fold must not silently change ------------------------------------

def test_a_fold_preserves_an_update_that_clears_a_property(svc):
    """The overlay merged with ``COALESCE(u.col, b.col)``, which cannot tell
    "assigned NULL" from "not assigned" — so an update clearing a property had
    no expression at all. Folding one silently restored the old value and then
    marked the edit folded, which makes it unrecoverable."""
    svc.apply_action("clear_realm", pk="city-a", parameters={"realm": None})
    before = objects(svc)
    assert before["city-a"]["realm"] is None

    svc.writeback("city")

    assert objects(svc) == before
    rows = {r["name"]: r for r in svc.catalog.table_to_rows(svc.catalog.read("cities"))}
    assert rows["city-a"]["realm"] is None


def test_a_null_update_still_works_after_the_create_it_targets_is_folded(svc):
    """Worse than losing a folded edit: the fold broke edits it never touched.
    A null update merged into an *overlay create* works (dict update), and the
    moment that create became a base row the merge switched to COALESCE and the
    same edit stopped working."""
    svc.apply_action("found", pk=None, parameters={"name": "zed", "realm": "q"})
    svc.writeback("city")
    svc.apply_action("clear_realm", pk="zed", parameters={"realm": None})
    assert objects(svc)["zed"]["realm"] is None


def test_a_fold_refuses_a_dataset_whose_primary_key_is_not_unique(svc):
    """The object view de-duplicates last-wins in a window function, so it
    never showed the extra rows. Folding materialized that dedup into the
    dataset: one unrelated hand edit turned five rows into three, and the two
    deleted rows were named nowhere."""
    svc.catalog.write("cities", pa.table({
        "name": ["city-a", "city-a", "city-b", "city-b", "city-c"],
        "realm": ["r1", "r2", "r3", "r4", "r5"],
        "note": ["n1", "n2", "n3", "n4", "n5"],
        "pop": pa.array([1, 2, 3, 4, 5], type=pa.int64()),
    }))
    svc.apply_action("rename", pk="city-c", parameters={"realm": "touched"})

    with pytest.raises(ValueError, match="sharing a"):
        svc.writeback("city")

    assert svc.catalog.read("cities").num_rows == 5, "no row was dropped"
    assert len(svc.store.list_object_edits("city")) == 1, "and nothing was marked"


def test_a_create_over_an_existing_key_keeps_its_undeclared_columns(svc):
    """The same loss ``all_columns`` exists to prevent, through a different
    door: ``_overlay`` filters a create payload to the declared properties and
    the fold emitted the create row wholesale, so every undeclared column of
    the row it replaced was written as NULL. The object view never showed
    `note`, so nothing in the ontology surfaced it."""
    svc.apply_action("found", pk=None, parameters={"name": "city-a", "realm": "X"})
    before = objects(svc)

    svc.writeback("city")

    rows = {r["name"]: r for r in svc.catalog.table_to_rows(svc.catalog.read("cities"))}
    assert rows["city-a"]["note"] == "n1", "an undeclared column was nulled"
    assert rows["city-a"]["pop"] == 2
    assert rows["city-a"]["realm"] == "X", "the declared property was assigned"
    assert objects(svc) == before


def test_a_recreated_key_inherits_nothing_from_the_row_it_replaced(svc):
    """The counterpart: after a delete, a create with the same key is a *new*
    object, so it must not pick the deleted row's undeclared columns back up."""
    svc.apply_action("raze", pk="city-a", parameters={})
    svc.apply_action("found", pk=None, parameters={"name": "city-a", "realm": "X"})
    svc.writeback("city")
    rows = {r["name"]: r for r in svc.catalog.table_to_rows(svc.catalog.read("cities"))}
    assert rows["city-a"] == {"name": "city-a", "realm": "X", "note": None, "pop": None}


def test_the_transform_guard_survives_its_own_override(svc):
    """``backing_is_transform_produced`` fell back to "is the LATEST version's
    source 'transform'?" — and writeback overwrites that source with
    'writeback'. So the guard fired exactly once per dataset: after a single
    deliberate override it was gone, and every later fold proceeded with no
    warning that tonight's build would revert the edits."""
    svc.catalog.write("cities", cities(), source="transform")
    ot = svc.ontology.object_type("city")
    assert svc.backing_is_transform_produced(ot) == "a transform"

    svc.apply_action("rename", pk="city-a", parameters={"realm": "hand-edit-1"})
    with pytest.raises(ValueError, match="transform"):
        svc.writeback("city")
    svc.writeback("city", allow_transform_backed=True)

    assert svc.backing_is_transform_produced(ot) == "a transform", "the guard eroded"
    svc.apply_action("rename", pk="city-b", parameters={"realm": "hand-edit-2"})
    with pytest.raises(ValueError, match="transform"):
        svc.writeback("city")


# -- policy ------------------------------------------------------------------

def test_a_fold_by_a_policied_user_does_not_rewrite_the_dataset_as_they_see_it(tmp_path):
    """The single most dangerous thing in the feature: a fold run through a
    policy would delete every row that user's RLS hides, for everybody."""
    svc = make(tmp_path, name="pol")
    svc.store.set_dataset_policy("cities", {
        "dataset": "cities",
        "row_policy": {"column": "realm", "rules": [
            {"subject_kind": "user", "subject": "elf", "values": ["valinor"]},
        ]},
        "column_masks": [],
    }, ticket=_TICKET)
    perms = PermissionService(svc.store)
    elf = User(id="e", username="elf", role=Role.viewer)
    elf_svc = OntologyService(
        svc.workspace, svc.catalog, svc.store, svc.ontology,
        policy=perms.query_policy_fn(elf),
        policy_for=perms.per_dataset_policy_fn(elf),
        plan_for=perms.arrow_policy_fn(elf),
        decide_for=lambda ds, cols, _u=elf: perms.decide(ds, cols, _u),
    )
    elf_svc.apply_action("rename", pk="city-c", parameters={"realm": "valinor"})
    assert elf_svc.query("city", limit=50)["total"] == 2, "the policy does apply"

    elf_svc.writeback("city")

    rows = svc.catalog.table_to_rows(svc.catalog.read("cities"))
    assert len(rows) == 4, "no row outside the folder's predicate was deleted"
    assert {r["realm"] for r in rows} == {"valinor", "beleriand"}


# -- unwritable backings -----------------------------------------------------

def test_a_transform_produced_backing_is_refused_by_name(svc):
    """The hazard that actually matters. Fold into version 7, mark the edits
    folded, and tonight's build writes version 8 from upstream with no trace of
    them — hand edits reverting silently, hours later, with no error anywhere."""
    svc.catalog.store.replace_lineage_for_transform("nightly_cities", [
        LineageEdge(upstream_dataset="upstream", downstream_dataset="cities",
                    transform_name="nightly_cities"),
    ])
    svc.apply_action("rename", pk="city-a", parameters={"realm": "changed"})

    with pytest.raises(ValueError, match="nightly_cities"):
        svc.writeback("city")
    assert len(svc.store.list_object_edits("city")) == 1, "nothing was folded"


def test_a_transform_produced_backing_can_be_overridden_deliberately(svc):
    svc.catalog.store.replace_lineage_for_transform("nightly_cities", [
        LineageEdge(upstream_dataset="upstream", downstream_dataset="cities",
                    transform_name="nightly_cities"),
    ])
    svc.apply_action("rename", pk="city-a", parameters={"realm": "changed"})
    assert svc.writeback("city", allow_transform_backed=True)["folded"] == 1


def test_a_scanned_at_source_backing_is_refused(svc, tmp_path):
    """Iceberg, ClickHouse, StarRocks and federated alike: there is no version
    for Laurelin to write, so folding cannot mean anything."""
    import pyarrow.parquet as pq

    remote = tmp_path / "remote.parquet"
    pq.write_table(cities(), remote)
    svc.catalog.store.set_dataset_source(
        "cities", "federated", {"type": "parquet", "path": str(remote)}
    )
    with pytest.raises(ValueError, match="scanned at the source"):
        svc.writeback("city")


# -- the endpoint ------------------------------------------------------------

def test_writeback_needs_dataset_edit_not_just_object_type_edit(tmp_path):
    """Recording an edit and rewriting the dataset are different privileges,
    and dataset ACLs exist to express the second one.

    ``scribe`` is a global editor with *view* on the dataset, which is enough
    for object-type EDITOR — so a 403 here can only be the dataset-edit check.
    """
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
    assert client.put("/api/v1/datasets/cities/permissions", json={"grants": [
        {"subject_kind": "user", "subject": "root", "can_view": True, "can_edit": True},
        {"subject_kind": "user", "subject": "scribe", "can_view": True, "can_edit": False},
    ]}).status_code == 200

    scribe = TestClient(create_app(ws))
    assert scribe.post("/api/v1/auth/login", json={
        "username": "scribe", "password": "password123"}).status_code == 200
    assert scribe.get("/api/v1/ontology/object-types/city").status_code == 200, (
        "the object type itself is visible and editable to this user"
    )
    denied = scribe.post("/api/v1/ontology/object-types/city/writeback")
    assert denied.status_code == 403
    assert "cities" in denied.json()["detail"], "refused on the dataset, not the type"

    allowed = client.post("/api/v1/ontology/object-types/city/writeback")
    assert allowed.status_code == 200
    assert allowed.json()["folded"] == 0


def test_the_fold_is_audited(svc):
    svc.apply_action("rename", pk="city-a", parameters={"realm": "changed"})
    result = svc.writeback("city", actor="scribe")
    entry = next(e for e in svc.store.list_audit(limit=50)
                 if e.action == "object_edits_folded")
    assert entry.actor == "scribe"
    assert entry.details["version"] == result["version"]
    assert entry.details["edits"] == 1


def test_a_folded_version_records_its_source(svc):
    svc.apply_action("rename", pk="city-a", parameters={"realm": "changed"})
    result = svc.writeback("city")
    version = svc.catalog.store.get_version("cities", result["version"])
    assert version.source == "writeback"


def test_the_edit_payload_survives_as_history(svc):
    """`folded_into_version` answers "what changed in version N and who did it",
    which is the whole reason marking beats deleting."""
    svc.apply_action("rename", pk="city-a", parameters={"realm": "changed"},
                     actor="feanor")
    result = svc.writeback("city")
    folded = svc.edits("city", live_only=False)[0]
    assert folded.actor == "feanor"
    assert json.loads(json.dumps(folded.payload)) == {"realm": "changed"}
    assert result["version"] == 2
