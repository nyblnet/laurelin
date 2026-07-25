"""Query resource limits and admission control.

Without these one expensive query degrades a whole replica, and with federated
datasets it can run up a bill on someone else's infrastructure too. Three
independent controls with different failure modes: memory, time, concurrency.
"""

import threading
import time

import duckdb
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.catalog import DatasetCatalog
from laurelin.core import limits
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore


@pytest.fixture(autouse=True)
def _clean_gate():
    limits.reset_admission()
    yield
    limits.reset_admission()


# -- configuration --------------------------------------------------------------

def test_limits_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("LAURELIN_QUERY_MEMORY_LIMIT", "512MB")
    monkeypatch.setenv("LAURELIN_QUERY_TIMEOUT", "5")
    monkeypatch.setenv("LAURELIN_MAX_CONCURRENT_QUERIES", "3")
    monkeypatch.setenv("LAURELIN_QUERY_THREADS", "2")

    q = limits.QueryLimits.interactive()
    assert (q.memory_limit, q.timeout_s, q.max_concurrent, q.threads) == ("512MB", 5.0, 3, 2)

    # Builds get a separate, looser budget: they aren't blocking a browser.
    b = limits.QueryLimits.build()
    assert b.timeout_s == 0 and b.max_concurrent == 0


def test_apply_is_best_effort(monkeypatch):
    """A knob an older DuckDB lacks must not break the query it protects."""
    con = duckdb.connect()
    limits.apply(con, limits.QueryLimits(memory_limit="not-a-size"))
    assert con.execute("SELECT 1").fetchone()[0] == 1


# -- memory ---------------------------------------------------------------------

def test_memory_limit_raises_instead_of_exhausting_the_process():
    con = duckdb.connect()
    budget = limits.QueryLimits(memory_limit="100MB", timeout_s=0, max_concurrent=0)
    limits.apply(con, budget)
    with pytest.raises(limits.QueryTooLarge, match="memory budget"):
        with limits.guard(con, budget):
            con.execute(
                "SELECT i, repeat('x', 200) FROM range(20000000) t(i) ORDER BY 2, 1"
            ).fetchall()


# -- time -----------------------------------------------------------------------

def test_timeout_interrupts_a_runaway_query():
    con = duckdb.connect()
    budget = limits.QueryLimits(timeout_s=0.5, max_concurrent=0)
    started = time.perf_counter()
    with pytest.raises(limits.QueryTimeout, match="0.5s limit"):
        with limits.guard(con, budget):
            con.execute(
                "SELECT count(*) FROM range(20000000000) t(i) WHERE i % 7 = 0"
            ).fetchall()
    assert time.perf_counter() - started < 10, "must not run to completion"


def test_timeout_zero_disables_the_watchdog():
    con = duckdb.connect()
    with limits.guard(con, limits.QueryLimits(timeout_s=0, max_concurrent=0)):
        assert con.execute("SELECT 42").fetchone()[0] == 42


def test_a_manual_interrupt_is_not_reported_as_a_timeout():
    """Only the watchdog's own interrupt becomes QueryTimeout; anything else
    propagates, so a real error isn't mislabelled."""
    con = duckdb.connect()
    budget = limits.QueryLimits(timeout_s=30, max_concurrent=0)
    threading.Timer(0.3, con.interrupt).start()
    with pytest.raises(duckdb.InterruptException):
        with limits.guard(con, budget):
            con.execute(
                "SELECT count(*) FROM range(20000000000) t(i) WHERE i % 7 = 0"
            ).fetchall()


# -- concurrency ----------------------------------------------------------------

def test_admission_refuses_past_the_limit():
    budget = limits.QueryLimits(max_concurrent=2)
    with limits.admit(budget):
        with limits.admit(budget):
            with pytest.raises(limits.QueryRejected, match="already running"):
                with limits.admit(budget, wait_s=0.1):
                    pass
    # Slots are released on exit, so the next caller is admitted.
    with limits.admit(budget, wait_s=0.1):
        pass


def test_admission_waits_briefly_for_a_slot():
    budget = limits.QueryLimits(max_concurrent=1)
    released = threading.Event()

    def hold():
        with limits.admit(budget):
            time.sleep(0.3)
        released.set()

    threading.Thread(target=hold, daemon=True).start()
    time.sleep(0.05)
    with limits.admit(budget, wait_s=3.0):  # waits for the holder to finish
        assert released.is_set()


def test_admission_disabled_when_zero():
    budget = limits.QueryLimits(max_concurrent=0)
    with limits.admit(budget), limits.admit(budget), limits.admit(budget):
        pass


def test_slot_is_released_when_the_query_raises():
    budget = limits.QueryLimits(max_concurrent=1)
    with pytest.raises(RuntimeError):
        with limits.admit(budget):
            raise RuntimeError("boom")
    with limits.admit(budget, wait_s=0.1):  # not leaked
        pass


# -- through the API ------------------------------------------------------------

CREDS = {"username": "root", "password": "trustno1!"}


@pytest.fixture()
def client(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="lim")
    store = MetadataStore(ws.metadata_path)
    DatasetCatalog(ws, store).write("t", pa.table({"id": list(range(100))}))
    c = TestClient(create_app(ws, no_auth=True))
    return c


def test_timeout_surfaces_as_504(client, monkeypatch):
    monkeypatch.setenv("LAURELIN_QUERY_TIMEOUT", "0.5")
    r = client.post("/api/v1/query", json={
        "sql": "SELECT count(*) FROM range(20000000000) x(i) WHERE i % 7 = 0"})
    assert r.status_code == 504, r.text
    assert "limit" in r.json()["detail"]


def test_rejection_surfaces_as_503_with_retry_after(client, monkeypatch):
    monkeypatch.setenv("LAURELIN_MAX_CONCURRENT_QUERIES", "1")
    limits.reset_admission()
    budget = limits.QueryLimits.interactive()

    # Hold the only slot, then a query must be refused rather than queued.
    with limits.admit(budget):
        r = client.post("/api/v1/query", json={"sql": "SELECT 1"})
    assert r.status_code == 503, r.text
    assert r.headers.get("Retry-After") == "2"


def test_ordinary_errors_are_still_400(client):
    """Resource limits must not flatten syntax errors into the wrong code."""
    r = client.post("/api/v1/query", json={"sql": "SELECT * FROM nonexistent"})
    assert r.status_code == 400


def test_normal_queries_are_unaffected(client):
    r = client.post("/api/v1/query", json={"sql": "SELECT count(*) AS n FROM t"})
    assert r.status_code == 200 and r.json()["rows"][0]["n"] == 100


# -- audit log growth -----------------------------------------------------------

def test_prune_audit_keeps_the_most_recent(tmp_path):
    store = MetadataStore(tmp_path / "m.db")
    for i in range(50):
        store.log_audit("event", {"i": i})

    assert store.prune_audit(0) == 0, "pruning is opt-in"
    removed = store.prune_audit(10)
    assert removed == 40

    kept = store.list_audit(limit=100)
    assert len(kept) == 10
    assert [e.details["i"] for e in kept] == list(range(49, 39, -1))
    assert store.prune_audit(10) == 0  # idempotent


def test_prune_audit_noop_when_under_the_limit(tmp_path):
    store = MetadataStore(tmp_path / "m.db")
    store.log_audit("only", {})
    assert store.prune_audit(100) == 0
    assert len(store.list_audit()) == 1
