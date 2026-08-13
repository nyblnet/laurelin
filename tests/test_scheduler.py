"""Scheduling — what turns a platform you drive into one that runs.

The properties that matter: a due schedule fires exactly once even with
several replicas polling; a failure doesn't disable the pipeline; an overdue
schedule fires once rather than once per missed window; and an upstream
trigger follows its dataset rather than a clock.
"""

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core import scheduler
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import ScheduleInfo, utcnow_iso

PAST = "2000-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"


@pytest.fixture()
def store(tmp_path):
    return MetadataStore(tmp_path / "m.db")


def cron_schedule(name="nightly", cron="0 2 * * *", **kw):
    return ScheduleInfo(name=name, trigger="cron", cron=cron,
                        action="build", **kw)


# -- validation -----------------------------------------------------------------

@pytest.mark.parametrize("info, msg", [
    (ScheduleInfo(name="a", trigger="whenever"), "Unknown trigger"),
    (ScheduleInfo(name="a", trigger="cron", cron=""), "needs a cron expression"),
    (ScheduleInfo(name="a", trigger="cron", cron="not a cron"), "Invalid cron"),
    (ScheduleInfo(name="a", trigger="upstream"), "needs an upstream_dataset"),
    (ScheduleInfo(name="a", cron="0 2 * * *", action="explode"), "Unknown action"),
    (ScheduleInfo(name="a", cron="0 2 * * *", action="sync"), "needs a source"),
])
def test_validation_rejects_unrunnable_schedules(info, msg):
    with pytest.raises(scheduler.ScheduleError, match=msg):
        scheduler.validate(info)


def test_next_fire_computes_the_following_window():
    from datetime import datetime, timezone

    base = datetime(2026, 7, 26, 3, 30, tzinfo=timezone.utc)
    assert scheduler.next_fire("0 2 * * *", base).startswith("2026-07-27T02:00")
    assert scheduler.next_fire("*/15 * * * *", base).startswith("2026-07-26T03:45")


# -- exactly-once across replicas -------------------------------------------------

def test_only_one_replica_fires_a_due_schedule(store):
    store.upsert_schedule(cron_schedule(next_run_at=PAST))
    fired = []

    def make(worker):
        return scheduler.Scheduler(
            open_stores=lambda: [("ws", store, lambda s: fired.append(worker) or "b1")],
            worker_id=worker,
        )

    a, b = make("replica-a"), make("replica-b")
    assert a.tick() == ["nightly"]
    assert b.tick() == [], "the second replica must not re-fire it"
    assert fired == ["replica-a"]


def test_claim_is_released_so_the_next_window_can_fire(store):
    store.upsert_schedule(cron_schedule(next_run_at=PAST))
    runs = []
    sched = scheduler.Scheduler(
        open_stores=lambda: [("ws", store, lambda s: runs.append(1) or "b")],
        worker_id="a",
    )
    assert sched.tick() == ["nightly"]

    # After firing, next_run_at moved into the future: not due any more.
    assert sched.tick() == []
    info = store.get_schedule("nightly")
    assert info.next_run_at > utcnow_iso()
    assert info.last_status == "succeeded"

    # Force it due again — it fires, so the claim was genuinely released.
    info.next_run_at = PAST
    store.upsert_schedule(info)
    assert sched.tick() == ["nightly"]
    assert len(runs) == 2


def test_an_overdue_schedule_fires_once_not_once_per_missed_window(store):
    """A restart after downtime must not unleash a catch-up storm."""
    store.upsert_schedule(cron_schedule(cron="*/5 * * * *", next_run_at=PAST))
    runs = []
    sched = scheduler.Scheduler(
        open_stores=lambda: [("ws", store, lambda s: runs.append(1) or "b")],
        worker_id="a",
    )
    sched.tick()
    sched.tick()
    assert len(runs) == 1


# -- failure handling -------------------------------------------------------------

def test_a_failing_action_is_recorded_and_still_reschedules(store):
    """One bad night must not silently disable the pipeline."""
    store.upsert_schedule(cron_schedule(next_run_at=PAST))

    def boom(_):
        raise RuntimeError("transform exploded")

    sched = scheduler.Scheduler(
        open_stores=lambda: [("ws", store, boom)], worker_id="a"
    )
    assert sched.tick() == ["nightly"]

    info = store.get_schedule("nightly")
    assert info.last_status == "failed"
    # R1: `_redacted_failure` used to load the schedule's source config and
    # substring-replace that source's password out of the driver's sentence
    # before storing it — a correct implementation of a doomed idea, since a
    # psycopg message can quote a password back *re-escaped*. There is no
    # sentence now.
    assert info.last_failure is not None
    assert info.last_failure.subject == "schedule:nightly"
    assert "transform exploded" not in info.last_failure.model_dump_json()
    assert info.next_run_at > utcnow_iso(), "must still be scheduled"
    assert "schedule_failed" in [e.action for e in store.list_audit()]


def test_one_broken_workspace_does_not_stop_the_others(store, tmp_path):
    other = MetadataStore(tmp_path / "other.db")
    other.upsert_schedule(cron_schedule(name="healthy", next_run_at=PAST))

    def targets():
        def explode():
            raise RuntimeError("workspace unreachable")
        return [("broken", _Exploding(), lambda s: None),
                ("ok", other, lambda s: "b1")]

    sched = scheduler.Scheduler(open_stores=targets, worker_id="a")
    assert sched.tick() == ["healthy"]


class _Exploding:
    def due_schedules(self, now):
        raise RuntimeError("workspace unreachable")


def test_disabled_schedules_never_fire(store):
    store.upsert_schedule(cron_schedule(enabled=False, next_run_at=PAST))
    sched = scheduler.Scheduler(
        open_stores=lambda: [("ws", store, lambda s: "b")], worker_id="a"
    )
    assert sched.tick() == []


# -- upstream trigger --------------------------------------------------------------

@pytest.fixture()
def catalog_env(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="sched")
    store = MetadataStore(ws.metadata_path)
    return ws, store, DatasetCatalog(ws, store)


def test_upstream_trigger_follows_the_dataset(catalog_env):
    ws, store, catalog = catalog_env
    catalog.write("raw", pa.table({"id": [1]}))
    store.upsert_schedule(ScheduleInfo(
        name="on_raw", trigger="upstream", upstream_dataset="raw", action="build"
    ))
    runs = []
    sched = scheduler.Scheduler(
        open_stores=lambda: [("ws", store, lambda s: runs.append(1) or "b")],
        worker_id="a",
    )

    assert sched.tick() == ["on_raw"], "first sighting fires"
    assert sched.tick() == [], "no new version: nothing to do"

    catalog.write("raw", pa.table({"id": [2]}))  # v2
    assert sched.tick() == ["on_raw"], "a new version fires again"
    assert len(runs) == 2
    assert store.get_schedule("on_raw").watermark == 2


def test_upstream_trigger_waits_for_a_dataset_that_does_not_exist_yet(catalog_env):
    _, store, _ = catalog_env
    store.upsert_schedule(ScheduleInfo(
        name="on_missing", trigger="upstream", upstream_dataset="ghost"
    ))
    sched = scheduler.Scheduler(
        open_stores=lambda: [("ws", store, lambda s: "b")], worker_id="a"
    )
    assert sched.tick() == []


# -- API ---------------------------------------------------------------------------

CREDS = {"username": "root", "password": "trustno1!"}

PIPELINE = """
from laurelin.transforms import transform, Input, Output

@transform(output=Output("clean"), raw=Input("raw"))
def clean(raw):
    return raw
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURELIN_SCHEDULER", "0")  # drive ticks explicitly
    ws = Workspace.init(tmp_path / "ws", name="api")
    store = MetadataStore(ws.metadata_path)
    DatasetCatalog(ws, store).write("raw", pa.table({"id": [1, 2]}))
    (ws.pipelines_dir / "p.py").write_text(PIPELINE)
    app = create_app(ws)
    c = TestClient(app)
    c.post("/api/v1/auth/setup", json=CREDS)
    c.post("/api/v1/auth/login", json=CREDS)
    return c, app, store


def test_schedule_crud(client):
    c, _, _ = client
    body = {"trigger": "cron", "cron": "0 2 * * *", "action": "build"}
    r = c.put("/api/v1/schedules/nightly", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["next_run_at"] is not None, "a cron schedule must be due sometime"

    assert [s["name"] for s in c.get("/api/v1/schedules").json()] == ["nightly"]
    assert c.get("/api/v1/schedules/nightly").json()["cron"] == "0 2 * * *"
    assert c.delete("/api/v1/schedules/nightly").status_code == 200
    assert c.get("/api/v1/schedules/nightly").status_code == 404


def test_bad_definitions_are_rejected_at_save(client):
    c, _, _ = client
    for body in (
        {"trigger": "cron", "cron": "not a cron"},
        {"trigger": "upstream"},
        {"trigger": "cron", "cron": "0 2 * * *", "action": "sync"},
    ):
        assert c.put("/api/v1/schedules/bad", json=body).status_code == 400
    assert c.put("/api/v1/schedules/Bad Name",
                 json={"cron": "0 2 * * *"}).status_code == 400


def test_run_now_makes_it_due(client):
    c, app, store = client
    c.put("/api/v1/schedules/nightly", json={"cron": "0 2 * * *"})
    assert store.get_schedule("nightly").next_run_at > utcnow_iso()

    r = c.post("/api/v1/schedules/nightly/run")
    assert r.status_code == 200
    assert store.get_schedule("nightly").next_run_at <= utcnow_iso()

    # Firing goes through the ordinary scheduler path, producing a real build.
    from laurelin.api.app import _scheduler_targets

    sched = scheduler.Scheduler(lambda: _scheduler_targets(app), worker_id="a")
    assert sched.tick() == ["nightly"]
    info = store.get_schedule("nightly")
    assert info.last_status == "succeeded" and info.last_build_id
    assert store.get_build(info.last_build_id) is not None


def test_run_now_refuses_a_disabled_schedule(client):
    c, _, _ = client
    c.put("/api/v1/schedules/off", json={"cron": "0 2 * * *", "enabled": False})
    assert c.post("/api/v1/schedules/off/run").status_code == 409


def test_schedules_are_editor_gated(client):
    c, _, _ = client
    c.post("/api/v1/users", json={"username": "vic", "password": "password123",
                                  "role": "viewer"})
    viewer = TestClient(c.app)
    viewer.post("/api/v1/auth/login", json={"username": "vic", "password": "password123"})
    assert viewer.get("/api/v1/schedules").status_code == 403
    assert viewer.put("/api/v1/schedules/x", json={"cron": "0 2 * * *"}).status_code == 403
