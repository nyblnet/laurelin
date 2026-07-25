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
