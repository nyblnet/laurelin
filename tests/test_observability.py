"""Observability: metrics, structured logs, request IDs.

The platform now does things on its own — a scheduler fires pipelines, leases
change hands, limits reject queries. None of that should be invisible.
"""

import json
import logging

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core import limits, metrics
from laurelin.core import logging as laurelin_logging
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore

CREDS = {"username": "root", "password": "trustno1!"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURELIN_SCHEDULER", "0")
    ws = Workspace.init(tmp_path / "ws", name="obs")
    store = MetadataStore(ws.metadata_path)
    DatasetCatalog(ws, store).write("t", pa.table({"id": list(range(10))}))
    return ws, store


@pytest.fixture()
def client(env):
    ws, _ = env
    return TestClient(create_app(ws, no_auth=True))


def scrape(client) -> str:
    r = client.get("/metrics")
    assert r.status_code == 200, r.text
    return r.text


# -- exposition -----------------------------------------------------------------

def test_metrics_endpoint_serves_prometheus_text(client):
    body = scrape(client)
    assert "laurelin_http_requests_total" in body
    assert "# TYPE" in body


def test_http_requests_are_counted_by_route_template(client):
    """The label must be the template, not the concrete path — otherwise
    cardinality grows with the number of datasets."""
    client.get("/api/v1/datasets/t/rows")
    client.get("/api/v1/datasets/t/schema")
    body = scrape(client)

    # FastAPI reports the template without the constant /api/v1 prefix.
    assert 'route="/datasets/{name}/rows"' in body
    assert 'route="/datasets/{name}/schema"' in body
    assert "datasets/t" not in body, "concrete paths must never become labels"


def test_unmatched_routes_do_not_create_labels(client):
    """A 404 has no route; it must not label the metric with the raw path."""
    client.get("/api/v1/datasets/nope/rows")
    client.get("/totally/unknown/path")
    body = scrape(client)
    assert 'route="unmatched"' in body
    assert "totally/unknown" not in body


def test_request_id_is_returned_and_propagated(client):
    r = client.get("/api/v1/datasets")
    assert r.headers.get("X-Request-ID"), "every response should be traceable"

    # A caller-supplied id is honoured, so a trace spans services.
    r = client.get("/api/v1/datasets", headers={"X-Request-ID": "abc123"})
    assert r.headers["X-Request-ID"] == "abc123"


def test_queries_are_counted_and_timed(client):
    before = scrape(client)
    client.post("/api/v1/query", json={"sql": "SELECT count(*) FROM t"})
    after = scrape(client)
    assert 'laurelin_queries_total{surface="workbench"}' in after
    assert "laurelin_query_seconds_bucket" in after
    assert before != after


def test_rejections_are_labelled_by_reason(client, monkeypatch):
    """Timeout, memory and admission need different remedies, so they need
    different labels."""
    monkeypatch.setenv("LAURELIN_QUERY_TIMEOUT", "0.5")
    r = client.post("/api/v1/query", json={
        "sql": "SELECT count(*) FROM range(20000000000) x(i) WHERE i % 7 = 0"})
    assert r.status_code == 504
    assert 'laurelin_query_rejections_total{reason="timeout"}' in scrape(client)


def test_builds_are_counted(env, client):
    ws, _ = env
    (ws.pipelines_dir / "p.py").write_text(
        "from laurelin.transforms import transform, Input, Output\n"
        "@transform(output=Output('out'), t=Input('t'))\n"
        "def out(t): return t\n"
    )
    client.post("/api/v1/builds", json={"wait": True})
    assert 'laurelin_builds_total{status="succeeded"}' in scrape(client)


def test_scheduler_and_sync_metrics_exist_after_use(env):
    """Background work must be visible too — it is the part nobody watches."""
    from laurelin.core import scheduler
    from laurelin.core.models import ScheduleInfo

    _, store = env
    store.upsert_schedule(ScheduleInfo(
        name="s", trigger="cron", cron="0 2 * * *",
        next_run_at="2000-01-01T00:00:00+00:00",
    ))
    sched = scheduler.Scheduler(
        open_stores=lambda: [("ws", store, lambda s: "b1")], worker_id="a"
    )
    sched.tick()
    body = metrics.render().decode()
    assert 'laurelin_schedule_fires_total{status="succeeded",trigger="cron"}' in body
    assert "laurelin_scheduler_ticks_total" in body


# -- access control ---------------------------------------------------------------

def test_metrics_require_credentials_by_default(env):
    """Counts leak activity patterns, so scraping is credentialed unless the
    operator opts out for an unexposed port."""
    ws, _ = env
    app = create_app(ws)
    anon = TestClient(app)
    anon.post("/api/v1/auth/setup", json=CREDS)  # auth now on

    assert TestClient(app).get("/metrics").status_code == 401

    authed = TestClient(app)
    authed.post("/api/v1/auth/login", json=CREDS)
    assert authed.get("/metrics").status_code == 200


def test_metrics_can_be_made_public(env, monkeypatch):
    ws, _ = env
    monkeypatch.setenv("LAURELIN_METRICS_PUBLIC", "1")
    app = create_app(ws)
    TestClient(app).post("/api/v1/auth/setup", json=CREDS)
    assert TestClient(app).get("/metrics").status_code == 200


def test_metrics_can_be_disabled(client, monkeypatch):
    monkeypatch.setenv("LAURELIN_METRICS", "0")
    r = client.get("/metrics")
    assert r.status_code == 501
    assert "disabled" in r.json()["detail"].lower()


def test_noop_metrics_have_the_same_interface():
    """Instrumentation is written unconditionally, so the no-op fallback must
    accept every call the real metric does."""
    noop = metrics._NoOpMetric()
    noop.labels(a="b").inc()
    noop.labels(a="b").observe(1.5)
    noop.inc(2)
    noop.dec()
    noop.set(3)


# -- structured logging -----------------------------------------------------------

def test_json_formatter_emits_one_object_per_record():
    record = logging.LogRecord(
        "laurelin.test", logging.INFO, __file__, 1, "hello %s", ("world",), None
    )
    record.dataset = "orders"  # extra= fields ride along
    token = laurelin_logging.request_id.set("rid-1")
    try:
        payload = json.loads(laurelin_logging.JsonFormatter().format(record))
    finally:
        laurelin_logging.request_id.reset(token)

    assert payload["message"] == "hello world"
    assert payload["level"] == "info"
    assert payload["logger"] == "laurelin.test"
    assert payload["request_id"] == "rid-1"
    assert payload["dataset"] == "orders"


def test_json_formatter_includes_exceptions():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "l", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    payload = json.loads(laurelin_logging.JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]


def test_configure_is_opt_in_and_idempotent(monkeypatch):
    root = logging.getLogger()
    original = list(root.handlers)
    try:
        monkeypatch.delenv("LAURELIN_LOG_FORMAT", raising=False)
        laurelin_logging.configure()
        assert root.handlers == original, "plain logs stay the default"

        monkeypatch.setenv("LAURELIN_LOG_FORMAT", "json")
        laurelin_logging.configure()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, laurelin_logging.JsonFormatter)

        laurelin_logging.configure()  # twice must not stack handlers
        assert len(root.handlers) == 1
    finally:
        root.handlers = original
