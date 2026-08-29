"""Data health (task #74): a deterministic read over records that already exist.

The invariants under test, in the order they matter:

* the rollup never names a dataset its caller cannot view — a health page that
  lists every dataset name is a disclosure;
* an expired build lease reads as *failed*, never as an eternal `running`;
* a silent schedule (dead scheduler included — same predicate) reads `overdue`
  with no scheduler thread anywhere in the process;
* a declared freshness window that was missed reads `stale`; an undeclared,
  unscheduled, never-built dataset reads `unknown`, not red;
* expectation `message`/`measured`, `Failure` operator fields, and schedule /
  source *names* stay editor-and-above — a viewer gets statuses, codes,
  timestamps and names of things they can already see.
"""

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.failure import Failure, FailureCode
from laurelin.core.health import HealthService
from laurelin.core.models import (
    BuildStatus,
    BuildTaskInfo,
    DatasetVersionInfo,
    HealthStatus,
    ScheduleInfo,
    SourceInfo,
)

PAST = "2000-01-01T00:00:00+00:00"

ADMIN_CREDS = {"username": "root", "password": "trustno1!"}


@pytest.fixture()
def ws(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="health")
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    cat.write("public_ds", pa.table({"id": ["a"]}))
    cat.write("secret_ds", pa.table({"id": ["s"]}))
    cat.write("classified_ds", pa.table({"id": ["c"]}))
    return ws


@pytest.fixture()
def store(ws):
    return MetadataStore(ws.metadata_path)


@pytest.fixture()
def service(store):
    return HealthService(store)


@pytest.fixture()
def app(ws):
    return create_app(ws)


@pytest.fixture()
def admin(app):
    c = TestClient(app)
    assert c.post("/api/v1/auth/setup", json=ADMIN_CREDS).status_code == 200
    assert c.post("/api/v1/auth/login", json=ADMIN_CREDS).status_code == 200
    return c


def _login(app, admin, username, role):
    r = admin.post(
        "/api/v1/users",
        json={"username": username, "password": "password123", "role": role},
    )
    assert r.status_code == 200
    c = TestClient(app)
    assert c.post(
        "/api/v1/auth/login", json={"username": username, "password": "password123"}
    ).status_code == 200
    return c


@pytest.fixture()
def viewer(app, admin):
    return _login(app, admin, "vic", "viewer")


@pytest.fixture()
def editor(app, admin):
    return _login(app, admin, "ed", "editor")


def _seed_failing_task(store, dataset, *, message="", measured=None, endpoint=""):
    """A failed build task with one failing error-severity expectation."""
    build = store.create_build([dataset])
    store.update_build(build.id, status=BuildStatus.failed)
    store.upsert_build_task(build.id, BuildTaskInfo(
        transform_name="clean",
        output_dataset=dataset,
        status=BuildStatus.failed,
        started_at="2026-01-01T00:00:00+00:00",
        failure=Failure(
            code=FailureCode.EXPECTATION_FAILED,
            subject=f"build_task:{dataset}",
            endpoint=endpoint,
        ),
        rows_written=987_654,
        expectations=[{
            "expectation": "not_null(order_id)",
            "passed": False,
            "severity": "error",
            "measured": measured if measured is not None else 133_737,
            "message": message or "editor prose about the failure",
        }],
    ))
    return build.id


# -- the filter: the rollup never names what the caller cannot view ------------

def test_health_rollup_omits_datasets_the_caller_cannot_view(admin, viewer):
    # Grant-restricted: secret_ds locked to root alone.
    r = admin.put(
        "/api/v1/datasets/secret_ds/permissions",
        json={"grants": [{"subject_kind": "user", "subject": "root",
                          "can_view": True, "can_edit": True}]},
    )
    assert r.status_code == 200
    # Marking-restricted: classified_ds needs a clearance vic does not hold.
    assert admin.post("/api/v1/markings", json={"name": "pii"}).status_code == 200
    assert admin.put(
        "/api/v1/datasets/classified_ds/markings", json={"markings": ["pii"]}
    ).status_code == 200

    r = viewer.get("/api/v1/health/datasets")
    assert r.status_code == 200
    # The disclosure assertion is on the RAW BODY, not parsed fields: a name
    # leaking through any stray field is exactly what this test exists to catch.
    assert "secret_ds" not in r.text
    assert "classified_ds" not in r.text
    assert {h["dataset"] for h in r.json()} == {"public_ds"}

    # The admin still sees the whole workspace.
    names = {h["dataset"] for h in admin.get("/api/v1/health/datasets").json()}
    assert names == {"public_ds", "secret_ds", "classified_ds"}


def test_health_rollup_has_no_unfiltered_totals(admin, viewer):
    admin.put(
        "/api/v1/datasets/secret_ds/permissions",
        json={"grants": [{"subject_kind": "user", "subject": "root",
                          "can_view": True}]},
    )
    va = admin.get("/api/v1/health/datasets").json()
    vv = viewer.get("/api/v1/health/datasets").json()
    # Different viewers legitimately see different totals; the totals ARE the
    # list lengths, and there is no count field anywhere for them to disagree
    # in. A global count would leak existence deltas when a hidden dataset
    # flips state.
    assert isinstance(va, list) and isinstance(vv, list)
    assert len(va) == 3 and len(vv) == 2


def test_health_events_are_filtered_like_the_rollup(admin, viewer, store, service):
    admin.put(
        "/api/v1/datasets/secret_ds/permissions",
        json={"grants": [{"subject_kind": "user", "subject": "root",
                          "can_view": True}]},
    )
    _seed_failing_task(store, "secret_ds")
    service.evaluate_and_alert()  # records a dataset_failing event
    r = viewer.get("/api/v1/health/events")
    assert r.status_code == 200
    assert "secret_ds" not in r.text
    admin_events = admin.get("/api/v1/health/events").json()
    assert any(e["dataset"] == "secret_ds" for e in admin_events)


def test_health_event_seq_does_not_leak_hidden_transitions(admin, viewer, store):
    """The finding: ``seq`` is a GLOBAL monotonic PK, so filtering rows by
    dataset AFTER assigning it left gaps a viewer could read — seq 1 and 3 but
    not 2 says "one transition happened on a dataset I cannot see", timeable
    from the bracketing ``at`` values, and the absolute max leaks the total
    event count workspace-wide. The per-response ordinal must be gap-free and
    must not encode the global count."""
    admin.put(
        "/api/v1/datasets/secret_ds/permissions",
        json={"grants": [{"subject_kind": "user", "subject": "root",
                          "can_view": True}]},
    )
    # Interleave so the GLOBAL seq alternates hidden/visible: 1,2,3,4.
    store.add_health_event("public_ds", "dataset_stale", "stale", "2026-01-01T00:00:01+00:00")
    store.add_health_event("secret_ds", "dataset_failing", "failing", "2026-01-01T00:00:02+00:00")
    store.add_health_event("public_ds", "dataset_healthy", "healthy", "2026-01-01T00:00:03+00:00")
    store.add_health_event("secret_ds", "dataset_healthy", "healthy", "2026-01-01T00:00:04+00:00")

    events = viewer.get("/api/v1/health/events").json()
    # Only the viewer's two public_ds events, and their seq is contiguous with
    # no gap where the two hidden secret_ds events (global seq 2 and 4) sit.
    assert [e["dataset"] for e in events] == ["public_ds", "public_ds"]
    seqs = [e["seq"] for e in events]
    assert seqs == [2, 1]  # renumbered locally, not [3, 1]
    # The max ordinal equals the count the viewer can see — never the global 4.
    assert max(seqs) == len(events)


# -- status derivation ---------------------------------------------------------

def test_an_expired_build_lease_reads_as_failed_not_running(store, service):
    """A replica that died mid-build must not read as eternally in-flight."""
    build = store.create_build(["public_ds"])
    assert store.claim_build(build.id, "dead-replica", lease_seconds=-5)
    store.update_build(build.id, status=BuildStatus.running)
    store.upsert_build_task(build.id, BuildTaskInfo(
        transform_name="clean", output_dataset="public_ds",
        status=BuildStatus.running, started_at="2026-01-01T00:00:00+00:00",
    ))

    health = service.dataset_health(["public_ds"])["public_ds"]
    assert health.status == HealthStatus.failing
    assert health.last_build_status == BuildStatus.failed
    assert health.last_failure is not None
    assert health.last_failure.code == FailureCode.REMOTE_FAILED


def test_a_cron_schedule_past_next_run_reports_overdue(store, service):
    store.upsert_schedule(ScheduleInfo(
        name="nightly", trigger="cron", cron="0 2 * * *", action="build",
        targets=["public_ds"], next_run_at=PAST,
    ))
    health = service.dataset_health(["public_ds"])["public_ds"]
    assert health.status == HealthStatus.overdue
    assert health.schedule_overdue is True


def test_overdue_is_detected_when_the_scheduler_thread_is_dead(store):
    """Same predicate, and deliberately no Scheduler anywhere in this test:
    next_run_at only advances via record_schedule_run, so a dead scheduler
    process trips the same comparison at read time."""
    store.upsert_schedule(ScheduleInfo(
        name="nightly", trigger="cron", cron="0 2 * * *", action="build",
        targets=["public_ds"], next_run_at=PAST,
    ))
    health = HealthService(store).dataset_health(["public_ds"])["public_ds"]
    assert health.status == HealthStatus.overdue


def test_a_disabled_schedule_is_not_overdue(store, service):
    store.upsert_schedule(ScheduleInfo(
        name="nightly", trigger="cron", cron="0 2 * * *", action="build",
        targets=["public_ds"], next_run_at=PAST, enabled=False,
    ))
    health = service.dataset_health(["public_ds"])["public_ds"]
    assert health.status == HealthStatus.healthy


def test_a_dataset_with_no_declaration_and_no_schedule_is_unknown_not_stale(
    store, service
):
    """An ad-hoc dataset is not 'stale', it is undeclared — reporting it red
    would train operators to ignore red."""
    store.upsert_dataset("adhoc")  # no versions, no schedule, no declaration
    health = service.dataset_health(["adhoc", "public_ds"])
    assert health["adhoc"].status == HealthStatus.unknown
    # ...while a dataset that HAS data and nothing else declared is healthy.
    assert health["public_ds"].status == HealthStatus.healthy


def test_declared_freshness_marks_a_quiet_dataset_stale(store, service):
    store.upsert_dataset("quiet")
    store.add_version(DatasetVersionInfo(
        dataset="quiet", version=1, created_at=PAST, row_count=1,
    ))
    store.set_dataset_freshness("quiet", 3600)
    health = service.dataset_health(["quiet"])["quiet"]
    assert health.status == HealthStatus.stale
    assert health.expected_fresh_within == 3600
    # A fresh dataset with the same declaration is healthy.
    store.set_dataset_freshness("public_ds", 3600)
    assert (
        service.dataset_health(["public_ds"])["public_ds"].status
        == HealthStatus.healthy
    )


def test_a_failing_source_sync_marks_the_dataset_failing(store, service):
    store.upsert_source(SourceInfo(
        name="feed", type="http", dataset="public_ds", config={},
    ))
    store.record_source_sync("feed", "failed", failure=Failure(
        code=FailureCode.AUTH_REJECTED, subject="source:feed",
    ))
    health = service.dataset_health(["public_ds"])["public_ds"]
    assert health.status == HealthStatus.failing
    assert health.sync_failing is True
    assert health.last_failure is not None
    assert health.last_failure.code == FailureCode.AUTH_REJECTED


def test_failing_outranks_overdue_outranks_stale(store, service):
    _seed_failing_task(store, "public_ds")
    store.upsert_schedule(ScheduleInfo(
        name="nightly", trigger="cron", cron="0 2 * * *", action="build",
        targets=["public_ds"], next_run_at=PAST,
    ))
    store.set_dataset_freshness("public_ds", 60)
    health = service.dataset_health(["public_ds"])["public_ds"]
    assert health.status == HealthStatus.failing
    assert health.schedule_overdue is True  # both facts still reported


# -- R2 on the rollup body -----------------------------------------------------

def test_expectation_message_and_measured_are_withheld_below_editor(
    admin, viewer, editor, store
):
    _seed_failing_task(
        store, "public_ds", message="S3NT1NEL_EXPECTATION_PROSE", measured=133_737
    )
    v = viewer.get("/api/v1/health/datasets")
    assert v.status_code == 200
    assert "S3NT1NEL_EXPECTATION_PROSE" not in v.text
    assert "133737" not in v.text
    # The viewer still learns WHICH expectation failed — name, severity — which
    # is schema-shaped and viewer-visible through the query path anyway.
    row = next(h for h in v.json() if h["dataset"] == "public_ds")
    assert row["failing_expectations"] == [{
        "name": "not_null(order_id)", "column": "", "severity": "error",
        "passed": False,
    }]

    e = editor.get("/api/v1/health/datasets")
    assert "S3NT1NEL_EXPECTATION_PROSE" in e.text
    assert "133737" in e.text


def test_viewer_sees_failure_as_code_and_subject_only(admin, viewer, store):
    _seed_failing_task(store, "public_ds", endpoint="secret-db.internal:5432")
    row = next(
        h for h in viewer.get("/api/v1/health/datasets").json()
        if h["dataset"] == "public_ds"
    )
    assert set(row["failure"] if "failure" in row else row["last_failure"]) == {
        "code", "subject"
    }
    assert "secret-db.internal" not in viewer.get("/api/v1/health/datasets").text


def test_schedule_and_source_names_do_not_reach_a_viewer(
    admin, viewer, editor, store
):
    store.upsert_schedule(ScheduleInfo(
        name="sekrit-sched", trigger="cron", cron="0 2 * * *", action="build",
        targets=["public_ds"], next_run_at=PAST,
    ))
    store.upsert_source(SourceInfo(
        name="sekrit_source", type="http", dataset="public_ds", config={},
    ))
    store.record_source_sync("sekrit_source", "failed", failure=Failure(
        code=FailureCode.AUTH_REJECTED, subject="source:sekrit_source",
    ))
    v = viewer.get("/api/v1/health/datasets")
    assert "sekrit-sched" not in v.text
    assert "sekrit_source" not in v.text
    row = next(h for h in v.json() if h["dataset"] == "public_ds")
    # The viewer learns the booleans, not the names.
    assert row["schedule_overdue"] is True
    assert row["sync_failing"] is True

    e = editor.get("/api/v1/health/datasets")
    assert "sekrit-sched" in e.text  # detail is editor-and-above
    assert "sekrit_source" in e.text.replace("source:sekrit_source", "")  # the name itself


def test_rows_written_and_row_counts_never_appear_in_the_rollup(viewer, store):
    """No count field exists on DatasetHealth's surface at all — outbound
    payloads reuse this exact record, and the export precedent applies to
    anything outbound."""
    _seed_failing_task(store, "public_ds")  # rows_written=987654 on the task
    r = viewer.get("/api/v1/health/datasets")
    assert "987654" not in r.text
    assert "rows_written" not in r.text
    assert "row_count" not in r.text


# -- the freshness declaration -------------------------------------------------

def test_freshness_declaration_is_editor_gated(admin, viewer, editor):
    assert viewer.put(
        "/api/v1/datasets/public_ds/freshness",
        json={"expected_fresh_seconds": 3600},
    ).status_code == 403
    r = editor.put(
        "/api/v1/datasets/public_ds/freshness",
        json={"expected_fresh_seconds": 3600},
    )
    assert r.status_code == 200
    row = next(
        h for h in editor.get("/api/v1/health/datasets").json()
        if h["dataset"] == "public_ds"
    )
    assert row["expected_fresh_within"] == 3600
    # Clearing works, and clears.
    assert editor.put(
        "/api/v1/datasets/public_ds/freshness",
        json={"expected_fresh_seconds": None},
    ).status_code == 200
    row = next(
        h for h in editor.get("/api/v1/health/datasets").json()
        if h["dataset"] == "public_ds"
    )
    assert row["expected_fresh_within"] is None


def test_freshness_declaration_rejects_unknown_dataset_and_bad_values(editor):
    assert editor.put(
        "/api/v1/datasets/nope/freshness", json={"expected_fresh_seconds": 3600}
    ).status_code == 404
    assert editor.put(
        "/api/v1/datasets/public_ds/freshness", json={"expected_fresh_seconds": 5}
    ).status_code == 400  # sub-minute promise is a typo, not a policy
