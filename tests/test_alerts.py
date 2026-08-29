"""Alerting (task #74): edge-triggered, opt-in outbound, viewer-level payloads.

The load-bearing invariants:

* **nothing posts anywhere by default** — no webhook exists until an admin
  creates one, and creation lands disabled;
* **the payload is mechanically the viewer serialization of DatasetHealth** —
  never a hand-filtered dict, so a masked value, an expectation's editor prose,
  a measured count, a cursor value or a row count cannot ride out;
* **alerts fire on transitions, once**, and recovery fires once;
* the webhook URL is a credential: write-only through the API, withheld from
  export archives;
* a dead endpoint records a structured Failure and never breaks the tick, and
  the response body is never stored.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core import serialize
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.failure import Failure, FailureCode
from laurelin.core.health import HealthService
from laurelin.core.models import (
    BuildStatus,
    BuildTaskInfo,
    Role,
    ScheduleInfo,
    SourceInfo,
)
from laurelin.core.redaction import WITHHELD

ADMIN_CREDS = {"username": "root", "password": "trustno1!"}

SECRET_URL = "https://hooks.example.com/services/T00/B00/hookS3KRETpath"


@pytest.fixture()
def ws(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="alerts")
    cat = DatasetCatalog(ws, MetadataStore(ws.metadata_path))
    cat.write("orders_clean", pa.table({"order_id": ["a"], "card_number": ["4111"]}))
    return ws


@pytest.fixture()
def store(ws):
    return MetadataStore(ws.metadata_path)


@pytest.fixture()
def service(store):
    return HealthService(store)


def _make_failing(store, dataset="orders_clean"):
    build = store.create_build([dataset])
    store.update_build(build.id, status=BuildStatus.failed)
    store.upsert_build_task(build.id, BuildTaskInfo(
        transform_name="clean", output_dataset=dataset,
        status=BuildStatus.failed, started_at="2026-01-01T00:00:00+00:00",
        failure=Failure(code=FailureCode.EXPECTATION_FAILED,
                        subject=f"build_task:{dataset}"),
        rows_written=987_654,
        expectations=[{
            "expectation": "not_null(order_id)", "passed": False,
            "severity": "error", "measured": 424_242,
            "message": "S3NT1NEL editor prose quoting card_number 4111",
        }],
    ))
    return build.id


def _make_healthy(store, dataset="orders_clean"):
    """A later, succeeding task supersedes the failing one."""
    build = store.create_build([dataset])
    store.update_build(build.id, status=BuildStatus.succeeded)
    store.upsert_build_task(build.id, BuildTaskInfo(
        transform_name="clean", output_dataset=dataset,
        status=BuildStatus.succeeded, started_at="2027-01-01T00:00:00+00:00",
        expectations=[],
    ))


class _Recorder:
    """An injected delivery: records (url, payload) and returns 200."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        return 200


# -- default posture -----------------------------------------------------------

def test_no_webhook_exists_by_default_and_nothing_posts(store, service):
    _make_failing(store)
    recorder = _Recorder()
    fired = service.evaluate_and_alert(post=recorder)
    # The transition is real and recorded in-app...
    assert fired == ["dataset_failing"]
    assert [e["event"] for e in store.list_health_events()] == ["dataset_failing"]
    # ...and zero bytes went anywhere: no webhook is configured.
    assert store.list_alert_webhooks() == []
    assert recorder.calls == []


def test_an_alert_fires_on_transition_and_not_on_every_tick(store, service):
    _make_failing(store)
    store.upsert_alert_webhook(
        "sink", SECRET_URL, [], [], True, created_by="root"
    )
    recorder = _Recorder()
    assert service.evaluate_and_alert(post=recorder) == ["dataset_failing"]
    assert service.evaluate_and_alert(post=recorder) == []  # level, not edge
    assert service.evaluate_and_alert(post=recorder) == []
    assert len(recorder.calls) == 1


def test_recovery_fires_once(store, service):
    _make_failing(store)
    store.upsert_alert_webhook("sink", SECRET_URL, [], [], True, created_by="root")
    recorder = _Recorder()
    service.evaluate_and_alert(post=recorder)
    _make_healthy(store)
    assert service.evaluate_and_alert(post=recorder) == ["dataset_healthy"]
    assert service.evaluate_and_alert(post=recorder) == []
    events = [c[1]["event"] for c in recorder.calls]
    assert events == ["dataset_failing", "dataset_healthy"]


def test_a_dataset_that_appears_already_healthy_does_not_alert(store, service):
    """First sight of a healthy dataset is not a recovery."""
    recorder = _Recorder()
    store.upsert_alert_webhook("sink", SECRET_URL, [], [], True, created_by="root")
    assert service.evaluate_and_alert(post=recorder) == []
    assert recorder.calls == []


def test_webhook_dataset_and_event_filters_scope_delivery(store, service):
    _make_failing(store)
    store.upsert_alert_webhook(
        "other", SECRET_URL, ["some_other_ds"], [], True, created_by="root"
    )
    store.upsert_alert_webhook(
        "recoveries-only", SECRET_URL, [], ["dataset_healthy"], True,
        created_by="root",
    )
    store.upsert_alert_webhook("disabled", SECRET_URL, [], [], False, created_by="root")
    recorder = _Recorder()
    assert service.evaluate_and_alert(post=recorder) == ["dataset_failing"]
    assert recorder.calls == []  # scoped away, filtered away, or disabled


# -- the payload rule ----------------------------------------------------------

def test_alert_payload_is_the_viewer_serialization_of_dataset_health(
    store, service
):
    _make_failing(store)
    store.upsert_alert_webhook("sink", SECRET_URL, [], [], True, created_by="root")
    recorder = _Recorder()
    service.evaluate_and_alert(post=recorder)
    [(url, payload)] = recorder.calls
    assert url == SECRET_URL

    record = service.dataset_health(["orders_clean"])["orders_clean"]
    expected = serialize.dump_as(record, Role.viewer)
    for key, value in expected.items():
        assert payload[key] == value, key
    # Envelope: the event, a timestamp, and a PATH — never an absolute URL
    # that would teach the recipient where the server lives.
    assert payload["event"] == "dataset_failing"
    assert payload["link"] == "/health"
    assert set(payload) == set(expected) | {"event", "at", "link"}


def test_alert_payload_never_contains_message_measured_row_counts_or_cursor_value(
    store, service
):
    _make_failing(store)
    # A source with a cursor value — governed data wearing a bookkeeping hat.
    store.upsert_source(SourceInfo(
        name="feed", type="http", dataset="orders_clean", config={},
    ))
    store.record_source_sync(
        "feed", "succeeded", cursor_value="CURS3KRET-last-card-4111",
    )
    store.upsert_schedule(ScheduleInfo(
        name="sekrit-sched", trigger="cron", cron="0 2 * * *", action="build",
        targets=["orders_clean"], next_run_at="2000-01-01T00:00:00+00:00",
    ))
    store.upsert_alert_webhook("sink", SECRET_URL, [], [], True, created_by="root")
    recorder = _Recorder()
    service.evaluate_and_alert(post=recorder)
    assert recorder.calls, "the failing dataset must have alerted"
    raw = json.dumps(recorder.calls[0][1])
    # Editor prose and the measured value over possibly-masked contents:
    assert "S3NT1NEL" not in raw
    assert "424242" not in raw
    # Row counts of any kind:
    assert "987654" not in raw
    assert "rows_written" not in raw and "row_count" not in raw
    # The source's cursor (a value out of the source's own data):
    assert "CURS3KRET" not in raw
    # Operator names below editor:
    assert "sekrit-sched" not in raw and "feed" not in raw


def test_the_alert_survives_a_raised_ambient_role(store, service):
    """Even if a future caller runs the tick under an admin serialization
    context, the payload stays the viewer projection — dump_as(viewer) is
    called explicitly, not inherited from the ContextVar."""
    _make_failing(store)
    store.upsert_alert_webhook("sink", SECRET_URL, [], [], True, created_by="root")
    recorder = _Recorder()
    with serialize.as_author(Role.admin):
        service.evaluate_and_alert(post=recorder)
    raw = json.dumps(recorder.calls[0][1])
    assert "S3NT1NEL" not in raw
    assert "424242" not in raw


# -- webhook config over HTTP: the URL is a credential --------------------------

@pytest.fixture()
def app(ws):
    return create_app(ws)


@pytest.fixture()
def admin(app):
    c = TestClient(app)
    assert c.post("/api/v1/auth/setup", json=ADMIN_CREDS).status_code == 200
    assert c.post("/api/v1/auth/login", json=ADMIN_CREDS).status_code == 200
    return c


@pytest.fixture()
def viewer(app, admin):
    assert admin.post(
        "/api/v1/users",
        json={"username": "vic", "password": "password123", "role": "viewer"},
    ).status_code == 200
    c = TestClient(app)
    assert c.post(
        "/api/v1/auth/login", json={"username": "vic", "password": "password123"}
    ).status_code == 200
    return c


def test_webhook_url_reads_back_withheld(admin):
    r = admin.put(
        "/api/v1/alerts/webhooks/pager",
        json={"url": SECRET_URL, "enabled": False},
    )
    assert r.status_code == 200
    assert "hookS3KRET" not in r.text
    assert r.json()["url"] == WITHHELD

    single = admin.get("/api/v1/alerts/webhooks/pager")
    listing = admin.get("/api/v1/alerts/webhooks")
    assert "hookS3KRET" not in single.text and "hookS3KRET" not in listing.text
    assert single.json()["url"] == WITHHELD

    # An update that omits `url` succeeds and still discloses nothing; that it
    # kept the stored credential is the next test's assertion.
    r = admin.put("/api/v1/alerts/webhooks/pager", json={"enabled": True})
    assert r.status_code == 200
    assert "hookS3KRET" not in r.text


def test_an_update_without_url_keeps_the_stored_credential(admin, ws):
    admin.put("/api/v1/alerts/webhooks/pager", json={"url": SECRET_URL})
    admin.put("/api/v1/alerts/webhooks/pager", json={"enabled": True})
    row = MetadataStore(ws.metadata_path).get_alert_webhook("pager")
    assert row["url"] == SECRET_URL
    assert row["enabled"] is True


def test_putting_the_withheld_marker_back_is_refused(admin):
    admin.put("/api/v1/alerts/webhooks/pager", json={"url": SECRET_URL})
    r = admin.put("/api/v1/alerts/webhooks/pager", json={"url": WITHHELD})
    assert r.status_code == 400


def test_webhook_routes_are_admin_only(admin, viewer):
    admin.put("/api/v1/alerts/webhooks/pager", json={"url": SECRET_URL})
    assert viewer.get("/api/v1/alerts/webhooks").status_code == 403
    assert viewer.get("/api/v1/alerts/webhooks/pager").status_code == 403
    assert viewer.put(
        "/api/v1/alerts/webhooks/mine", json={"url": "https://x.example/y"}
    ).status_code == 403
    assert viewer.delete("/api/v1/alerts/webhooks/pager").status_code == 403
    assert viewer.post("/api/v1/alerts/webhooks/pager/test").status_code == 403


def test_webhook_url_is_omitted_from_an_export_archive(ws, store, tmp_path):
    import tarfile

    from laurelin.export import ExportOptions, export_workspace

    store.upsert_alert_webhook("pager", SECRET_URL, [], [], True, created_by="root")
    archive = tmp_path / "out.tar.gz"
    export_workspace(ws, store, archive, ExportOptions())
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            f = tar.extractfile(member)
            if f is None:
                continue
            assert b"hookS3KRET" not in f.read(), member.name


# -- delivery ------------------------------------------------------------------

def test_webhook_delivery_failure_is_recorded_as_a_failure_and_does_not_raise(
    store, service
):
    _make_failing(store)
    # Port 9 (discard) on localhost: nothing listens; connection refused fast.
    store.upsert_alert_webhook(
        "dead", "http://127.0.0.1:9/hook", [], [], True, created_by="root"
    )
    fired = service.evaluate_and_alert()  # real urllib path; must not raise
    assert fired == ["dataset_failing"]
    row = store.get_alert_webhook("dead")
    assert row["last_delivery_status"] is None
    failure = row["last_delivery_failure"]
    assert isinstance(failure, Failure)
    assert failure.subject == "webhook:dead"
    # A structured Failure has no field a response body fits into — the
    # attacker-controlled text cannot have been stored. Spot-check the record:
    assert "refused" not in json.dumps(failure.model_dump(mode="json")).lower()


class _CaptureHandler(BaseHTTPRequestHandler):
    bodies: list[bytes] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", 0))
        _CaptureHandler.bodies.append(self.rfile.read(length))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture()
def capture_server():
    _CaptureHandler.bodies = []
    server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook"
    server.shutdown()


def test_delivery_actually_posts_json_over_http(store, service, capture_server):
    _make_failing(store)
    store.upsert_alert_webhook("live", capture_server, [], [], True, created_by="root")
    assert service.evaluate_and_alert() == ["dataset_failing"]
    assert len(_CaptureHandler.bodies) == 1
    payload = json.loads(_CaptureHandler.bodies[0])
    assert payload["dataset"] == "orders_clean"
    assert payload["event"] == "dataset_failing"
    row = store.get_alert_webhook("live")
    assert row["last_delivery_status"] == 200
    assert row["last_delivery_failure"] is None


def test_the_test_route_sends_a_synthetic_payload_with_no_governed_values(
    admin, ws, capture_server
):
    # Real datasets exist in the workspace; none of their names may travel.
    r = admin.put(
        "/api/v1/alerts/webhooks/probe", json={"url": capture_server}
    )
    assert r.status_code == 200
    r = admin.post("/api/v1/alerts/webhooks/probe/test")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    [body] = _CaptureHandler.bodies
    payload = json.loads(body)
    assert payload["dataset"] == "laurelin.test"
    assert b"orders_clean" not in body


# -- the scheduler piggyback ---------------------------------------------------

def test_the_scheduler_tick_runs_the_alert_pass(store):
    from laurelin.core.scheduler import Scheduler

    _make_failing(store)
    sched = Scheduler(open_stores=lambda: [("ws", store, lambda s: None)],
                      worker_id="w1")
    sched.tick()
    assert [e["event"] for e in store.list_health_events()] == ["dataset_failing"]
    # State persisted: a second tick (or a restart) does not re-fire.
    sched.tick()
    assert len(store.list_health_events()) == 1
