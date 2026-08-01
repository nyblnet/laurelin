"""Query resource limits and admission control.

Without these, one deliberately expensive query degrades a whole replica for
everyone on it — and with federated datasets it can also run up a bill on
someone else's infrastructure. Three independent controls, because they fail
in different ways:

- **Memory.** DuckDB raises ``OutOfMemoryException`` at the limit instead of
  letting the process get OOM-killed, so an over-large query fails its own
  request rather than taking the replica down with it.
- **Time.** DuckDB has no statement timeout, so a watchdog thread calls
  ``interrupt()`` on the connection at the deadline. Precise and cooperative.
- **Concurrency.** A semaphore bounds how many queries run at once per
  process; past that, requests are refused quickly with ``Retry-After``
  rather than queueing until everything is slow.

Interactive queries and builds get different budgets: a dashboard panel that
runs for two minutes is broken, whereas a build that does is just a build.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

import duckdb

from laurelin.core import metrics


class QueryTimeout(RuntimeError):
    """A query exceeded its wall-clock budget."""


class QueryTooLarge(RuntimeError):
    """A query needed more memory than its budget allowed."""


class QueryRejected(RuntimeError):
    """Too many queries already running; this one was not admitted."""


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class QueryLimits:
    """One budget. ``timeout_s`` or ``max_concurrent`` of 0 disables that control."""

    memory_limit: str = "2GB"
    threads: Optional[int] = None
    temp_directory_size: str = "8GB"
    timeout_s: float = 60.0
    max_concurrent: int = 8

    @classmethod
    def interactive(cls) -> "QueryLimits":
        """Budget for anything a user is waiting on: workbench, dashboards,
        object queries, row pages."""
        threads = _env("LAURELIN_QUERY_THREADS", "")
        return cls(
            memory_limit=_env("LAURELIN_QUERY_MEMORY_LIMIT", "2GB"),
            threads=int(threads) if threads else None,
            temp_directory_size=_env("LAURELIN_QUERY_TEMP_LIMIT", "8GB"),
            timeout_s=float(_env("LAURELIN_QUERY_TIMEOUT", "60")),
            max_concurrent=int(_env("LAURELIN_MAX_CONCURRENT_QUERIES", "8")),
        )

    @classmethod
    def build(cls) -> "QueryLimits":
        """Budget for background work. Builds are allowed to be slow — they are
        not blocking a browser — but still must not exhaust the machine."""
        threads = _env("LAURELIN_BUILD_THREADS", "")
        return cls(
            memory_limit=_env("LAURELIN_BUILD_MEMORY_LIMIT", "4GB"),
            threads=int(threads) if threads else None,
            temp_directory_size=_env("LAURELIN_BUILD_TEMP_LIMIT", "32GB"),
            timeout_s=float(_env("LAURELIN_BUILD_TIMEOUT", "0")),  # 0 = no limit
            max_concurrent=0,  # builds are already bounded by the worker pool
        )


def apply(con: duckdb.DuckDBPyConnection, limits: QueryLimits) -> None:
    """Apply a budget to a connection. Best-effort: an older DuckDB missing a
    knob should not break the query it was meant to protect."""
    settings = [
        f"SET memory_limit='{limits.memory_limit}'",
        f"SET max_temp_directory_size='{limits.temp_directory_size}'",
    ]
    if limits.threads:
        settings.append(f"SET threads={int(limits.threads)}")
    for stmt in settings:
        try:
            con.execute(stmt)
        except duckdb.Error:
            pass


# One semaphore per process, sized on first use. Interactive queries share it,
# so a burst from one user cannot starve everyone else on this replica.
_gate_lock = threading.Lock()
_gate: Optional[threading.BoundedSemaphore] = None
_gate_size = 0


def _semaphore(size: int) -> Optional[threading.BoundedSemaphore]:
    global _gate, _gate_size
    if size <= 0:
        return None
    with _gate_lock:
        if _gate is None or _gate_size != size:
            _gate = threading.BoundedSemaphore(size)
            _gate_size = size
        return _gate


def reset_admission() -> None:
    """Drop the shared semaphore (tests change the configured size)."""
    global _gate, _gate_size
    with _gate_lock:
        _gate, _gate_size = None, 0


@contextmanager
def admit(limits: QueryLimits, wait_s: float = 2.0) -> Iterator[None]:
    """Take a concurrency slot, or refuse quickly.

    Waiting briefly absorbs bursts; waiting indefinitely just converts a
    throughput problem into a latency problem for everyone.
    """
    gate = _semaphore(limits.max_concurrent)
    if gate is None:
        yield
        return
    if not gate.acquire(timeout=wait_s):
        metrics.query_rejections.labels(reason="admission").inc()
        raise QueryRejected(
            f"{limits.max_concurrent} queries are already running on this "
            "replica. Retry shortly."
        )
    try:
        metrics.queries_in_flight.inc()
        yield
    finally:
        metrics.queries_in_flight.dec()
        gate.release()


@contextmanager
def guard(con: duckdb.DuckDBPyConnection, limits: QueryLimits) -> Iterator[None]:
    """Enforce the time budget around a query, and translate DuckDB's resource
    errors into ones the API layer can map to sensible status codes."""
    timer: Optional[threading.Timer] = None
    timed_out = threading.Event()

    if limits.timeout_s and limits.timeout_s > 0:
        def fire() -> None:
            timed_out.set()
            try:
                con.interrupt()
            except Exception:  # noqa: BLE001 - connection may already be closed
                pass

        timer = threading.Timer(limits.timeout_s, fire)
        timer.daemon = True
        timer.start()
    try:
        yield
    except duckdb.InterruptException as exc:
        if timed_out.is_set():
            metrics.query_rejections.labels(reason="timeout").inc()
            raise QueryTimeout(
                f"Query exceeded the {limits.timeout_s:g}s limit. Narrow it with "
                "a filter, an aggregate, or a smaller LIMIT."
            ) from exc
        raise
    except duckdb.OutOfMemoryException as exc:
        metrics.query_rejections.labels(reason="memory").inc()
        raise QueryTooLarge(
            f"Query needed more than the {limits.memory_limit} memory budget. "
            "Narrow it with a filter, an aggregate, or a smaller LIMIT."
        ) from exc
    finally:
        if timer is not None:
            timer.cancel()


# -- ClickHouse ---------------------------------------------------------------
#
# ClickHouse enforces both budgets itself, per query, so there is no watchdog
# thread here: `apply`/`guard` above are DuckDB-typed (con.interrupt(),
# duckdb.InterruptException) and have no ClickHouse equivalent. What is needed
# instead is a settings string and a translation of ClickHouse's error codes,
# because without it a timeout and an out-of-memory both flatten into the
# generic 400 at routes.py and lose the 408/413 distinction that route
# deliberately preserves.

def _bytes_of(limit: str) -> int:
    """A DuckDB-style memory limit ('2GB') as bytes, for max_memory_usage."""
    text = limit.strip().upper().replace("IB", "B")
    scale = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    for suffix, factor in scale.items():
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * factor)
    return int(float(text.rstrip("B") or 0))


def clickhouse_settings(limits: QueryLimits) -> str:
    """Query-level ``SETTINGS`` for a governed ClickHouse statement.

    Query-level settings override whatever profile the engine was started
    with — that is the point of putting them here rather than configuring the
    engine once.

    ``transform_null_in=0`` is a *semantic* pin rather than a budget: with it
    on, a NULL in the value list would match a NULL in the policy column, and a
    row whose tenant is merely unknown would start being admitted. Laurelin's
    renderer never emits a NULL literal (policy values are strings), so today
    this is defence in depth against a future renderer, not a live fix — but
    the cost of pinning it is one clause and the cost of not pinning it is a
    silent semantics change under someone else's server profile.

    ``schema_inference_make_columns_nullable=0`` is deliberately never set: it
    turns NULL into '', which then hashes to e3b0c44298fc1c14 — a real-looking
    digest for data that is absent.
    """
    parts = ["transform_null_in=0"]
    if limits.timeout_s and limits.timeout_s > 0:
        parts.append(f"max_execution_time={limits.timeout_s:g}")
    parts.append(f"max_memory_usage={_bytes_of(limits.memory_limit)}")
    return ", ".join(parts)


@contextmanager
def clickhouse_guard(limits: QueryLimits) -> Iterator[None]:
    """Translate ClickHouse's resource errors into Laurelin's.

    Codes are matched, not messages: 159 is TIMEOUT_EXCEEDED and 241 is
    MEMORY_LIMIT_EXCEEDED, both measured against chdb 4.2.1.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - chdb raises bare RuntimeError
        text = str(exc)
        if "Code: 159" in text or "TIMEOUT_EXCEEDED" in text:
            metrics.query_rejections.labels(reason="timeout").inc()
            raise QueryTimeout(
                f"Query exceeded the {limits.timeout_s:g}s limit. Narrow it with "
                "a filter, an aggregate, or a smaller LIMIT."
            ) from exc
        if "Code: 241" in text or "MEMORY_LIMIT_EXCEEDED" in text:
            metrics.query_rejections.labels(reason="memory").inc()
            raise QueryTooLarge(
                f"Query needed more than the {limits.memory_limit} memory budget. "
                "Narrow it with a filter, an aggregate, or a smaller LIMIT."
            ) from exc
        raise


@contextmanager
def clickhouse_limited(limits: Optional[QueryLimits] = None) -> Iterator[None]:
    """Admission control plus error translation for one ClickHouse statement.

    ``admit`` is engine-agnostic — it bounds how many queries this *process*
    runs at once — so it is reused unchanged; the budgets themselves ride
    along in the statement's SETTINGS clause.
    """
    limits = limits or QueryLimits.interactive()
    with admit(limits), clickhouse_guard(limits):
        yield


@contextmanager
def limited(
    con: duckdb.DuckDBPyConnection,
    limits: Optional[QueryLimits] = None,
    admission: bool = True,
) -> Iterator[None]:
    """Apply a budget, take a concurrency slot, and guard the deadline."""
    limits = limits or QueryLimits.interactive()
    apply(con, limits)
    if admission:
        with admit(limits), guard(con, limits):
            yield
    else:
        with guard(con, limits):
            yield
