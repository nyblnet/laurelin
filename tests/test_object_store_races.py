"""Concurrency on the write path: what happens when two people edit at once.

Every test here corresponds to a defect that was demonstrated with two ordinary
threads calling ``apply_action`` — no instrumentation, no private entry points —
and every one of them was *silent*. That is the theme worth stating up front:
the watermark advanced normally in all of them, so ``store_is_caught_up``
returned True, reads never fell through to the scan, ``catch_up`` had nothing to
replay, and the index endpoint reported ``fresh: true, lag: 0``. Rule 2 ("a
store that is behind never answers") was intact and simply did not apply,
because the store was not behind — it was wrong while level.

The single root cause: building an edit's rows is a read-modify-write (an update
merges onto the current row, a create inherits its ordinal), and the read
happened on a different connection from the write. It now happens inside the
transaction that writes, under a lock on the type's state row.

**How the races are forced.** A hook inside the write path signals that it has
been reached and then waits for its counterpart. With the fix, the counterpart
*cannot* arrive — it is blocked on the state-row lock — so the wait times out
and the writers serialize. Without the fix, it arrives immediately and both
writers proceed from the same stale pre-image. So the wait timeout is only ever
paid on the passing path, and the interleaving being unreachable is the thing
being asserted.
"""

import json
import threading

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.ontology import OntologyService, load_ontology
from laurelin.ontology.store import ORD_CREATED_BASE

# How long a hooked thread waits for its counterpart before giving up. Only
# spent when the fix is working, and only once per test.
BLOCKED = 1.5

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
actions:
  - api_name: rename_realm
    object_type: city
    kind: update
    parameters:
      realm: {type: string, required: true}
  - api_name: set_pop
    object_type: city
    kind: update
    parameters:
      pop: {type: integer, required: true}
  - api_name: found_city
    object_type: city
    kind: create
    parameters:
      name: {type: string, required: true}
      realm: {type: string, required: true}
      pop: {type: integer}
  - api_name: raze
    object_type: city
    kind: delete
    parameters: {}
"""


def cities(n: int = 6) -> pa.Table:
    return pa.table({
        "name": [f"city-{i}" for i in range(n)],
        "realm": [["valinor", "beleriand"][i % 2] for i in range(n)],
        "pop": pa.array([100 + i for i in range(n)], type=pa.int64()),
    })


@pytest.fixture()
def svc(tmp_path):
    ws = Workspace.init(tmp_path / "race", name="race")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("cities", cities())
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    s = OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))
    s.reindex("city")
    return s


def replay(svc) -> dict:
    """The oracle: objects as an in-memory replay of the log computes them."""
    ot = svc.ontology.object_type("city")
    return {o["__pk"]: {k: v for k, v in o.items() if not k.startswith("__")}
            for o in svc._materialize(ot)}


def served(svc) -> dict:
    return {o["__pk"]: {k: v for k, v in o.items() if not k.startswith("__")}
            for o in svc.query("city", limit=500)["objects"]}


def run_all(*fns) -> list[BaseException]:
    """Run every callable on its own thread, join, and return what blew up."""
    errors: list[BaseException] = []

    def guard(fn):
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=guard, args=(fn,)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return errors


@pytest.fixture()
def hook(monkeypatch):
    """Wrap ``_rows_for_edit`` so a test can pause one writer inside it.

    Signature-agnostic on purpose: this file has to be runnable against the
    version of the code that has the bug, and that version's ``_rows_for_edit``
    takes different arguments.
    """
    original = OntologyService._rows_for_edit
    installed = {}

    def patched(self, ot, edit, *args, **kwargs):
        fn = installed.get("fn")
        if fn is not None:
            fn(edit)
        return original(self, ot, edit, *args, **kwargs)

    monkeypatch.setattr(OntologyService, "_rows_for_edit", patched)
    return installed


# -- lost update -------------------------------------------------------------

def test_two_concurrent_updates_to_one_object_keep_both(svc, hook):
    """Two people editing different properties of one object, at once.

    The demonstrated failure: both writers merged their payload onto the same
    pre-image, and whichever committed second wrote its whole row — so the
    first writer's committed, durable edit was invisible to every reader
    forever. Only a hand rebuild recovered it, and the log still said it
    happened.
    """
    reached_b = threading.Event()

    def gate(edit):
        if edit.payload.get("realm") == "ALPHA":
            # Writer A waits for B to reach this same point. B can only get
            # here while A is mid-write if the pre-image is read outside the
            # transaction that writes it.
            reached_b.wait(timeout=BLOCKED)
        else:
            reached_b.set()

    hook["fn"] = gate
    errors = run_all(
        lambda: svc.apply_action("rename_realm", "city-0", {"realm": "ALPHA"}),
        lambda: svc.apply_action("set_pop", "city-0", {"pop": 999}),
    )
    assert errors == []
    assert served(svc) == replay(svc)
    # Both edits are in the log, so both must be in the answer.
    assert served(svc)["city-0"]["realm"] == "ALPHA"
    assert served(svc)["city-0"]["pop"] == 999


def test_an_update_racing_a_delete_cannot_resurrect_the_object(svc, hook):
    """A deleted object must not come back because an update was in flight.

    The demonstrated failure: the update read its pre-image before the delete
    committed, then committed an upsert that re-inserted the row the delete had
    removed. The store held and served a row the edit log actively denied — the
    exact class rule 2 exists to prevent — and the safety net never fired
    because the watermark was level with the log.
    """
    update_ready, delete_done = threading.Event(), threading.Event()

    def gate(edit):
        if edit.payload.get("realm") == "ZOMBIE":
            update_ready.set()
            delete_done.wait(timeout=BLOCKED)

    hook["fn"] = gate
    updater = threading.Thread(
        target=lambda: svc.apply_action("rename_realm", "city-0", {"realm": "ZOMBIE"})
    )
    updater.start()
    assert update_ready.wait(timeout=10)
    # The delete blocks here if — and only if — the updater is holding the
    # write transaction while it builds its rows.
    svc.apply_action("raze", "city-0", {})
    delete_done.set()
    updater.join(timeout=30)

    assert "city-0" not in served(svc), "a deleted object came back"
    assert served(svc) == replay(svc)
    assert svc.get("city", "city-0") is None


# -- ordinals ----------------------------------------------------------------

def test_concurrent_creates_take_distinct_positions(svc):
    """``ord`` exists so paging is stable, so two objects must not share one.

    The demonstrated failure: the position was computed from
    ``max_edit_seq() + 1`` read on its own connection *before* the append
    transaction allocated the real one, so four concurrent creates all took
    ORD_CREATED_BASE + 1. ``ord`` is deliberately never updated on conflict, so
    the wrong value was permanent, and a later rebuild renumbered them —
    reshuffling the page someone was looking at.
    """
    ready = threading.Barrier(4, timeout=20)

    def create(i):
        def go():
            ready.wait()
            svc.apply_action("found_city", None, {"name": f"z-{i}", "realm": "new"})
        return go

    assert run_all(*[create(i) for i in range(4)]) == []

    rows = svc.object_store.rows_for("city", [f"z-{i}" for i in range(4)])
    positions = sorted(int(r["ord"]) for r in rows.values())
    assert len(rows) == 4
    assert positions == [ORD_CREATED_BASE + n for n in (1, 2, 3, 4)]
    # And a rebuild reproduces exactly those, rather than renumbering.
    stored = {pk: int(r["ord"]) for pk, r in rows.items()}
    svc.reindex("city")
    rebuilt = svc.object_store.rows_for("city", list(stored))
    assert {pk: int(r["ord"]) for pk, r in rebuilt.items()} == stored


# -- the divergence digest ---------------------------------------------------

def test_concurrent_writes_do_not_raise_a_false_corruption_alarm(svc, hook):
    """The digest is the only detector for a row nobody logged. It has to be
    quiet when nothing is wrong.

    The demonstrated failure: the digest's base was read on its own connection
    before the write transaction opened, so two concurrent writes to *different*
    objects — rows perfectly correct — left ``verify_digest`` reporting False.
    An operator who learns to ignore that has lost the only check that catches
    the two findings above.
    """
    ot = svc.ontology.object_type("city")
    for round_ in range(6):
        both = threading.Barrier(2, timeout=20)

        def edit(pk, realm):
            def go():
                both.wait()
                svc.apply_action("rename_realm", pk, {"realm": realm})
            return go

        hook["fn"] = None
        assert run_all(edit("city-0", f"a{round_}"), edit("city-1", f"b{round_}")) == []
        assert svc.verify_digest(ot), f"false alarm after round {round_}"
        assert served(svc) == replay(svc)


# -- rebuild -----------------------------------------------------------------

def test_a_rebuild_survives_an_edit_committing_underneath_it(svc, monkeypatch):
    """``reindex`` read the ordinals, the watermark and the objects on three
    separate connections. A create landing between the first read and the third
    was in the objects and not in the ordinals, and the rebuild died on a
    ``KeyError`` — an unhandled database-layer exception out of the rebuild
    endpoint, which is also the documented recovery path from every other
    finding here, and is called by ``writeback`` too.
    """
    import uuid

    from laurelin.core.models import EditKind, ObjectEdit

    original = OntologyService._object_ordinals

    def patched(self, ot):
        result = original(self, ot)
        if not patched.fired:
            patched.fired = True
            # Committed to the log only, exactly as a concurrent writer whose
            # transaction lands mid-rebuild would leave it.
            self.store.add_object_edit(ObjectEdit(
                id=uuid.uuid4().hex, object_type="city", pk_value="n-0",
                kind=EditKind.create,
                payload={"name": "n-0", "realm": "late"}, actor="racer",
            ))
        return result

    patched.fired = False
    monkeypatch.setattr(OntologyService, "_object_ordinals", patched)

    svc.reindex("city")  # must not raise

    # The late create is outside the snapshot, so the store is behind by one —
    # which is a lag, which is replayable, which is the safe direction.
    ot = svc.ontology.object_type("city")
    assert not svc.store_is_caught_up(ot)
    assert svc.catch_up("city") == 1
    assert svc.store_is_caught_up(ot)
    assert served(svc) == replay(svc)
    assert "n-0" in served(svc)


# -- the object-type definition ----------------------------------------------

NARROWED = ONTOLOGY.replace("      pop: {type: integer}\n", "", 1)


def test_withdrawing_a_property_stops_it_being_served(svc):
    """Withdrawing a property from the ontology is how an operator stops
    exposing a column through the object API. Every other read path projects to
    the declared set on every read, so it took effect there immediately; the
    materialization had no idea the definition had changed and kept serving it.

    Worse than a stale read: the write path merged each new payload onto the
    stored bag, so every subsequent write copied the withdrawn property
    forward. Under the previous invalidate-on-write semantics the next edit
    forced a rebuild that dropped it — a repair became a refresh.
    """
    assert "pop" in served(svc)["city-0"]

    (svc.workspace.ontology_dir / "o.yml").write_text(NARROWED)
    narrow = OntologyService(svc.workspace, svc.catalog, svc.store,
                             load_ontology(svc.workspace.ontology_dir))
    ot = narrow.ontology.object_type("city")
    assert set(ot.properties) == {"name", "realm"}

    assert not narrow.store_is_caught_up(ot), "a definition change must invalidate"
    assert "pop" not in narrow.query("city", limit=50)["objects"][0]
    assert "pop" not in narrow.get("city", "city-0")

    # …and a write does not launder it back in, before or after a rebuild.
    narrow.apply_action("rename_realm", "city-0", {"realm": "narrowed"})
    assert "pop" not in narrow.get("city", "city-0")
    narrow.reindex("city")
    assert narrow.store_is_caught_up(ot)
    narrow.apply_action("rename_realm", "city-0", {"realm": "again"})
    assert "pop" not in narrow.get("city", "city-0")
    assert json.loads(
        narrow.object_store.rows_for("city", ["city-0"])["city-0"]["props_json"]
    ).keys() == {"name", "realm"}
