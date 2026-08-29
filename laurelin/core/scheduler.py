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
from laurelin.core.failure import Failure, FailureCode, Phase
from laurelin.core.models import Role, ScheduleInfo, utcnow_iso

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
            try:
                # Health alerting piggybacks the existing loop: no new daemon,
                # no new lease machinery. Edge-triggered against persisted
                # state, so overlapping replicas re-firing is bounded by the
                # health_state upsert, and a restart does not re-alert every
                # red dataset. Honest asymmetry, stated: a dead scheduler
                # cannot webhook about itself — but the health PAGE computes
                # `overdue` at read time from next_run_at, so a dead scheduler
                # is visible in-app the moment anyone looks.
                self._alert(store)
            except Exception:  # noqa: BLE001 - alerting must not stop scheduling
                log.exception("health alert pass failed for %s", label)
        return fired

    def _alert(self, store) -> None:
        from laurelin.core.health import HealthService

        HealthService(store, poll_seconds=self.poll_seconds).evaluate_and_alert()

    def _tick_store(self, label: str, store, run_action) -> list[str]:
        fired = []
        for schedule in store.due_schedules(utcnow_iso()):
            if schedule.trigger == "upstream" and not self._upstream_advanced(
                store, schedule
            ):
                continue
            if not store.claim_schedule(schedule.name, self.worker_id):
                continue  # another replica got there first
            # The due list is a stale snapshot: between our poll and our claim
            # another replica may have served the entire window (its record
            # released the claim, so winning the claim alone proves nothing
            # about the window). claim_schedule re-checks cron due-ness in
            # SQL; upstream due-ness is a watermark comparison, so re-read the
            # schedule under the claim and re-verify before firing — without
            # this, two overlapping polls fire the action twice per window.
            name = schedule.name
            schedule = store.get_schedule(name)
            if schedule is None or not self._still_due(store, schedule):
                store.release_schedule_claim(name, self.worker_id)
                continue
            fired.append(name)
            self._run(store, schedule, run_action)
        return fired

    def _still_due(self, store, schedule: ScheduleInfo) -> bool:
        """Due-ness re-evaluated on freshly read state, AFTER winning the
        claim. Holding the claim makes this stable: no other replica can
        serve the window while we decide."""
        if not schedule.enabled:
            return False
        if schedule.trigger == "upstream":
            return self._upstream_advanced(store, schedule)
        return (
            schedule.next_run_at is not None
            and schedule.next_run_at <= utcnow_iso()
        )

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
                build_id=build_id, watermark=watermark, worker=self.worker_id,
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
            failure = Failure.from_exception(
                exc, code=FailureCode.TRANSFORM_FAILED, phase=Phase.execute,
                subject=f"schedule:{schedule.name}", driver="python",
            )
            store.record_schedule_run(
                schedule.name, "failed", next_run_at=next_at,
                failure=failure, watermark=watermark, worker=self.worker_id,
            )
            metrics.schedule_fires.labels(
                trigger=schedule.trigger, status="failed"
            ).inc()
            store.log_audit(
                "schedule_failed",
                # The projection, not the record: a schedule's failure can be a
                # connector's, and `endpoint` is rebuilt from an admin's config.
                # See `Failure.audit_projection`.
                {"schedule": schedule.name, "failure": failure.audit_projection()},
                actor="scheduler",
                min_read_role=Role.editor,
            )


# `_redacted_failure` lived here and is deleted. It loaded the schedule's
# source config so it could substring-replace that source's password out of the
# driver's sentence before storing it — which is a correct implementation of a
# doomed idea. Round 3 walked around it because a psycopg message can quote a
# password back *re-escaped*, so the substring the config holds is not the
# substring in the message. The scheduler now records a `Failure`; there is no
# sentence to scrub.
#
# Note what else this removes: the scheduler is a *recorder*, not a logger, and
# the old code had to know that `connectors.sync_source` re-raises the
# unredacted original. That coupling is gone — a Failure is safe wherever it
# lands.


def enabled() -> bool:
    """Whether this process should run the scheduler.

    On by default: a platform that needs a separate process to honour its own
    schedules is a platform that will silently not honour them. Set
    ``LAURELIN_SCHEDULER=0`` to run a dedicated scheduler elsewhere.
    """
    return os.environ.get("LAURELIN_SCHEDULER", "1") != "0"


def poll_seconds() -> float:
    return float(os.environ.get("LAURELIN_SCHEDULER_POLL", DEFAULT_POLL_SECONDS))
