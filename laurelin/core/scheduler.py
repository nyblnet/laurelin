"""The scheduler — what turns a platform you drive into one that runs.

A schedule binds a **trigger** to an **action**:

- ``cron`` fires on a clock; ``upstream`` fires when a dataset gains a version,
  so a pipeline can follow its inputs instead of guessing when they land.
- ``build`` runs the transform DAG (optionally specific targets); ``sync``
  pulls one connector source.

**Exactly once across replicas.** Every replica polls, but firing requires
winning a conditional ``UPDATE`` — the same lease primitive that build
coordination uses. A replica that dies mid-run has its claim expire rather
than wedging the schedule forever.

**No catch-up storms.** A schedule overdue by a day fires *once*, not once per
missed window. Backfilling is a deliberate act, not something a restart should
trigger on your behalf.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from laurelin.core import metrics
from laurelin.core.models import ScheduleInfo, utcnow_iso

log = logging.getLogger("laurelin.scheduler")

DEFAULT_POLL_SECONDS = 15.0


class ScheduleError(ValueError):
    """A schedule definition that cannot be honoured."""


def next_fire(cron: str, after: Optional[datetime] = None) -> str:
    """The next time a cron expression fires, as an ISO timestamp."""
    try:
        from croniter import croniter
    except ImportError as exc:  # pragma: no cover - optional extra
        raise ScheduleError(
            "Cron schedules need croniter: pip install 'laurelin[scheduler]'"
        ) from exc
    base = after or datetime.now(timezone.utc)
    try:
        return croniter(cron, base).get_next(datetime).isoformat()
    except Exception as exc:  # noqa: BLE001 - croniter raises its own types
        raise ScheduleError(f"Invalid cron expression {cron!r}: {exc}") from exc


def validate(info: ScheduleInfo) -> None:
    """Reject an unrunnable schedule at definition time rather than at 2am."""
    if info.trigger not in ("cron", "upstream"):
        raise ScheduleError(
            f"Unknown trigger {info.trigger!r}: expected 'cron' or 'upstream'"
        )
    if info.trigger == "cron":
        if not info.cron.strip():
            raise ScheduleError("A cron schedule needs a cron expression")
        next_fire(info.cron)  # raises if malformed
    elif not info.upstream_dataset.strip():
        raise ScheduleError("An upstream schedule needs an upstream_dataset")

    if info.action not in ("build", "sync"):
        raise ScheduleError(
            f"Unknown action {info.action!r}: expected 'build' or 'sync'"
        )
    if info.action == "sync" and not info.source.strip():
        raise ScheduleError("A sync action needs a source")


class Scheduler:
    """Polls one or more workspaces and fires whatever is due.

    ``open_stores`` yields ``(label, store, run_action)`` so the same loop
    serves a single workspace and a multi-workspace server without knowing
    which it is in.
    """

    def __init__(
        self,
        open_stores: Callable[[], list],
        worker_id: str,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ):
        self.open_stores = open_stores
        self.worker_id = worker_id
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="laurelin-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
                log.exception("scheduler tick failed")
            self._stop.wait(self.poll_seconds)

    # -- one pass -------------------------------------------------------------

    def tick(self) -> list[str]:
        """Fire everything currently due. Returns the names fired."""
        metrics.scheduler_ticks.inc()
        fired: list[str] = []
        for label, store, run_action in self.open_stores():
            try:
                fired.extend(self._tick_store(label, store, run_action))
            except Exception:  # noqa: BLE001 - one workspace must not stop others
                log.exception("scheduler failed for %s", label)
        return fired

    def _tick_store(self, label: str, store, run_action) -> list[str]:
        fired = []
        for schedule in store.due_schedules(utcnow_iso()):
            if schedule.trigger == "upstream" and not self._upstream_advanced(
                store, schedule
            ):
                continue
            if not store.claim_schedule(schedule.name, self.worker_id):
                continue  # another replica got there first
            fired.append(schedule.name)
            self._run(store, schedule, run_action)
        return fired

    @staticmethod
    def _upstream_advanced(store, schedule: ScheduleInfo) -> bool:
        """True when the watched dataset has a version we haven't acted on."""
        dataset = store.get_dataset(schedule.upstream_dataset)
        if dataset is None or dataset.latest_version is None:
            return False
        return schedule.watermark is None or dataset.latest_version > schedule.watermark

    def _run(self, store, schedule: ScheduleInfo, run_action) -> None:
        """Execute the action and record the outcome.

        The claim is always released — a schedule that fails must still be able
        to fire in its next window, otherwise one bad night silently disables
        the pipeline.
        """
        next_at = None
        if schedule.trigger == "cron":
            try:
                next_at = next_fire(schedule.cron)
            except ScheduleError:
                next_at = None  # recorded below; the schedule stops firing

        watermark = None
        if schedule.trigger == "upstream":
            dataset = store.get_dataset(schedule.upstream_dataset)
            watermark = dataset.latest_version if dataset else None

        try:
            build_id = run_action(schedule)
            store.record_schedule_run(
                schedule.name, "succeeded", next_run_at=next_at,
                build_id=build_id, watermark=watermark,
            )
            metrics.schedule_fires.labels(
                trigger=schedule.trigger, status="succeeded"
            ).inc()
            store.log_audit(
                "schedule_fired",
                {"schedule": schedule.name, "action": schedule.action,
                 "build_id": build_id},
                actor="scheduler",
            )
        except Exception as exc:  # noqa: BLE001 - any action failure
            store.record_schedule_run(
                schedule.name, "failed", next_run_at=next_at,
                error=f"{type(exc).__name__}: {exc}"[:500], watermark=watermark,
            )
            metrics.schedule_fires.labels(
                trigger=schedule.trigger, status="failed"
            ).inc()
            store.log_audit(
                "schedule_failed",
                {"schedule": schedule.name, "error": str(exc)[:300]},
                actor="scheduler",
            )
            log.warning("schedule %r failed: %s", schedule.name, exc)


def enabled() -> bool:
    """Whether this process should run the scheduler.

    On by default: a platform that needs a separate process to honour its own
    schedules is a platform that will silently not honour them. Set
    ``LAURELIN_SCHEDULER=0`` to run a dedicated scheduler elsewhere.
    """
    return os.environ.get("LAURELIN_SCHEDULER", "1") != "0"


def poll_seconds() -> float:
    return float(os.environ.get("LAURELIN_SCHEDULER_POLL", DEFAULT_POLL_SECONDS))
