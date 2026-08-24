"""End-to-end concurrency invariants on the permission path.

The security-critical property under test: a viewer must NEVER observe a
half-applied policy/grant/marking change that WIDENS access. The enforcement
point is ``PermissionService._has_clearance`` (permissions.py:388-400), which
reads only the *effective* (inherited=1) marking rows and treats an empty
effective set as "no clearance needed" — so any interleaving that leaves the
effective set momentarily or durably behind the explicit set is an access
widening, not a cosmetic staleness.

Interleavings are forced deterministically with pause hooks that park a thread
at an exact point BETWEEN store calls (each MetadataStore method opens and
commits its own connection, so nothing is parked inside a held transaction).
No test here asserts a timing.
"""

from __future__ import annotations

import itertools
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

import laurelin.core.db as db_module
from laurelin.api import create_app
from laurelin.core.auth import AuthService, hash_password
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import LineageEdge, Role, User, utcnow_iso
from laurelin.core.permissions import PermissionService
from tests.concurrency_harness import (
    BACKENDS,
    PG_URL,
    open_store,
    pause_hook,
    rounds,
)


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    yield from open_store(request.param, tmp_path)


def _viewer(name: str) -> User:
    return User(id=f"u-{name}", username=name, role=Role.viewer)


# ---------------------------------------------------------------------------
# D2 — a recompute racing a marking change never drops a committed marking.
# ---------------------------------------------------------------------------


def test_recompute_racing_a_marking_change_never_drops_a_marking(store):
    """``recompute_all_markings`` reads datasets, lineage and explicit markings
    through MANY separate transactions and then writes the effective sets
    blind-last (db.py:2686-2698). A recompute that read *before* a marking was
    set and writes *after* that marking's own recompute finished will replace
    the correct effective sets with stale ones. Because ``_has_clearance``
    treats an empty effective set as cleared, the stale write does not merely
    lag — it WIDENS: an uncleared viewer can read a dataset that a committed,
    acknowledged classification says they must not. This exact interleaving
    happens whenever a build (builder.py:349 recomputes on every build)
    overlaps an admin's PUT /markings.

    Deterministic: R2 is parked between its last read (propagate_markings is
    called after all reads complete) and its write transaction; the marking
    change plus its own recompute run to completion in the gap; R2 is then
    released to lay down its stale effective sets."""
    store.upsert_dataset("src", "")
    store.upsert_dataset("derived", "")
    store.replace_lineage_for_transform(
        "t1",
        [LineageEdge(upstream_dataset="src", downstream_dataset="derived", transform_name="t1")],
    )
    store.create_marking("secret")
    perms = PermissionService(store)
    bob = _viewer("bob")  # no clearances

    anomalies = []
    n_rounds = rounds(3)
    for r in range(n_rounds):
        # Reset to the pre-change world: no markings anywhere, bob can view.
        store.set_explicit_markings("src", [])
        store.recompute_all_markings()
        assert perms.can_view_dataset(bob, "src"), "baseline: unmarked dataset is viewable"

        with pause_hook(
            db_module, "propagate_markings", only_thread_named="r2-recompute"
        ) as gate:
            r2 = threading.Thread(
                target=store.recompute_all_markings, name="r2-recompute", daemon=True
            )
            r2.start()
            gate.wait_reached()  # R2 has read everything, has written nothing

            # The committed, acknowledged classification change:
            store.set_explicit_markings("src", ["secret"])
            store.recompute_all_markings()
            assert set(store.get_effective_markings("src")) == {"secret"}
            assert "secret" in set(store.get_effective_markings("derived"))
            assert not perms.can_view_dataset(bob, "src")
            assert not perms.can_view_dataset(bob, "derived")

            gate.open()  # R2 now writes effective sets computed pre-change
            r2.join(90)
            assert not r2.is_alive(), "parked recompute never finished"

        for ds in ("src", "derived"):
            effective = set(store.get_effective_markings(ds))
            expected = {"secret"}  # explicit on src, lineage-inherited on derived
            if not (expected <= effective):
                anomalies.append((r, ds, sorted(effective), "marking dropped"))
            if perms.can_view_dataset(bob, ds):
                anomalies.append((r, ds, sorted(effective), "UNCLEARED VIEWER CAN READ"))
    assert not anomalies, (
        "a recompute that raced a marking change dropped the committed marking "
        "from the effective set and re-opened the dataset to an uncleared "
        f"viewer; {len(anomalies)} anomalies across {n_rounds} rounds "
        f"(round, dataset, effective_after, what): {anomalies}"
    )


# ---------------------------------------------------------------------------
# E3 — a classification marking denies from the moment its write returns.
# ---------------------------------------------------------------------------


def test_a_marking_denies_the_moment_its_write_returns(store):
    """PUT /datasets/{name}/markings is two store transactions in sequence
    (routes.py:2854-2855): ``set_explicit_markings`` then
    ``recompute_all_markings``. Enforcement reads only the inherited=1 rows
    (db.py:2647 via get_effective_markings), so in the gap between the two
    statements the marking is committed and visible to every admin screen —
    and enforced against nobody. This is the route's own interleaving: any
    other request thread can read between its two statements, no thread
    machinery needed to reproduce it faithfully.

    The invariant: once ``set_explicit_markings`` has RETURNED (the write is
    committed and acknowledged), an uncleared viewer is denied."""
    store.upsert_dataset("gap_ds", "")
    store.create_marking("secret")
    store.recompute_all_markings()
    perms = PermissionService(store)
    bob = _viewer("bob")
    assert perms.can_view_dataset(bob, "gap_ds"), "baseline: unmarked dataset is viewable"

    # Statement one of the route, committed and returned:
    store.set_explicit_markings("gap_ds", ["secret"])

    denied_in_gap = not perms.can_view_dataset(bob, "gap_ds")

    # Statement two; afterwards denial is undisputed (sanity, not the test):
    store.recompute_all_markings()
    assert not perms.can_view_dataset(bob, "gap_ds")

    assert denied_in_gap, (
        "between set_explicit_markings returning and recompute_all_markings "
        "running, the committed marking 'secret' on 'gap_ds' denied nobody: "
        "an uncleared viewer read a dataset the acknowledged classification "
        "says they must not (enforcement reads only inherited=1 rows)"
    )


# ---------------------------------------------------------------------------
# D3 — concurrent SCIM PATCHes never lose a member change.
# ---------------------------------------------------------------------------

SCIM_TOKEN = "conc-scim-token"
H = {"Authorization": f"Bearer {SCIM_TOKEN}", "Content-Type": "application/scim+json"}


@pytest.fixture(params=BACKENDS)
def scim_app(request, tmp_path, monkeypatch):
    monkeypatch.setenv("LAURELIN_SCIM_TOKEN", SCIM_TOKEN)
    ws = Workspace.init(tmp_path / "ws", name="conc")
    app = create_app(ws)
    if request.param == "postgres":
        if not PG_URL:
            pytest.skip("set LAURELIN_TEST_POSTGRES=<url> to run the Postgres half")
        from laurelin.core.backend import PostgresBackend

        schema = f"race_{uuid.uuid4().hex[:10]}"
        pg_store = MetadataStore(PG_URL, schema=schema)
        # Identity for SCIM is app.state.store in single-workspace mode
        # (context.identity_store); point it at the throwaway Postgres schema.
        app.state.store = pg_store
        app.state.auth = AuthService(pg_store)
        app.state.auth.create_first_admin("root", "trustno1!")
        try:
            yield app
        finally:
            with pg_store._conn() as c:
                c.execute(
                    f"DROP SCHEMA IF EXISTS {PostgresBackend.quote_ident(schema)} CASCADE"
                )
        return
    app.state.auth.create_first_admin("root", "trustno1!")
    yield app


def _patch_op(op: str, *usernames: str, path: str | None = None) -> dict:
    body: dict = {"op": op, "value": [{"value": u} for u in usernames]}
    if path:
        body["path"] = path
    return body


def _run_scim_pair(app, group: str, op_a: dict, op_b: dict) -> tuple[list, set]:
    """Drive two PATCH /Groups/{group} requests with their read-modify-writes
    forced to overlap — the interleaving two IdP calls (or an IdP retry racing
    itself) produce whenever their requests overlap.

    Two shapes of the route exist and each gets its faithful forcing:

    * Fixed shape: the route merges through the store's atomic
      ``update_group_members``. Both requests are held at a barrier
      immediately BEFORE that call, so both hit the store's scope lock at the
      same instant and the database — not luck — must serialize the merges.
    * Original shape (kept so reverting the fix makes this test fail for the
      race it documents): the route reads members via store.list_groups
      (scim_routes.py:304) and writes via set_group_members (:320); the hook
      holds the first two list_groups calls at a barrier, which are
      necessarily the two requests' pre-write reads, forcing both reads
      before either write."""
    store = app.state.store
    read_barrier = threading.Barrier(2, timeout=30)
    calls = itertools.count()

    def _gate() -> None:
        # Hold the first two arrivals at one barrier; later calls (the
        # response re-reads) pass straight through.
        if next(calls) < 2:
            read_barrier.wait()

    real_update = getattr(store, "update_group_members", None)
    real_list = store.list_groups

    def hooked_list():
        out = real_list()
        _gate()
        return out

    store.list_groups = hooked_list
    if real_update is not None:
        def hooked_update(name, merge):
            _gate()
            return real_update(name, merge)

        store.update_group_members = hooked_update
    responses = [None, None]
    errors = []

    def patch(i: int, op: dict) -> None:
        try:
            client = TestClient(app)
            responses[i] = client.patch(
                f"/api/v1/scim/v2/Groups/{group}",
                headers=H,
                json={"Operations": [op]},
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        t1 = threading.Thread(target=patch, args=(0, op_a), daemon=True)
        t2 = threading.Thread(target=patch, args=(1, op_b), daemon=True)
        t1.start(), t2.start()
        t1.join(90), t2.join(90)
        assert not t1.is_alive() and not t2.is_alive(), "a PATCH request is stuck"
    finally:
        store.list_groups = real_list
        if real_update is not None:
            store.update_group_members = real_update
    assert not errors, f"PATCH raised: {errors}"
    assert all(r is not None and r.status_code == 200 for r in responses), (
        f"PATCH failed: {[(r.status_code, r.text) for r in responses if r is not None]}"
    )
    final = {
        m
        for g in store.list_groups()
        if g["name"] == group
        for m in g["members"]
    }
    return responses, final


def test_concurrent_scim_patches_never_lose_a_member_change(scim_app):
    """Two overlapping SCIM PATCHes both succeed (200) — so the IdP believes
    both changes landed — and both must actually be in effect: PATCH is a
    route-level read-modify-write across two store transactions
    (scim_routes.py:295-320), so nothing in the database arbitrates the merge.
    A lost 'add' quietly under-provisions; a lost 'remove' quietly KEEPS a
    deprovisioned member in a group that may carry grants — access widening
    reported to the IdP as success."""
    store = scim_app.state.store
    for u in ("u1", "u2"):
        if store.get_user(u) is None:
            store.create_user(_viewer(u), hash_password(uuid.uuid4().hex))

    anomalies = []
    n_rounds = rounds(3)
    for r in range(n_rounds):
        # Case 1: add u1 || add u2 on an empty group -> both adds survive.
        g1 = f"addrace{r}"
        store.create_group(g1, utcnow_iso())
        _, final = _run_scim_pair(
            scim_app, g1, _patch_op("add", "u1"), _patch_op("add", "u2")
        )
        if final != {"u1", "u2"}:
            anomalies.append((r, "add||add", sorted(final), "an add was lost"))

        # Case 2: remove u1 || add u2 on {u1} -> u1 gone AND u2 present.
        g2 = f"rmrace{r}"
        store.create_group(g2, utcnow_iso())
        store.set_group_members(g2, ["u1"])
        _, final = _run_scim_pair(
            scim_app, g2, _patch_op("remove", "u1", path="members[...]"),
            _patch_op("add", "u2"),
        )
        if final != {"u2"}:
            what = (
                "the REMOVE was lost — deprovisioned member kept"
                if "u1" in final
                else "the add was lost"
            )
            anomalies.append((r, "remove||add", sorted(final), what))
    assert not anomalies, (
        "concurrent SCIM PATCHes lost member changes while reporting 200 to "
        f"the IdP; {len(anomalies)} anomalies across {n_rounds} rounds "
        f"(round, case, final_members, what): {anomalies}"
    )
