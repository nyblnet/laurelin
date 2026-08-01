"""The operational object store: an edit updates the materialization instead of
throwing it away.

The tests here are mostly threat tests, because the interesting failures are
not "it returned the wrong row" but "it returned a *plausible* row". A store
that is behind must be indistinguishable, to every caller, from a store that
does not exist — both mean "fall through to the scan". A store that answers
while behind is the whole hazard, and an empty answer is the worst version of
it: "these objects do not exist" is a valid-looking response that the write
path's existence check would act on.

Everything is asserted against the in-memory replay (``_materialize``), which
reads the edit log directly and is therefore the definition the other two paths
have to match.
"""

import json
import random

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.ontology import OntologyService, load_ontology
from laurelin.ontology.store import (
    ORD_CREATED_BASE,
    MetadataObjectStore,
    ObjectRow,
    StarRocksObjectStore,
    StoreState,
    advance_digest,
    canonical_props,
    digest_hex,
    digest_of_rows,
)

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


def make(tmp_path, name="os", n=6):
    ws = Workspace.init(tmp_path / name, name=name)
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("cities", cities(n))
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    return OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))


@pytest.fixture()
def svc(tmp_path):
    s = make(tmp_path)
    s.reindex("city")
    return s


def replay(svc) -> dict:
    """The oracle: objects as the in-memory replay of the log computes them."""
    ot = svc.ontology.object_type("city")
    return {o["__pk"]: {k: v for k, v in o.items() if not k.startswith("__")}
            for o in svc._materialize(ot)}


def served(svc) -> dict:
    return {o["__pk"]: {k: v for k, v in o.items() if not k.startswith("__")}
            for o in svc.query("city", limit=500)["objects"]}


# -- the feature itself ------------------------------------------------------

def test_an_edit_updates_the_materialization_instead_of_invalidating_it(svc):
    """The whole point. This is the test that proves the feature exists."""
    ot = svc.ontology.object_type("city")
    assert svc.store_is_caught_up(ot)

    svc.apply_action("rename_realm", pk="city-0", parameters={"realm": "changed"})

    assert svc.store_is_caught_up(ot), "one write must not throw the index away"
    state = svc.object_store.state("city")
    assert state.applied_seq == svc.store.max_edit_seq("city") == 1
    # …and the new value is served *from the store*, with no reindex anywhere.
    assert svc._index_query(ot, None, {"name": "city-0"}, 10, 0)["objects"][0][
        "realm"] == "changed"
    assert served(svc) == replay(svc)


@pytest.mark.parametrize("n_edits", [1, 5, 50])
def test_the_store_stays_level_with_the_log_however_many_writes(svc, n_edits):
    ot = svc.ontology.object_type("city")
    for i in range(n_edits):
        svc.apply_action("rename_realm", pk=f"city-{i % 6}",
                         parameters={"realm": f"r{i}"})
        assert svc.store_is_caught_up(ot), f"fell behind at edit {i}"
    assert svc.object_store.state("city").applied_seq == n_edits
    assert served(svc) == replay(svc)


def test_creates_deletes_and_updates_all_land(svc):
    svc.apply_action("found_city", pk=None,
                     parameters={"name": "new-1", "realm": "aman", "pop": 7})
    svc.apply_action("raze", pk="city-1", parameters={})
    svc.apply_action("rename_realm", pk="city-2", parameters={"realm": "x"})
    assert served(svc) == replay(svc)
    assert "city-1" not in served(svc)
    assert served(svc)["new-1"]["realm"] == "aman"


def test_delete_then_recreate_inherits_nothing(svc):
    """A create is an absolute assignment. If anything leaked from the deleted
    object, a recreated key would quietly carry a dead property forward."""
    svc.apply_action("raze", pk="city-3", parameters={})
    svc.apply_action("found_city", pk=None,
                     parameters={"name": "city-3", "realm": "reborn"})
    # Exactly the created payload, padded with the declared-but-unset column as
    # null. What matters is that `pop` is *not* the 103 the deleted object had.
    assert served(svc)["city-3"] == {"name": "city-3", "realm": "reborn", "pop": None}
    assert served(svc) == replay(svc)


def test_the_store_and_the_scan_give_a_created_object_the_same_shape(svc):
    """One object, one JSON shape, whichever path answered.

    A create that omits a declared property returned ``{'name', 'realm'}`` from
    the store and the in-memory replay, and ``{'name', 'realm', 'pop': None}``
    from the pushdown — which is also what it becomes the moment a writeback
    folds it into the dataset. So whether a client saw the key at all depended
    on whether the type happened to be materialized and whether the overlay had
    been folded yet. No value is lost either way, but a client distinguishing
    absent from null sees the object change under it.
    """
    ot = svc.ontology.object_type("city")
    svc.apply_action("found_city", pk=None, parameters={"name": "new", "realm": "n"})

    indexed = svc._index_query(ot, None, {"name": "new"}, 1, 0)["objects"][0]
    scanned = svc._sql_query(ot, None, {"name": "new"}, 1, 0)["objects"][0]
    assert indexed == scanned
    assert indexed["pop"] is None
    assert replay(svc)["new"] == {"name": "new", "realm": "n", "pop": None}


def test_create_delete_create(svc):
    svc.apply_action("found_city", pk=None, parameters={"name": "z", "realm": "a"})
    svc.apply_action("raze", pk="z", parameters={})
    svc.apply_action("found_city", pk=None, parameters={"name": "z", "realm": "b"})
    assert served(svc)["z"]["realm"] == "b"
    assert served(svc) == replay(svc)


# -- the lost write ----------------------------------------------------------

def test_a_failed_materialization_never_serves_the_old_value(svc, monkeypatch):
    """Fail after the log commits. The read must return the NEW value or refuse
    to answer from the store — never the value the edit replaced."""
    ot = svc.ontology.object_type("city")
    def boom(*a, **k):
        raise RuntimeError("materialize failed")

    monkeypatch.setattr(svc.object_store, "commit_edit", boom)

    svc.apply_action("rename_realm", pk="city-0", parameters={"realm": "survived"})
    monkeypatch.undo()

    assert not svc.store_is_caught_up(ot), "a lagging store must be detectable"
    state = svc.object_store.state("city")
    assert state.applied_seq < svc.store.max_edit_seq("city")
    assert svc.index_state(ot)["lag"] == 1, "the lag is reported, not hidden"
    # The store refuses; the scan answers, and it answers correctly.
    assert svc._index_query(ot, None, None, 10, 0) is None
    assert served(svc)["city-0"]["realm"] == "survived"
    assert served(svc) == replay(svc)


def test_catch_up_converges_after_a_failed_materialization(svc, monkeypatch):
    ot = svc.ontology.object_type("city")
    monkeypatch.setattr(svc.object_store, "commit_edit",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    for i in range(3):
        svc.apply_action("rename_realm", pk=f"city-{i}", parameters={"realm": f"r{i}"})
    monkeypatch.undo()
    assert not svc.store_is_caught_up(ot)

    # The count is deliberately not asserted: a later write already catches the
    # store up on earlier edits, so how many are left is a scheduling detail.
    # Convergence is the property.
    svc.catch_up("city")
    assert svc.store_is_caught_up(ot)
    assert svc._index_query(ot, None, None, 10, 0) is not None
    assert served(svc) == replay(svc)


def test_the_next_write_catches_the_store_up_by_itself(svc, monkeypatch):
    """Catch-up lives on the write path, never inside a read: a read that
    writes breaks read-only replicas and races other readers for no gain."""
    ot = svc.ontology.object_type("city")
    monkeypatch.setattr(svc.object_store, "commit_edit",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    svc.apply_action("rename_realm", pk="city-0", parameters={"realm": "lost"})
    monkeypatch.undo()
    assert not svc.store_is_caught_up(ot)

    # A read does NOT repair it...
    svc.query("city", limit=10)
    assert not svc.store_is_caught_up(ot)
    # ...the next write does.
    svc.apply_action("rename_realm", pk="city-1", parameters={"realm": "next"})
    assert svc.store_is_caught_up(ot)
    assert served(svc) == replay(svc)


# -- the stale read ----------------------------------------------------------

def test_an_unapplied_edit_makes_the_store_refuse(svc):
    """Appending straight to the log is the footgun ``add_object_edit`` now is:
    the log advances, the materialization does not, and the store must notice."""
    import uuid

    from laurelin.core.models import EditKind, ObjectEdit

    ot = svc.ontology.object_type("city")
    svc.store.add_object_edit(ObjectEdit(
        id=uuid.uuid4().hex, object_type="city", pk_value="city-0",
        kind=EditKind.update, payload={"realm": "smuggled"}, actor="t",
    ))
    assert not svc.store_is_caught_up(ot)
    assert svc._index_query(ot, None, None, 10, 0) is None
    assert served(svc)["city-0"]["realm"] == "smuggled"


def test_a_new_dataset_version_still_invalidates(svc):
    """A version can rewrite arbitrary base rows and renumbers every ordinal,
    so no delta expresses it. A build stays the one thing that invalidates."""
    ot = svc.ontology.object_type("city")
    svc.apply_action("rename_realm", pk="city-0", parameters={"realm": "kept"})
    assert svc.store_is_caught_up(ot)

    svc.catalog.append("cities", pa.table({
        "name": ["new-city"], "realm": ["valinor"],
        "pop": pa.array([999], type=pa.int64()),
    }))
    assert not svc.store_is_caught_up(ot)
    assert svc.catch_up("city") == 0, "a version bump is not a delta"
    assert svc.query("city", limit=20)["total"] == 7
    svc.reindex("city")
    assert svc.store_is_caught_up(ot)
    assert served(svc) == replay(svc)


def test_a_missing_state_row_is_behind_not_empty(svc):
    ot = svc.ontology.object_type("city")
    svc.store.drop_object_index("city")
    assert svc.object_store.page("city", limit=10) is None
    assert svc._index_query(ot, None, None, 10, 0) is None
    assert svc.query("city", limit=10)["total"] == 6, "never a plausible-looking zero"


@pytest.mark.parametrize("seed", range(6))
def test_randomized_interleavings_always_agree_with_a_full_replay(tmp_path, seed):
    """Property test: every store-answered read equals a replay at that instant,
    or is refused. Never a third option."""
    rng = random.Random(seed)
    svc = make(tmp_path, name=f"fuzz{seed}")
    svc.reindex("city")
    ot = svc.ontology.object_type("city")
    live = [f"city-{i}" for i in range(6)]
    minted = 0

    for step in range(40):
        choice = rng.random()
        if choice < 0.5 and live:
            svc.apply_action("rename_realm", pk=rng.choice(live),
                             parameters={"realm": f"r{step}"})
        elif choice < 0.75:
            minted += 1
            pk = f"minted-{minted}"
            svc.apply_action("found_city", pk=None,
                             parameters={"name": pk, "realm": "new", "pop": step})
            live.append(pk)
        elif live:
            pk = rng.choice(live)
            svc.apply_action("raze", pk=pk, parameters={})
            live.remove(pk)

        page = svc._index_query(ot, None, None, 500, 0)
        oracle = replay(svc)
        if page is not None:
            got = {o["__pk"]: {k: v for k, v in o.items() if not k.startswith("__")}
                   for o in page["objects"]}
            assert got == oracle, f"step {step}"
        assert served(svc) == oracle, f"step {step} (whichever path answered)"


# -- watermark ordering ------------------------------------------------------

@pytest.mark.parametrize("fail_at", ["commit", "search_sync"])
def test_the_watermark_never_runs_ahead_of_the_rows(svc, monkeypatch, fail_at):
    """The asymmetry that the whole design rests on: a watermark *behind* its
    rows costs an idempotent replay; a watermark *ahead* of them is a silent
    permanent stale read."""
    ot = svc.ontology.object_type("city")
    if fail_at == "commit":
        monkeypatch.setattr(svc.object_store, "commit_edit",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    else:
        monkeypatch.setattr(svc.store.backend, "upsert_search_rows",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    svc.apply_action("rename_realm", pk="city-0", parameters={"realm": "x"})
    monkeypatch.undo()

    state = svc.object_store.state("city")
    rows = svc.object_store.rows_for("city", ["city-0"])
    if state.applied_seq >= 1:
        assert json.loads(rows["city-0"]["props_json"])["realm"] == "x", (
            "the watermark claims the edit landed, so the row must have landed"
        )
    else:
        assert not svc.store_is_caught_up(ot)
    assert served(svc) == replay(svc)


def test_replaying_an_applied_edit_is_a_no_op(svc):
    ot = svc.ontology.object_type("city")
    svc.apply_action("rename_realm", pk="city-0", parameters={"realm": "once"})
    before = (svc.object_store.state("city"), served(svc))

    edit = svc.store.list_object_edits("city")[0]
    svc._apply_to_store(ot, edit)
    svc._apply_to_store(ot, edit)

    after = (svc.object_store.state("city"), served(svc))
    assert before[1] == after[1]
    assert before[0].applied_seq == after[0].applied_seq
    assert before[0].digest == after[0].digest


def test_an_older_position_leaves_the_row_alone(svc):
    """Conflicts resolve by log position, not by arrival order."""
    import uuid

    from laurelin.core.models import EditKind, ObjectEdit

    svc.apply_action("rename_realm", pk="city-0", parameters={"realm": "newer"})
    stale = ObjectEdit(id=uuid.uuid4().hex, object_type="city", pk_value="city-0",
                       kind=EditKind.update, payload={"realm": "older"}, actor="t",
                       edit_seq=0)
    svc.object_store.apply_edit(
        stale, pks=["city-0"],
        build=lambda pre_image, seq: (
            [ObjectRow(pk="city-0", ord=0, title="city-0", search_text="",
                       props={"name": "city-0", "realm": "older", "pop": 100})],
            [],
        ),
    )
    assert svc.object_store.rows_for("city", ["city-0"])["city-0"]["applied_seq"] == 1
    assert json.loads(
        svc.object_store.rows_for("city", ["city-0"])["city-0"]["props_json"]
    )["realm"] == "newer"


# -- the phantom write -------------------------------------------------------

def test_a_hand_inserted_row_is_caught_by_the_digest(svc):
    """A watermark cannot detect divergence — a diverged store can be perfectly
    caught up by position. That is what the digest is for."""
    ot = svc.ontology.object_type("city")
    assert svc.verify_digest(ot) is True

    with svc.store._conn() as c:
        c.execute(
            "INSERT INTO object_index (object_type, pk, ord, applied_seq, title,"
            " search_text, props_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("city", "phantom", 99, 0, "phantom", "phantom", '{"name":"phantom"}'),
        )
    assert svc.verify_digest(ot) is False, "a row nobody logged must be visible"

    svc.reindex("city")
    assert svc.verify_digest(ot) is True
    assert "phantom" not in served(svc)


def test_the_materialization_is_driven_by_the_committed_log(svc):
    """Not by the in-memory ObjectEdit: what is durable is what the edit log
    holds, and the store must describe that, not what the request intended."""
    edit = svc.apply_action("rename_realm", pk="city-0", parameters={"realm": "v"})
    stored = svc.store.list_object_edits("city")
    assert [e.id for e in stored] == [edit.id]
    assert stored[0].edit_seq == svc.object_store.state("city").applied_seq


# -- the digest --------------------------------------------------------------

def test_the_digest_is_order_independent(tmp_path):
    """Any shuffle of the edit stream that reaches the same state must produce
    the same digest, or the check reports drift that is not there."""
    rows = [{"pk": f"p{i}", "applied_seq": i, "props_json": canonical_props({"a": i})}
            for i in range(8)]
    forward = digest_of_rows(rows)
    backward = digest_of_rows(list(reversed(rows)))
    shuffled = digest_of_rows(random.Random(0).sample(rows, len(rows)))
    assert forward == backward == shuffled


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda r: r.update(props_json=canonical_props({"a": 99})), id="mutated"),
    pytest.param(lambda r: r.update(applied_seq=999), id="stale-position"),
    pytest.param(lambda r: r.update(pk="other"), id="renamed-key"),
])
def test_the_digest_notices(mutate):
    rows = [{"pk": f"p{i}", "applied_seq": i, "props_json": canonical_props({"a": i})}
            for i in range(4)]
    before = digest_of_rows(rows)
    mutate(rows[1])
    assert digest_of_rows(rows) != before


def test_incremental_digest_equals_from_scratch(svc):
    """The XOR maintenance has to land on the same number a full recomputation
    would, or "maintained incrementally" is just a different digest."""
    for i in range(5):
        svc.apply_action("rename_realm", pk=f"city-{i}", parameters={"realm": f"r{i}"})
    svc.apply_action("found_city", pk=None, parameters={"name": "extra", "realm": "e"})
    svc.apply_action("raze", pk="city-5", parameters={})

    incremental = svc.object_store.state("city").digest
    svc.reindex("city")
    assert svc.object_store.state("city").digest != ""
    # reindex recomputes every row at the same watermark, so the two agree.
    assert svc.object_store.state("city").digest == incremental


def test_advance_digest_removes_and_re_adds():
    before = [{"pk": "a", "applied_seq": 1, "props_json": "{}"}]
    after = [{"pk": "a", "applied_seq": 2, "props_json": '{"x":1}'}]
    start = digest_hex(digest_of_rows(before))
    assert advance_digest(start, before, after) == digest_hex(digest_of_rows(after))


# -- round-trip fidelity -----------------------------------------------------

@pytest.mark.parametrize("value", [
    "", "  ", "line\nbreak", "tab\there", "quote'and\"quote", "back\\slash",
    "emoji 🜁🜂", "combining é vs é", "nul\x00inside", "x" * 4000,
    "'; DROP TABLE object_index; --",
])
def test_string_payloads_round_trip_byte_for_byte(svc, value):
    svc.apply_action("rename_realm", pk="city-0", parameters={"realm": value})
    assert served(svc)["city-0"]["realm"] == value
    assert served(svc) == replay(svc)


@pytest.mark.parametrize("value", [0, -1, 2 ** 53 + 1, 2 ** 62, -(2 ** 62)])
def test_integer_payloads_keep_value_and_type(svc, value):
    svc.apply_action("found_city", pk=None,
                     parameters={"name": "big", "realm": "r", "pop": value})
    got = served(svc)["big"]["pop"]
    assert got == value and isinstance(got, int)


# -- rebuild -----------------------------------------------------------------

def test_a_rebuild_reproduces_the_incremental_state_exactly(svc):
    for i in range(4):
        svc.apply_action("rename_realm", pk=f"city-{i}", parameters={"realm": f"r{i}"})
    svc.apply_action("found_city", pk=None, parameters={"name": "m", "realm": "q"})
    incremental = served(svc)
    order = [o["__pk"] for o in svc.query("city", limit=50)["objects"]]

    svc.reindex("city")
    assert served(svc) == incremental
    assert [o["__pk"] for o in svc.query("city", limit=50)["objects"]] == order


def test_a_failed_rebuild_does_not_come_back_claiming_freshness(svc, monkeypatch):
    ot = svc.ontology.object_type("city")
    svc.apply_action("rename_realm", pk="city-0", parameters={"realm": "x"})
    monkeypatch.setattr(svc.store, "replace_object_index",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        svc.reindex("city")
    monkeypatch.undo()
    # The old state is intact and still honest about what it holds.
    assert svc.store_is_caught_up(ot)
    assert served(svc) == replay(svc)


# -- the StarRocks implementation --------------------------------------------
#
# Against an in-memory double, NOT a real server. The Track A agent measured the
# engine behaviours this leans on (Stream Load is byte-faithful with no
# escaping, upsert-by-pk works, a bad row aborts the whole load, data rows plus
# a sentinel land in one transaction) but no part of StarRocksObjectStore has
# ever run against StarRocks. These tests pin the *protocol* the class speaks —
# that the watermark rides in the same load as the data, that a delete carries
# op=1, that an unreachable client is reported as "behind" and never as "empty"
# — not that a real StarRocks accepts it.

class FakeStarRocks:
    """A primary-key table with Stream Load semantics, in a dict."""

    def __init__(self):
        self.rows: dict[tuple, dict] = {}
        self.loads: list[list[dict]] = []
        self.fail = False
        # Fails the *load* while reads keep working — which is the one gap the
        # design documents, between the log commit and the load.
        self.fail_loads = False

    def _check(self):
        if self.fail:
            raise ConnectionError("BE unreachable")

    def stream_load(self, table, records):
        self._check()
        if self.fail_loads:
            raise ConnectionError("load rejected")
        # All-or-nothing: a bad row aborts the entire load, so validate first.
        for r in records:
            if not isinstance(r.get("pk"), str):
                raise ValueError("bad row; whole load aborted")
        self.loads.append(list(records))
        for r in records:
            key = (r["object_type"], r["pk"])
            if r.get("op") == 1:
                self.rows.pop(key, None)
            else:
                self.rows[key] = {k: v for k, v in r.items() if k != "op"}

    def execute(self, sql, params=()):
        self._check()
        if sql.startswith("DELETE"):
            ot = params[0]
            for key in [k for k in self.rows if k[0] == ot]:
                del self.rows[key]

    def query(self, sql, params=()):
        self._check()
        ot = params[0]
        rows = [r for r in self.rows.values() if r["object_type"] == ot]
        if "pk = ?" in sql and "ord = ?" in sql:  # the watermark probe
            return [r for r in rows if r["pk"] == params[1] and r["ord"] == params[2]]
        rows = [r for r in rows if r["ord"] >= 0]
        rest = list(params[1:])
        if "search_text LIKE ?" in sql:
            needle = rest.pop(0).strip("%")
            rows = [r for r in rows if needle in r["search_text"]]
        if "pk = ?" in sql:
            wanted = rest.pop(0)
            rows = [r for r in rows if r["pk"] == wanted]
        if "pk IN (" in sql:
            wanted = set(rest)
            rows = [r for r in rows if r["pk"] in wanted]
        rows.sort(key=lambda r: r["ord"])
        if "count(*)" in sql:
            return [{"n": len(rows)}]
        if "LIMIT ? OFFSET ?" in sql:
            limit, offset = rest[-2], rest[-1]
            rows = rows[offset:offset + limit]
        return rows


@pytest.fixture()
def sr(tmp_path):
    client = FakeStarRocks()
    svc = make(tmp_path, name="sr")
    svc.object_store = StarRocksObjectStore(svc.store, client, "objects__sr")
    svc.reindex("city")
    return svc, client


def test_starrocks_serves_what_the_replay_says(sr):
    svc, _ = sr
    assert served(svc) == replay(svc)
    svc.apply_action("rename_realm", pk="city-0", parameters={"realm": "sr"})
    assert svc.store_is_caught_up(svc.ontology.object_type("city"))
    assert served(svc) == replay(svc)


def test_starrocks_puts_the_watermark_in_the_same_load_as_the_data(sr):
    svc, client = sr
    client.loads.clear()
    svc.apply_action("rename_realm", pk="city-0", parameters={"realm": "atomic"})
    assert len(client.loads) == 1, "two loads means a window with no watermark"
    load = client.loads[0]
    assert any(r["pk"] == StarRocksObjectStore.WATERMARK_PK for r in load)
    assert any(r["pk"] == "city-0" for r in load)


def test_the_starrocks_watermark_row_is_never_a_result(sr):
    svc, client = sr
    assert StarRocksObjectStore.WATERMARK_PK not in served(svc)
    assert svc.query("city", limit=50)["total"] == 6


def test_a_starrocks_delete_carries_the_delete_op(sr):
    svc, client = sr
    client.loads.clear()
    svc.apply_action("raze", pk="city-2", parameters={})
    ops = {r["pk"]: r.get("op") for r in client.loads[0]}
    assert ops["city-2"] == 1
    assert ops[StarRocksObjectStore.WATERMARK_PK] == 0
    assert "city-2" not in served(svc)
    assert served(svc) == replay(svc)


def test_an_unreachable_starrocks_falls_back_and_never_returns_empty(sr):
    svc, client = sr
    ot = svc.ontology.object_type("city")
    client.fail = True
    assert svc.object_store.state("city") is None, "unreachable reads as behind"
    assert svc._index_query(ot, None, None, 10, 0) is None
    result = svc.query("city", limit=50)
    assert result["total"] == 6, "the scan answers; an empty page would be a lie"


def test_an_unreachable_starrocks_does_not_fail_the_users_write(sr):
    svc, client = sr
    client.fail = True
    svc.apply_action("rename_realm", pk="city-0", parameters={"realm": "written"})
    client.fail = False
    # The write is durable in the log even though the materialization missed it.
    assert svc.store.max_edit_seq("city") == 1
    assert not svc.store_is_caught_up(svc.ontology.object_type("city"))
    assert served(svc)["city-0"]["realm"] == "written"


def test_a_starrocks_load_failing_after_the_log_commit_is_not_a_failed_write(sr):
    """The one non-atomic gap in this design, made harmless.

    StarRocks logs first and loads second, so a failure strictly between the
    two leaves the edit durable and visible to every reader. Raising there told
    the caller their write had failed — a phantom failure that invites a retry
    and produces a second edit — and the caller's recovery path then appended
    the *same* edit again, hitting the log's primary key, burning every retry
    and aborting before the audit record was written. So an action that took
    effect had no trace in the audit log at all.
    """
    svc, client = sr
    client.fail_loads = True
    svc.apply_action("rename_realm", pk="city-0", parameters={"realm": "GHOST"})
    client.fail_loads = False

    edits = svc.store.list_object_edits("city")
    assert len(edits) == 1, "the edit must be logged exactly once"
    assert [a.action for a in svc.store.list_audit(limit=10)].count(
        "action_applied") == 1, "an action that took effect must be auditable"
    # The store is behind, which is a lag: detectable, replayable, and the
    # reads route around it.
    ot = svc.ontology.object_type("city")
    assert not svc.store_is_caught_up(ot)
    assert served(svc) == replay(svc)
    assert svc.catch_up("city") == 1
    assert svc.store_is_caught_up(ot)
    assert served(svc)["city-0"]["realm"] == "GHOST"


def test_starrocks_refuses_to_apply_from_a_position_it_is_not_level_with(sr):
    """No shared transaction means no lock, so the pre-image can only be
    trusted when the watermark proves nothing has landed since. Applying an
    edit the store is not immediately behind would merge onto a row that some
    unapplied edit has already changed — and advance the watermark past it,
    which is the one direction this design must never allow."""
    svc, client = sr
    client.fail_loads = True
    svc.apply_action("rename_realm", pk="city-0", parameters={"realm": "first"})
    svc.apply_action("rename_realm", pk="city-1", parameters={"realm": "second"})
    client.fail_loads = False
    assert svc.object_store.state("city").applied_seq == 0
    assert svc.store.max_edit_seq("city") == 2

    ot = svc.ontology.object_type("city")
    second = svc.store.list_object_edits("city")[1]
    assert second.edit_seq == 2
    svc._apply_to_store(ot, second)
    assert svc.object_store.state("city").applied_seq == 0, "skipped edit 1"

    assert svc.catch_up("city") == 2, "in order, it replays both"
    assert svc.store_is_caught_up(ot)
    assert served(svc) == replay(svc)


def test_a_starrocks_table_with_rows_but_no_watermark_falls_back(sr):
    svc, client = sr
    for key in [k for k in client.rows if k[1] == StarRocksObjectStore.WATERMARK_PK]:
        del client.rows[key]
    assert svc.object_store.state("city") is None
    assert svc.query("city", limit=50)["total"] == 6


def test_a_bad_row_aborts_the_whole_starrocks_load(sr):
    svc, client = sr
    before = dict(client.rows)
    with pytest.raises(ValueError):
        client.stream_load("objects__sr", [
            {"object_type": "city", "pk": "ok", "ord": 1, "applied_seq": 1,
             "title": "", "search_text": "", "props_json": "{}", "op": 0},
            {"object_type": "city", "pk": None, "ord": 2, "applied_seq": 1,
             "title": "", "search_text": "", "props_json": "{}", "op": 0},
        ])
    assert client.rows == before, "all-or-nothing, so no partial delta"


def test_starrocks_and_metadata_stores_agree_object_for_object(tmp_path):
    """Two implementations of one interface must not be two definitions of what
    an object is."""
    a = make(tmp_path, name="cmp-a")
    b = make(tmp_path, name="cmp-b")
    b.object_store = StarRocksObjectStore(b.store, FakeStarRocks(), "objects__b")
    for svc in (a, b):
        svc.reindex("city")
        svc.apply_action("rename_realm", pk="city-0", parameters={"realm": "same"})
        svc.apply_action("found_city", pk=None, parameters={"name": "n", "realm": "q"})
        svc.apply_action("raze", pk="city-4", parameters={})
    assert served(a) == served(b) == replay(a)
    assert ([o["__pk"] for o in a.query("city", limit=50)["objects"]]
            == [o["__pk"] for o in b.query("city", limit=50)["objects"]])


# -- the interface itself ----------------------------------------------------

def test_both_stores_satisfy_the_protocol():
    from laurelin.ontology.store import ObjectStore
    for cls in (MetadataObjectStore, StarRocksObjectStore):
        for method in ("state", "replace", "drop", "commit_edit", "apply_edit",
                       "rows_for", "page"):
            assert hasattr(cls, method), f"{cls.__name__} is missing {method}"
    # A Protocol with a non-method member ('name') cannot be issubclass-checked,
    # so conformance is asserted structurally above and by every other test in
    # this file running the same assertions through both implementations.
    assert ObjectStore.__name__ == "ObjectStore"


def test_state_collapses_never_built_and_unreachable(tmp_path):
    """Deliberate: the caller must not be able to tell them apart, because the
    correct response — fall through — is identical."""
    client = FakeStarRocks()
    store = StarRocksObjectStore(None, client, "t")
    assert store.state("city") is None          # never built
    client.fail = True
    assert store.state("city") is None          # unreachable


def test_created_objects_sort_after_every_base_row():
    assert ORD_CREATED_BASE > 10 ** 12
    assert StoreState.from_dict(None) is None


def test_the_starrocks_ddl_defaults_every_non_key_column(sr):
    """Measured requirement, not style: a delete load carries only the key, and
    without NULL-with-a-default it fails with "Column has no default value: ord"."""
    from laurelin.ontology.store import STARROCKS_TABLE_DDL, starrocks_table_name

    ddl = STARROCKS_TABLE_DDL.format(table=starrocks_table_name("ws"))
    assert "`objects__ws`" in ddl
    assert 'PRIMARY KEY(object_type, pk)' in ddl
    for col in ("ord", "applied_seq", "title", "search_text", "props_json"):
        line = next(ln for ln in ddl.splitlines() if ln.strip().startswith(col))
        assert "NULL" in line and "NOT NULL" not in line, col
