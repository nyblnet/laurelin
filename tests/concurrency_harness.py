"""Shared machinery for the concurrency-invariant tests.

Not collected by pytest (no ``test_`` prefix). The rules this module enforces,
because a bad concurrency test is worse than none:

- **Assert invariants, never timings.** Every helper returns observed values;
  the tests assert counts, set-equalities and monotonicity over them.
- **Force the race, don't hope for it.** ``run_racers`` releases N threads from
  one barrier immediately before the contended call; ``pause_hook`` parks a
  thread at an exact point *between* store calls so an interleaving is produced
  deterministically rather than probabilistically.
- **Fail, never hang.** Barrier and join timeouts exceed SQLite's 30 s busy
  timeout (laurelin/core/backend.py:311 ``sqlite3.connect(..., timeout=30)``),
  so a genuine deadlock surfaces as a test failure instead of a silent CI hang.

Threads get independent database connections for free: every ``MetadataStore``
method opens and commits its own connection (db.py ``_conn``). For the same
reason SQLite stores used here MUST be real files (``tmp_path``), never
``:memory:`` — per-connection memory databases would hand every thread its own
empty database and every racer would "win" a race that never happened.
"""

from __future__ import annotations

import itertools
import os
import threading
import uuid
from contextlib import contextmanager

import pytest

PG_URL = os.environ.get("LAURELIN_TEST_POSTGRES", "")

BACKENDS = ["sqlite", "postgres"]


def rounds(default: int) -> int:
    """How many times a racy test repeats. Defaults are sized to keep the whole
    suite acceptable in a normal run; soak runs set LAURELIN_SOAK_ROUNDS=500."""
    return int(os.environ.get("LAURELIN_SOAK_ROUNDS", default))


def open_store(backend: str, tmp_path):
    """Generator fixture body: yields a MetadataStore on the requested backend,
    tearing down the throwaway Postgres schema afterwards (the test_horizontal
    pattern). Postgres skips when LAURELIN_TEST_POSTGRES is unset."""
    from laurelin.core.db import MetadataStore

    if backend == "sqlite":
        yield MetadataStore(tmp_path / "meta.db")
        return
    if not PG_URL:
        pytest.skip("set LAURELIN_TEST_POSTGRES=<url> to run the Postgres half")
    from laurelin.core.backend import PostgresBackend

    schema = f"race_{uuid.uuid4().hex[:10]}"
    store = MetadataStore(PG_URL, schema=schema)
    try:
        yield store
    finally:
        with store._conn() as c:
            c.execute(f"DROP SCHEMA IF EXISTS {PostgresBackend.quote_ident(schema)} CASCADE")


def run_racers(n: int, fn, *, timeout: float = 90.0):
    """Run ``fn(i)`` in n threads, all released from one barrier immediately
    before the call. Returns (results, errors): per-thread lists where exactly
    one of the pair is non-None per index (unless fn returned None).

    Joins with a deadline and *fails* on stragglers rather than hanging: the
    timeout exceeds SQLite's 30 s busy timeout so a real deadlock reports.
    """
    barrier = threading.Barrier(n, timeout=30)
    results: list = [None] * n
    errors: list = [None] * n

    def runner(i: int) -> None:
        try:
            barrier.wait()
            results[i] = fn(i)
        except Exception as exc:  # noqa: BLE001 - collected for the assertion
            errors[i] = exc

    threads = [
        threading.Thread(target=runner, args=(i,), name=f"racer-{i}", daemon=True)
        for i in range(n)
    ]
    for t in threads:
        t.start()
    import time

    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    stuck = [t.name for t in threads if t.is_alive()]
    if stuck:
        pytest.fail(
            f"racer threads did not finish within {timeout}s: {stuck} — "
            "a held lock nothing releases (deadlock), not a slow machine"
        )
    return results, errors


class Gate:
    """A two-event rendezvous used by pause_hook: the hooked thread signals
    ``reached`` and parks until the test ``open()``s it."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()

    def wait_reached(self, timeout: float = 30.0) -> None:
        assert self.reached.wait(timeout), "hooked call was never reached"

    def open(self) -> None:
        self.release.set()


@contextmanager
def pause_hook(
    holder,
    attr: str,
    *,
    only_thread_named: str | None = None,
    nth_call: int | None = None,
    before: bool = False,
    park_timeout: float = 60.0,
):
    """Wrap ``holder.attr`` (an instance method or module function) so the
    calling thread signals the returned Gate and parks until released —
    *after* the real call by default, or ``before`` it.

    ``only_thread_named`` / ``nth_call`` select which invocation parks, so one
    racer can be frozen while others (and the main thread) pass through.

    Rule: hooks may only park BETWEEN store calls, never inside a held
    connection — every MetadataStore method opens and commits its own
    connection, which is what makes parking here deadlock-free.
    """
    gate = Gate()
    real = getattr(holder, attr)
    counter = itertools.count()

    def park() -> None:
        gate.reached.set()
        if not gate.release.wait(park_timeout):
            raise TimeoutError(f"pause_hook on {attr!r} was never released")

    def wrapper(*args, **kwargs):
        i = next(counter)
        eligible = (
            only_thread_named is None
            or threading.current_thread().name == only_thread_named
        ) and (nth_call is None or i == nth_call)
        if eligible and before:
            park()
        out = real(*args, **kwargs)
        if eligible and not before:
            park()
        return out

    setattr(holder, attr, wrapper)
    try:
        yield gate
    finally:
        gate.release.set()  # never leave a parked thread behind on failure
        setattr(holder, attr, real)
