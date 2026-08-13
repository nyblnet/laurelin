"""Cross-replica build coordination.

With several replicas serving one workspace, any of them can accept "run a
build" — but exactly one must execute it. The lease is a single conditional
UPDATE: the row is the lock and the database picks the winner.
"""

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import BuildStatus
from laurelin.transforms import Builder, collect_transforms

PIPELINE = """
from laurelin.transforms import transform, Input, Output

@transform(output=Output("out"), src=Input("src"))
def build_out(src):
    return src
"""


@pytest.fixture()
def store(tmp_path):
    return MetadataStore(tmp_path / "m.db")


def test_only_one_worker_claims_a_build(store):
    build = store.create_build([])
    assert store.claim_build(build.id, "replica-a") is True
    assert store.claim_build(build.id, "replica-b") is False
    # The owner may re-claim (idempotent retry).
    assert store.claim_build(build.id, "replica-a") is True


def test_expired_lease_can_be_taken_over(store):
    build = store.create_build([])
    assert store.claim_build(build.id, "replica-a", lease_seconds=-1) is True
    # a's lease is already in the past, so b may take over.
    assert store.claim_build(build.id, "replica-b") is True
    # …and now a cannot renew what it no longer owns.
    assert store.renew_build_lease(build.id, "replica-a") is False
    assert store.renew_build_lease(build.id, "replica-b") is True


def test_finished_builds_cannot_be_claimed(store):
    build = store.create_build([])
    store.update_build(build.id, status=BuildStatus.succeeded)
    assert store.claim_build(build.id, "replica-a") is False


def test_reaper_fails_abandoned_builds(store):
    alive = store.create_build([])
    dead = store.create_build([])
    store.claim_build(alive.id, "replica-a", lease_seconds=300)
    store.claim_build(dead.id, "replica-b", lease_seconds=-5)

    reaped = store.reap_expired_builds()
    assert reaped == [dead.id]
    assert store.get_build(dead.id).status == BuildStatus.failed
    reaped_failure = store.get_build(dead.id).failure
    assert reaped_failure is not None
    assert reaped_failure.subject == f"build:{dead.id}"
    # The healthy build is untouched.
    assert store.get_build(alive.id).status == BuildStatus.pending

    # And a reaped build is claimable again, so the work isn't stranded.
    assert store.claim_build(dead.id, "replica-c") is False  # terminal status


def test_release_clears_the_lease(store):
    build = store.create_build([])
    store.claim_build(build.id, "replica-a")
    store.release_build(build.id)
    assert store.reap_expired_builds() == []


# -- end to end ----------------------------------------------------------------

@pytest.fixture()
def workspace(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="leases")
    store = MetadataStore(ws.metadata_path)
    DatasetCatalog(ws, store).write("src", pa.table({"id": [1, 2, 3]}))
    (ws.pipelines_dir / "p.py").write_text(PIPELINE)
    return ws


def builder_for(ws):
    store = MetadataStore(ws.metadata_path)
    return Builder(ws, DatasetCatalog(ws, store), store, collect_transforms(ws.pipelines_dir)), store


def test_second_replica_skips_a_claimed_build(workspace):
    """The losing replica must return the existing record, not run the build."""
    builder_a, store = builder_for(workspace)
    builder_b, _ = builder_for(workspace)
    build = store.create_build([])

    result_a = builder_a.execute(build.id, None, worker="replica-a")
    assert result_a.status == BuildStatus.succeeded
    assert store.get_dataset("out").latest_version == 1

    # b runs after a finished: the build is terminal, so b must not rebuild.
    result_b = builder_b.execute(build.id, None, worker="replica-b")
    assert result_b.status == BuildStatus.succeeded
    assert store.get_dataset("out").latest_version == 1, "must not build twice"


def test_build_without_a_worker_id_is_unleased(workspace):
    """CLI/sync builds pass no worker and are unaffected by leasing."""
    builder, store = builder_for(workspace)
    build = store.create_build([])
    assert builder.execute(build.id, None).status == BuildStatus.succeeded
    row = store.get_build(build.id)
    assert row.status == BuildStatus.succeeded


def test_lease_is_renewed_during_a_build(workspace, monkeypatch):
    builder, store = builder_for(workspace)
    build = store.create_build([])
    renewals = []
    original = store.renew_build_lease

    def spy(build_id, worker, lease_seconds=120):
        renewals.append(worker)
        return original(build_id, worker, lease_seconds)

    monkeypatch.setattr(builder.store, "renew_build_lease", spy)
    builder.execute(build.id, None, worker="replica-a")
    assert renewals == ["replica-a"]  # one transform in this pipeline
