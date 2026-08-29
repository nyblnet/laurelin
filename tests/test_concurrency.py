"""Store-level concurrency invariants: no-lost-update and no-torn-read.

Every test here asserts an *invariant* — a count, a set-equality, a
containment — never a timing. Races are forced with a barrier (all writers
released at the same instant) plus a seeded pre-existing row in the contended
scope, which makes the writers' DELETEs collide on the same row and turns the
overlap deterministic: on Postgres the loser blocks on the winner's row lock
until commit, then proceeds over a stale snapshot.

Both backends run, because they serialize differently: SQLite's single-writer
WAL lock makes DELETE+INSERT replaces effectively serial, while Postgres READ
COMMITTED lets two writers interleave for real. A race that cannot happen on
one absolutely can on the other.

Measured before writing these tests (2 barrier-synchronized writers through
the real ``MetadataStore.set_grants_for_dataset`` over a seeded row):
postgres 50/50 rounds anomalous (the union of both writers' lists),
sqlite 0/50. At a pessimistic per-round hit rate of 0.5 the odds that 20
rounds all miss are 2^-20 ≈ 10^-6; the measured rate is ≈ 1.0.
"""

from __future__ import annotations

import threading
import time

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.approvals import ChangeTicket as _ChangeTicket
from laurelin.core.config import Workspace
from laurelin.core.models import utcnow_iso
from tests.concurrency_harness import (
    BACKENDS,
    open_store,
    rounds,
    run_racers,
)

_TICKET = _ChangeTicket(kind="local", actor="test")

soak = pytest.mark.soak


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    yield from open_store(request.param, tmp_path)


@pytest.fixture(params=BACKENDS)
def catalog(request, tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="conc")
    for s in open_store(request.param, tmp_path):
        yield DatasetCatalog(ws, s)


# ---------------------------------------------------------------------------
# D1 — no-lost-update: every security list is replaced whole-for-whole.
#
# All six setters share one shape (db.py:2447, 2493, 2543, 2650, 2676): an
# unlocked DELETE of the scope followed by INSERTs, in one transaction, with
# nothing serializing two replacers of the same scope. The invariant is that
# two concurrent replaces end as exactly ONE writer's list — last-writer-wins
# is fine, a merge is not: for grants and clearances the union of two lists is
# *wider access than either administrator wrote*.
# ---------------------------------------------------------------------------


def _grant(subject: str) -> dict:
    return {"subject_kind": "user", "subject": subject, "can_view": True, "can_edit": False}


def _seed_dataset_grants(s, scope):
    s.set_grants_for_dataset(scope, [_grant("seed")], ticket=_TICKET)


def _seed_type_grants(s, scope):
    s.set_grants_for_type(scope, [_grant("seed")], ticket=_TICKET)


def _seed_group(s, scope):
    s.create_group(scope, utcnow_iso())
    s.set_group_members(scope, ["seed"], ticket=_TICKET)


def _seed_clearances(s, scope):
    s.set_clearances(scope, ["seed"], ticket=_TICKET)


def _seed_markings(s, scope):
    s.set_explicit_markings(scope, ["seed"], ticket=_TICKET)


REPLACE_CASES = {
    # name: (seed(s, scope), write(s, scope, subject), read(s, scope) -> [subjects])
    "dataset_grants": (
        _seed_dataset_grants,
        lambda s, scope, subj: s.set_grants_for_dataset(scope, [_grant(subj)], ticket=_TICKET),
        lambda s, scope: sorted(g["subject"] for g in s.grants_for_dataset(scope)),
    ),
    "ontology_grants": (
        _seed_type_grants,
        lambda s, scope, subj: s.set_grants_for_type(scope, [_grant(subj)], ticket=_TICKET),
        lambda s, scope: sorted(g["subject"] for g in s.grants_for_type(scope)),
    ),
    "group_members": (
        _seed_group,
        lambda s, scope, subj: s.set_group_members(scope, [subj], ticket=_TICKET),
        lambda s, scope: sorted(
            m
            for g in s.list_groups()
            if g["name"] == scope
            for m in g["members"]
        ),
    ),
    "clearances": (
        _seed_clearances,
        lambda s, scope, subj: s.set_clearances(scope, [subj], ticket=_TICKET),
        lambda s, scope: sorted(s.get_clearances(scope)),
    ),
    "explicit_markings": (
        _seed_markings,
        lambda s, scope, subj: s.set_explicit_markings(scope, [subj], ticket=_TICKET),
        lambda s, scope: sorted(s.get_explicit_markings(scope)),
    ),
}


@soak
@pytest.mark.parametrize("case", sorted(REPLACE_CASES))
def test_concurrent_grant_replaces_end_as_one_writers_list_not_the_union(store, case):
    seed, write, read = REPLACE_CASES[case]
    subjects = ["alice", "bob"]
    anomalies = []
    for r in range(rounds(20)):
        scope = f"lostupd_{case}_{r}"
        # The seeded row is what makes the overlap deterministic: the loser's
        # DELETE blocks on the winner's lock on this row, resumes after
        # commit, and (on Postgres READ COMMITTED) never sees the winner's
        # freshly inserted row — so it deletes nothing and merges.
        seed(store, scope)
        _, errors = run_racers(2, lambda i: write(store, scope, subjects[i]))
        observed = read(store, scope)
        if observed not in (["alice"], ["bob"]) or any(errors):
            anomalies.append((r, observed, [repr(e) for e in errors if e]))
    assert not anomalies, (
        f"{case}: a concurrent whole-list replace must end as exactly one "
        f"writer's list (['alice'] or ['bob']). Lost-update anomalies in "
        f"{len(anomalies)}/{rounds(20)} rounds (round, final_list, errors): "
        f"{anomalies[:5]}"
    )


@soak
def test_concurrent_recomputes_of_effective_markings_neither_crash_nor_drop(store):
    """Two replicas recomputing effective markings at once (the builder calls
    recompute_all_markings on EVERY build, builder.py:349, so two replicas
    building concurrently do exactly this) must leave the effective set equal
    to the explicit set — and must not crash: each recompute's DELETE+INSERT
    of the same (dataset, marking, inherited=1) rows races the other's."""
    store.upsert_dataset("ds_rc", "")
    store.create_marking("secret")
    store.set_explicit_markings("ds_rc", ["secret"], ticket=_TICKET)
    store.recompute_all_markings()  # baseline: effective == {secret}
    anomalies = []
    n_rounds = rounds(20)
    for r in range(n_rounds):
        _, errors = run_racers(2, lambda i: store.recompute_all_markings())
        effective = sorted(store.get_effective_markings("ds_rc"))
        if effective != ["secret"] or any(errors):
            anomalies.append((r, effective, [repr(e) for e in errors if e]))
            # Repair before the next round so every round starts from the
            # same state and each anomaly is independently produced.
            store.recompute_all_markings()
    assert not anomalies, (
        "concurrent recompute_all_markings corrupted or crashed: "
        f"{len(anomalies)}/{n_rounds} rounds anomalous "
        f"(round, effective_markings_after, errors): {anomalies[:5]}"
    )


# ---------------------------------------------------------------------------
# D4 — no-lost-update: racing version registrations all survive.
# ---------------------------------------------------------------------------


@soak
def test_n_racing_version_registrations_all_survive_with_distinct_versions(catalog):
    """N writers racing ``catalog.write`` on one dataset: every write survives
    with its own version number, the numbers are a contiguous run, and no
    writer's rows are silently lost. The (dataset, version) primary key plus
    the take-the-next-number retry in _commit_version (catalog.py:232-255) is
    the mechanism under test."""
    n = 8
    table = pa.table({"id": [1, 2, 3]})
    total = 0
    for r in range(rounds(10)):
        results, errors = run_racers(
            n, lambda i: catalog.write("races", table, source=f"racer-{r}-{i}")
        )
        assert not any(errors), f"round {r}: writers raised: {[e for e in errors if e]}"
        total += n
        infos = catalog.store.list_versions("races")
        versions = [v.version for v in infos]
        assert versions == list(range(1, total + 1)), (
            f"round {r}: versions must be the contiguous run 1..{total}, got {versions}"
        )
        minted = sorted(res.version for res in results)
        assert len(set(minted)) == n, (
            f"round {r}: two writers were handed the same version: {minted}"
        )
        sources = [v.source for v in infos]
        assert len(sources) == len(set(sources)) and all(
            f"racer-{r}-{i}" in sources for i in range(n)
        ), f"round {r}: a writer's registration was lost: {sources}"


# ---------------------------------------------------------------------------
# E1 — no-torn-read: a policy swap is all-or-nothing.
# ---------------------------------------------------------------------------

_POLICY_A = {
    "row_policy": {
        "column": "tenant",
        "rules": [
            {"subject_kind": "user", "subject": "alice", "values": ["t1", "t2"]}
        ],
    },
    "column_masks": [],
}
_POLICY_B = {
    "row_policy": {
        "column": "region",
        "rules": [
            {"subject_kind": "user", "subject": "bob", "values": ["r1", "r2", "r3"]}
        ],
    },
    "column_masks": [{"column": "ssn", "mode": "null", "exempt": []}],
}


@soak
def test_a_policy_swap_is_never_seen_torn(store):
    """A reader concurrent with ``set_dataset_policy`` must observe policy A
    exactly or policy B exactly — never None, never a hybrid. The policy is a
    single-row JSON upsert (db.py:2739), so a blob cannot tear; this test
    exists because that claim has to be *observed*, not deduced."""
    store.set_dataset_policy("pol_ds", _POLICY_A, ticket=_TICKET)
    iters = max(200, rounds(200))
    violations = []
    done = threading.Event()

    def writer():
        for i in range(iters):
            store.set_dataset_policy("pol_ds", _POLICY_B if i % 2 else _POLICY_A, ticket=_TICKET)
        done.set()

    t = threading.Thread(target=writer, name="policy-writer", daemon=True)
    t.start()
    reads = 0
    while not done.is_set() or reads < iters:
        obs = store.get_dataset_policy("pol_ds")
        reads += 1
        if obs != _POLICY_A and obs != _POLICY_B:
            violations.append(obs)
        if done.is_set() and reads >= iters:
            break
    t.join(90)
    assert not t.is_alive(), "policy writer never finished — a held lock, not slowness"
    assert not violations, (
        f"{len(violations)}/{reads} reads observed a policy that is neither A "
        f"nor B (first: {violations[0]!r}) — a torn or vanished policy"
    )


# ---------------------------------------------------------------------------
# E2 — no-torn-read: a grant list is replaced atomically as seen by readers.
# ---------------------------------------------------------------------------


@soak
def test_a_reader_never_observes_a_half_replaced_grant_list(store):
    """While two writers replace a dataset's grant list, a looping reader must
    only ever see complete lists: the seed block whole, writer A's block
    whole, writer B's block whole, or a union of complete blocks — never an
    empty list (a moment of zero grants would deny everyone... or on an
    allowlist-by-absence model widen), and never a *partial* block, which
    would be a torn read of somebody's half-inserted replace.

    The union END-state is D1's lost-update finding, not this test's: here a
    union of complete blocks is accepted so this test stays red/green on the
    torn-read property alone."""
    blocks = {
        "seed": {"seed1", "seed2", "seed3"},
        "alice": {"alice1", "alice2", "alice3"},
        "bob": {"bob1", "bob2", "bob3"},
    }
    writers = ["alice", "bob"]
    violations = []
    n_rounds = rounds(20)
    for r in range(n_rounds):
        scope = f"torn_{r}"
        store.set_grants_for_dataset(scope, [_grant(s) for s in sorted(blocks["seed"])], ticket=_TICKET)
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                obs = {g["subject"] for g in store.grants_for_dataset(scope)}
                if not obs:
                    violations.append((r, "empty", obs))
                    continue
                for name, block in blocks.items():
                    part = obs & block
                    if part and part != block:
                        violations.append((r, f"partial {name} block", sorted(obs)))
                if not obs <= (blocks["seed"] | blocks["alice"] | blocks["bob"]):
                    violations.append((r, "foreign subjects", sorted(obs)))

        rt = threading.Thread(target=reader, name="grant-reader", daemon=True)
        rt.start()
        # Tiny stagger maximizes reader overlap with the replace transactions.
        time.sleep(0.005)
        _, errors = run_racers(
            2,
            lambda i: store.set_grants_for_dataset(
                scope, [_grant(s) for s in sorted(blocks[writers[i]])]
            , ticket=_TICKET),
        )
        stop.set()
        rt.join(90)
        assert not rt.is_alive(), "grant reader is stuck — a held lock, not slowness"
        assert not any(errors), f"round {r}: writers raised: {[e for e in errors if e]}"
    assert not violations, (
        f"torn grant-list reads in {len(violations)} observations across "
        f"{n_rounds} rounds (round, kind, observed): {violations[:5]}"
    )
