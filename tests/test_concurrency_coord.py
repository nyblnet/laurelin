"""Store-level coordination invariants: exactly-once claims and the watermark.

Every replica-coordination claim this project makes is arbitrated by the
metadata store: a conditional UPDATE decides who runs a build and who fires a
schedule, and the object-index watermark certifies which edits the
materialization holds. These tests put N threads on one barrier and assert the
invariant — a count, a set, a monotone sequence — never a timing.

Each ``MetadataStore`` method opens its own connection, so N threads sharing
one store object genuinely contend in the database, exactly as N replicas
would. SQLite and Postgres serialize these writes with different mechanisms
(WAL single-writer vs. row locks under READ COMMITTED), so every test runs on
both.
"""

from __future__ import annotations

import json
import threading
import uuid

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import EditKind, ObjectEdit, ScheduleInfo
from laurelin.core.scheduler import Scheduler
from laurelin.ontology import OntologyService, load_ontology
from tests.concurrency_harness import (
    BACKENDS,
    open_store,
    pause_hook,
    rounds,
    run_racers,
)

A_LONG_TIME_AGO = "2000-01-01T00:00:00+00:00"

soak = pytest.mark.soak


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    yield from open_store(request.param, tmp_path)


def _no_errors(errors):
    bad = [f"racer {i}: {exc!r}" for i, exc in enumerate(errors) if exc is not None]
    assert not bad, "racers raised: " + "; ".join(bad)


# -- exactly-once: builds ------------------------------------------------------


@soak
def test_exactly_one_of_n_simultaneous_claimants_wins_a_build(store):
    """With several replicas serving one workspace, any of them may accept
    "run a build" — but exactly one must execute it. The lease claim is one
    conditional UPDATE (db.py claim_build); 8 threads hitting it at the same
    instant must produce exactly one winner, and the row must name that
    winner."""
    for _ in range(rounds(20)):
        build = store.create_build(["target"])
        executions: list[int] = []  # what each winner would go on to do
        lock = threading.Lock()

        def racer(i):
            won = store.claim_build(build.id, f"worker-{i}")
            if won:
                with lock:
                    executions.append(i)
            return won

        wins, errors = run_racers(8, racer)
        _no_errors(errors)
        assert sum(wins) == 1, (
            f"build {build.id}: {sum(wins)} of 8 simultaneous claimants won "
            f"(winners: {[i for i, w in enumerate(wins) if w]})"
        )
        assert len(executions) == 1, "exactly one worker may execute the build"
        with store._conn() as c:
            row = c.execute(
                "SELECT claimed_by FROM builds WHERE id = ?", (build.id,)
            ).fetchone()
        assert row["claimed_by"] == f"worker-{executions[0]}", (
            "the database must name the same winner the return values did"
        )


# -- exactly-once: schedules ---------------------------------------------------


@soak
def test_exactly_one_replica_claims_a_due_schedule(store):
    """Every replica polls every schedule; firing requires winning the claim
    (db.py claim_schedule, one conditional UPDATE). 8 replicas claiming one
    due schedule at the same instant: exactly one winner."""
    for r in range(rounds(20)):
        name = f"nightly-{r}"
        store.upsert_schedule(
            ScheduleInfo(
                name=name, trigger="cron", cron="* * * * *",
                next_run_at=A_LONG_TIME_AGO,
            )
        )

        def racer(i):
            return store.claim_schedule(name, f"replica-{i}")

        wins, errors = run_racers(8, racer)
        _no_errors(errors)
        assert sum(wins) == 1, (
            f"schedule {name}: {sum(wins)} of 8 replicas won the claim "
            f"(winners: {[i for i, w in enumerate(wins) if w]})"
        )


# -- watermark: never ahead of the data it certifies ---------------------------


def _props(v) -> str:
    return json.dumps({"v": v})


def _seed_index(store, pks: list[str]) -> None:
    store.replace_object_index(
        "thing",
        [
            {"pk": pk, "ord": i, "applied_seq": 0, "title": pk,
             "search_text": pk, "props_json": _props(0)}
            for i, pk in enumerate(pks)
        ],
        dataset_version=1,
        applied_seq=0,
    )


def _commit_update(store, pk: str, value: int) -> int:
    """One edit through the real write path: log append + row + watermark in
    a single transaction, exactly as the ontology write path drives it."""
    edit = ObjectEdit(
        id=uuid.uuid4().hex, object_type="thing", pk_value=pk,
        kind=EditKind.update, payload={"v": value}, actor="racer",
    )

    def build(pre_image, seq):
        row = pre_image[pk]  # read inside the write transaction, under the lock
        return (
            [{"pk": pk, "ord": int(row["ord"]), "title": row["title"],
              "search_text": row["search_text"], "props_json": _props(value)}],
            [],
        )

    return store.commit_object_edit(
        edit, pks=[pk], build=build, digest_of=lambda cur, before, after: ""
    )


@soak
def test_the_watermark_is_never_observed_ahead_of_the_rows_it_certifies(store):
    """The contract of the object index: rows first, watermark last, one
    transaction (db.py _apply_index_delta). A watermark ahead of its rows is a
    silent permanent stale read — the one direction the design must never
    allow.

    4 writers commit edits (each to its own pk) while 2 readers page the index
    in a loop. Every page carries its state row from the same transaction
    (``search_object_index`` reads both together), so each observation is a
    consistent snapshot to check:

    * the watermark never exceeds the highest ``applied_seq`` among the rows
      it was returned with (never ahead of the data);
    * successive watermark observations never decrease (monotone);
    * afterwards: watermark == MAX(edit_seq), the seqs are gapless, and every
      pk holds its writer's last value (no lost update).
    """
    n_writers, n_readers = 4, 2
    per_writer = rounds(25)
    pks = [f"p{w}" for w in range(n_writers)]
    _seed_index(store, pks)

    seqs: dict[int, list[int]] = {w: [] for w in range(n_writers)}
    violations: list[str] = []
    errors: list[str] = []
    observed = {r: 0 for r in range(n_readers)}
    stop = threading.Event()
    barrier = threading.Barrier(n_writers + n_readers, timeout=30)

    def writer(w: int) -> None:
        try:
            barrier.wait()
            for k in range(per_writer):
                seqs[w].append(_commit_update(store, f"p{w}", k + 1))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"writer {w}: {exc!r}")

    def reader(r: int) -> None:
        try:
            barrier.wait()
            last = -1
            while not stop.is_set():
                rows, _total, state = store.search_object_index("thing", limit=100)
                if state is None:
                    continue
                wm = int(state["applied_seq"])
                if wm < last:
                    violations.append(
                        f"reader {r}: watermark went backwards {last} -> {wm}"
                    )
                last = wm
                data_high = max((int(x["applied_seq"]) for x in rows), default=0)
                if wm > data_high:
                    violations.append(
                        f"reader {r}: watermark {wm} observed AHEAD of its rows "
                        f"(max row applied_seq {data_high})"
                    )
                observed[r] += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"reader {r}: {exc!r}")

    writers = [threading.Thread(target=writer, args=(w,), daemon=True)
               for w in range(n_writers)]
    readers = [threading.Thread(target=reader, args=(r,), daemon=True)
               for r in range(n_readers)]
    for t in writers + readers:
        t.start()
    for t in writers:
        t.join(120)
    stop.set()
    for t in readers:
        t.join(30)
    assert not [t for t in writers + readers if t.is_alive()], "threads never finished"
    assert not errors, errors
    assert not violations, violations
    assert all(observed[r] > 0 for r in observed), "readers must actually observe pages"

    total = n_writers * per_writer
    state = store.object_index_state("thing")
    assert state is not None
    assert int(state["applied_seq"]) == store.max_edit_seq("thing") == total, (
        "after the dust settles the watermark is level with the log"
    )
    all_seqs = sorted(s for lst in seqs.values() for s in lst)
    assert all_seqs == list(range(1, total + 1)), (
        "edit seqs must be gapless and unique under concurrency"
    )
    rows, _total, _state = store.search_object_index("thing", limit=100)
    by_pk = {r["pk"]: r for r in rows}
    for w in range(n_writers):
        row = by_pk[f"p{w}"]
        assert row["props"] == {"v": per_writer}, (
            f"p{w}: last committed value lost (got {row['props']})"
        )
        assert int(row["applied_seq"]) == seqs[w][-1], (
            f"p{w}: row does not carry its last edit's position"
        )


# -- the scheduler fires once per window, even across replicas -----------------


def test_a_schedule_fires_once_per_window_even_with_a_stale_due_list(store):
    """Exactly-once per tick across replicas is the scheduler's headline
    claim. The dangerous interleaving is not two claims racing (the
    conditional UPDATE settles that) but a *stale due list*:

        replica B: due_schedules() -> [nightly]          (parked here)
        replica A: due_schedules() -> claim -> run -> record_schedule_run
                   (record RELEASES the claim and sets next_run_at)
        replica B: claim_schedule(nightly) -> ?

    B's claim is evaluated against a schedule that already fired this window.
    If it succeeds, the action runs twice in one window. The invariant: the
    action runs exactly once per window, counted at the action itself — never
    via ``tick()``'s return value, which appends the name before running
    (scheduler.py:140).

    This is a real interleaving two pollers produce whenever their ticks
    overlap; the park merely makes it deterministic instead of probabilistic.
    """
    store_a = store
    # Another handle on the same underlying database — the moral equivalent of
    # a second pod pointing at the same metadata.
    store_b = MetadataStore(store.path, schema=store.schema)
    for r in range(rounds(3)):
        name = f"nightly-{r}"
        store_a.upsert_schedule(
            ScheduleInfo(name=name, trigger="cron", cron="* * * * *",
                         next_run_at=A_LONG_TIME_AGO)
        )

        fires: list[str] = []
        lock = threading.Lock()

        def make_run_action(worker):
            def run_action(schedule):
                with lock:
                    fires.append(worker)
                return None
            return run_action

        sched_a = Scheduler(
            lambda: [("ws", store_a, make_run_action("A"))],
            worker_id="A", poll_seconds=9999,
        )
        sched_b = Scheduler(
            lambda: [("ws", store_b, make_run_action("B"))],
            worker_id="B", poll_seconds=9999,
        )

        # Park replica B between reading its due list and claiming — the gap
        # every real poll loop has. The hook parks AFTER the real call, with
        # no connection held.
        with pause_hook(store_b, "due_schedules") as gate:
            t = threading.Thread(target=sched_b.tick, daemon=True)
            t.start()
            gate.wait_reached()

            # Replica A completes the whole window while B holds a stale list.
            sched_a.tick()
            assert fires == ["A"], "replica A owns this window"

            gate.open()
            t.join(30)
            assert not t.is_alive(), "replica B's tick never finished"

        assert fires == ["A"], (
            f"schedule {name}: fired {len(fires)} times in one window "
            f"(by {fires}); a stale due list must not yield a second fire"
        )


# -- stale owners cannot release their successors' claims ----------------------
#
# These two are sequential-interleaving tests: the defect is a missing
# owner guard on a release, so the race is expressed as ordered
# single-threaded store calls. Zero threads, zero flake — the interleaving is
# one lease expiry genuinely produces (a runner stalls past its lease, a
# successor takes over, the stalled runner finally finishes and reports).
# Both call the release exactly as each code shape's runner does: with the
# worker identity when the store accepts one, bare otherwise.


def _call_with_worker_if_supported(fn, /, *args, worker: str, **kwargs):
    import inspect

    if "worker" in inspect.signature(fn).parameters:
        kwargs["worker"] = worker
    return fn(*args, **kwargs)


def test_a_stale_runner_cannot_release_a_successors_live_claim(store):
    """w1 claims a schedule and stalls past its lease; w2 legitimately takes
    over via expiry; w1's long-delayed completion report must not release
    w2's LIVE claim — otherwise a third replica can claim and the window
    fires again (and w1's stale next_run_at/status overwrite w2's window)."""
    name = "stale-runner"
    store.upsert_schedule(
        ScheduleInfo(name=name, trigger="cron", cron="* * * * *",
                     next_run_at=A_LONG_TIME_AGO)
    )
    assert store.claim_schedule(name, "w1", lease_seconds=-1), "w1 claims, then stalls"
    assert store.claim_schedule(name, "w2"), "w2 takes over the expired lease"

    # w1 wakes up and reports the run it finished an epoch ago:
    _call_with_worker_if_supported(
        store.record_schedule_run, name, "succeeded", worker="w1"
    )

    with store._conn() as c:
        row = c.execute(
            "SELECT claimed_by FROM schedules WHERE name = ?", (name,)
        ).fetchone()
    assert row["claimed_by"] == "w2", (
        f"a stale runner's completion released the successor's live claim "
        f"(claimed_by = {row['claimed_by']!r}, expected 'w2')"
    )
    assert not store.claim_schedule(name, "w3"), (
        "w3 claimed a schedule w2 is actively running — the stale release "
        "re-opened the window"
    )


def test_release_build_cannot_clear_a_lease_it_no_longer_owns(store):
    """Same shape on the build lease: w1's lease expires mid-build, w2 takes
    over, w1's tail-end release must not clear w2's live lease — otherwise a
    third replica claims the build and the transforms execute again."""
    build = store.create_build(["target"])
    assert store.claim_build(build.id, "w1", lease_seconds=-1), "w1 claims, stalls"
    assert store.claim_build(build.id, "w2"), "w2 takes over the expired lease"

    _call_with_worker_if_supported(store.release_build, build.id, worker="w1")

    assert not store.claim_build(build.id, "w3"), (
        "w3 claimed a build w2 is actively executing — w1's stale release "
        "cleared a lease it no longer owned"
    )


# -- catch_up racing live commits ----------------------------------------------

ONTOLOGY = """
object_types:
  - api_name: city
    backing_dataset: cities
    primary_key: name
    title_property: name
    properties:
      name: {type: string}
      realm: {type: string}
"""


def _cities(n: int = 6) -> pa.Table:
    return pa.table({
        "name": [f"city-{i}" for i in range(n)],
        "realm": ["valinor"] * n,
    })


@pytest.fixture()
def svc(store, tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="conc")
    catalog = DatasetCatalog(ws, store)
    catalog.write("cities", _cities())
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    return OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))


def test_catch_up_racing_commits_never_skips_an_edit(svc):
    """``catch_up`` (ontology/service.py:613) replays what the materialization
    owes, while a writer keeps appending to the log. The invariant is the
    design's one forbidden direction: the watermark must never claim an edit
    the rows don't hold.

    One writer appends log-only edits (the path that leaves the store behind);
    two catch-up threads replay concurrently — racing the writer *and* each
    other. During the race, every observed watermark must be monotone and
    never exceed the committed log; afterwards, one final catch_up must land
    exactly level with the log with every edit's effect present — no skips,
    no double-application artifacts.
    """
    store = svc.store
    n_edits = rounds(40)
    n_pks = 6

    svc.reindex("city")
    assert store.object_index_state("city") is not None

    done = threading.Event()
    violations: list[str] = []
    errors: list[str] = []
    barrier = threading.Barrier(3, timeout=30)

    def writer() -> None:
        try:
            barrier.wait()
            for k in range(n_edits):
                store.add_object_edit(ObjectEdit(
                    id=uuid.uuid4().hex, object_type="city",
                    pk_value=f"city-{k % n_pks}", kind=EditKind.update,
                    payload={"realm": f"realm-{k + 1}"}, actor="writer",
                ))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"writer: {exc!r}")
        finally:
            done.set()

    def catcher(cid: int) -> None:
        try:
            barrier.wait()
            last = -1
            while True:
                finished = done.is_set()
                svc.catch_up("city")
                state = store.object_index_state("city")
                if state is not None:
                    wm = int(state["applied_seq"])
                    if wm < last:
                        violations.append(
                            f"catcher {cid}: watermark went backwards {last} -> {wm}"
                        )
                    last = wm
                    # max_edit_seq only grows and is read AFTER the state, so
                    # any watermark above it certified edits that did not
                    # exist when the watermark was observed.
                    ceiling = store.max_edit_seq("city")
                    if wm > ceiling:
                        violations.append(
                            f"catcher {cid}: watermark {wm} ahead of the log "
                            f"(max committed seq {ceiling})"
                        )
                if finished:
                    break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"catcher {cid}: {exc!r}")

    threads = [threading.Thread(target=writer, daemon=True)] + [
        threading.Thread(target=catcher, args=(i,), daemon=True) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not [t for t in threads if t.is_alive()], "threads never finished"
    assert not errors, errors
    assert not violations, violations

    svc.catch_up("city")
    state = store.object_index_state("city")
    assert int(state["applied_seq"]) == store.max_edit_seq("city") == n_edits, (
        "after a final catch_up the watermark is exactly level with the log"
    )

    # Every edit at or below the watermark must be materialized: each pk's row
    # holds the payload and position of its last edit in the log.
    last_by_pk: dict[str, ObjectEdit] = {}
    for edit in store.list_object_edits("city"):
        last_by_pk[edit.pk_value] = edit
    rows, _total, _state = store.search_object_index("city", limit=100)
    by_pk = {r["pk"]: r for r in rows}
    for pk, edit in last_by_pk.items():
        row = by_pk.get(pk)
        assert row is not None, f"{pk}: edited object missing from the index"
        assert row["props"]["realm"] == edit.payload["realm"], (
            f"{pk}: edit {edit.edit_seq} at or below the watermark was skipped "
            f"(index holds {row['props']['realm']!r}, log says "
            f"{edit.payload['realm']!r})"
        )
        assert int(row["applied_seq"]) == edit.edit_seq, (
            f"{pk}: row position {row['applied_seq']} != last edit {edit.edit_seq}"
        )
