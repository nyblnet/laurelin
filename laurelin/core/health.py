"""Data health: a deterministic read over records Laurelin already keeps.

Nothing here is a new source of truth. ``dataset_health`` derives one
:class:`~laurelin.core.models.DatasetHealth` per dataset from builds, build
tasks, dataset versions, expectation results, schedules and sources — the
records the rest of the platform already writes — and ``evaluate_and_alert``
turns *transitions* of that derivation into events and (opt-in) webhooks.

Two invariants, stated here because the code below enforces both:

* **The rollup never names a dataset its caller cannot view.** Filtering is the
  route's job (``perms.viewable_datasets``, the GET /datasets precedent) — this
  module computes for the names it is given and no more.

* **An alert payload is exactly the viewer-level serialization of
  ``DatasetHealth``.** The webhook path calls ``serialize.dump_as(record,
  Role.viewer)`` — the same mechanical projection the API applies to an
  anonymous-est reader — so an alert can never carry what a viewer could not
  read through ``GET /health/datasets``. No hand-written field filter, no
  vigilance: the mechanism.

Honest asymmetry, on the scheduler piggyback: **a dead scheduler cannot
webhook about itself.** But ``overdue`` is computed at *read* time from
``next_run_at`` — which only advances when ``record_schedule_run`` runs — so a
dead scheduler is visible in-app the moment anyone looks at the health page.
That is the strongest guarantee a single process can make about its own death.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from laurelin.core import serialize
from laurelin.core.failure import Failure, FailureCode, Phase
from laurelin.core.models import (
    BuildStatus,
    DatasetHealth,
    ExpectationSummary,
    HealthStatus,
    Role,
    ScheduleInfo,
    utcnow_iso,
)

log = logging.getLogger("laurelin.health")

# Events that alerting knows about. Recovery is its own event so a webhook
# consumer can close what it opened.
RED_EVENTS = {
    HealthStatus.failing: "dataset_failing",
    HealthStatus.overdue: "dataset_overdue",
    HealthStatus.stale: "dataset_stale",
}
RECOVERY_EVENT = "dataset_healthy"
WEBHOOK_TIMEOUT_SECONDS = 10.0


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class HealthService:
    """Derives per-dataset health and runs the edge-triggered alert pass."""

    def __init__(self, store, poll_seconds: float = 15.0):
        self.store = store
        # Slack before a quiet schedule counts as overdue: two poll windows,
        # floored at 300s so a tight dev poll interval does not flap.
        self.overdue_slack = max(2 * poll_seconds, 300.0)

    # -- derivation -----------------------------------------------------------

    def dataset_health(self, names: list[str]) -> dict[str, DatasetHealth]:
        """Health for exactly ``names`` (already filtered by the caller)."""
        # The same idempotent healer POST /builds runs: an expired lease reads
        # as a structured Failure, never as an eternal `running`.
        try:
            self.store.reap_expired_builds()
        except Exception:  # noqa: BLE001 - health must not fail on the healer
            log.exception("reap_expired_builds failed during health derivation")

        now = datetime.now(timezone.utc)
        latest_tasks = self.store.latest_build_tasks_by_dataset()
        schedules = self.store.list_schedules()
        sources_by_dataset = {s.dataset: s for s in self.store.list_sources()}
        out: dict[str, DatasetHealth] = {}
        for name in names:
            out[name] = self._one(
                name, now, latest_tasks.get(name), schedules, sources_by_dataset.get(name)
            )
        return out

    def _one(
        self, name: str, now: datetime, latest_task: Optional[dict],
        schedules: list[ScheduleInfo], source,
    ) -> DatasetHealth:
        ds = self.store.get_dataset(name)
        version = self.store.get_version(name) if ds is not None else None
        last_success_at = version.created_at if version is not None else None

        task = latest_task["task"] if latest_task else None
        build_id = latest_task["build_id"] if latest_task else None
        # A reaped build (dead replica; reap_expired_builds) is failed on the
        # builds row while its task rows still say `running` — the reaper
        # cannot know which task the worker died inside. A task whose parent
        # build failed underneath it therefore counts as failed, carrying the
        # build's Failure (REMOTE_FAILED) when the task recorded none.
        build_failed = (
            task is not None
            and latest_task.get("build_status") == BuildStatus.failed.value
            and task.status != BuildStatus.succeeded
        )

        failing_exps: list[ExpectationSummary] = []
        detail: dict = {}
        if task is not None:
            exp_detail = []
            for e in task.expectations:
                if not e.get("passed", True):
                    failing_exps.append(ExpectationSummary(
                        name=str(e.get("expectation", e.get("name", ""))),
                        column=str(e.get("column", "") or ""),
                        severity=str(e.get("severity", "error")),
                        passed=False,
                    ))
                    # Editor-and-above detail: the editor-authored message and
                    # the measured value ride OPERATIONAL, never PRESENTATION.
                    exp_detail.append({
                        "expectation": e.get("expectation", e.get("name", "")),
                        "message": e.get("message", ""),
                        "measured": e.get("measured"),
                    })
            if exp_detail:
                detail["expectations"] = exp_detail
            detail["transform"] = task.transform_name

        # Schedules that target this dataset. Build schedules with an empty
        # target list build everything, so they target every dataset.
        overdue = False
        schedule_failed: list[ScheduleInfo] = []
        last_scheduled_run_at = None
        for sched in schedules:
            if not sched.enabled:
                continue
            targets_this = (
                (sched.action == "build" and (not sched.targets or name in sched.targets))
                or (sched.action == "sync" and source is not None
                    and sched.source == source.name)
            )
            if not targets_this:
                continue
            if sched.last_run_at and (
                last_scheduled_run_at is None or sched.last_run_at > last_scheduled_run_at
            ):
                last_scheduled_run_at = sched.last_run_at
            if self._schedule_overdue(sched, now):
                overdue = True
                detail.setdefault("overdue_schedules", []).append(sched.name)
            # S3: a schedule whose last firing failed *before a build existed*
            # (a plan refusal — nothing to queue, so no build task ever
            # carries it) used to leave this rollup all-clear while the
            # Schedules page showed the row red. Schedule *names* are
            # editor-and-above, so they ride `detail` exactly like
            # overdue_schedules; a viewer learns "failing", not which
            # schedule. Failures that DID queue a build are already counted
            # through the dataset's latest build task, so this looks only at
            # the recorded firing outcome.
            if sched.action == "build" and sched.last_status == "failed":
                schedule_failed.append(sched)
                detail.setdefault("schedule_run_failed", []).append(sched.name)

        sync_failing = source is not None and source.last_sync_status == "failed"
        if source is not None:
            detail["source"] = source.name

        # Priority order. failing > overdue > stale > unknown > healthy.
        failure: Optional[Failure] = None
        exp_error = any(e.severity == "error" for e in failing_exps)
        task_failed = task is not None and (
            task.status == BuildStatus.failed or build_failed
        )
        if task_failed:
            failure = task.failure or latest_task.get("build_failure")
        elif schedule_failed and schedule_failed[0].last_failure is not None:
            # Re-subjected like the sync branch below, and for the same
            # reason: `last_failure` is PRESENTATION and its stored subject
            # is "schedule:<name>" — a schedule name a viewer must not read.
            failure = schedule_failed[0].last_failure.model_copy(
                update={"subject": f"dataset:{name}"}
            )
        elif sync_failing and source.last_sync_failure is not None:
            # Re-subjected: the recorded failure's subject is "source:<name>",
            # and a source *name* is editor-and-above on this record (it rides
            # in `detail`). `last_failure` is PRESENTATION and a Failure
            # projects to {code, subject} for a viewer — so the subject here
            # must name the thing the viewer is looking at, not the source
            # behind it. Caught by
            # test_schedule_and_source_names_do_not_reach_a_viewer.
            failure = source.last_sync_failure.model_copy(
                update={"subject": f"dataset:{name}"}
            )

        fresh_within = ds.expected_fresh_seconds if ds is not None else None
        stale = False
        if fresh_within is not None:
            success_dt = _parse_iso(last_success_at)
            # Declared-but-never-built is stale too: the declaration is a
            # promise, and a promise nobody has kept yet is not "unknown".
            stale = success_dt is None or (now - success_dt) > timedelta(seconds=fresh_within)

        if task_failed or exp_error or sync_failing or schedule_failed:
            status = HealthStatus.failing
        elif overdue:
            status = HealthStatus.overdue
        elif stale:
            status = HealthStatus.stale
        elif last_success_at is None and task is None and fresh_within is None:
            # No versions, no build history, no declaration: an ad-hoc dataset
            # is not "stale", it is undeclared. Reporting it red trains
            # operators to ignore red.
            status = HealthStatus.unknown
        else:
            status = HealthStatus.healthy

        return DatasetHealth(
            dataset=name,
            status=status,
            last_success_at=last_success_at,
            # `failed`, not the task row's stranded `running`, when the parent
            # build was reaped: "an expired lease reads as failed, never as an
            # eternal running" is the whole point of running the healer first.
            last_build_status=(
                BuildStatus.failed if build_failed
                else task.status if task is not None else None
            ),
            last_build_id=build_id,
            last_failure=failure,
            failing_expectations=failing_exps,
            expected_fresh_within=fresh_within,
            schedule_overdue=overdue,
            last_scheduled_run_at=last_scheduled_run_at,
            sync_failing=sync_failing,
            detail=detail,
        )

    def _schedule_overdue(self, sched: ScheduleInfo, now: datetime) -> bool:
        """Overdue = the trigger should have fired and nothing recorded a run.

        ``next_run_at`` only advances via ``record_schedule_run``, so a dead
        scheduler process trips the same predicate as a failing one — the
        detection needs no live thread.
        """
        if sched.trigger == "cron":
            next_dt = _parse_iso(sched.next_run_at)
            if next_dt is None:
                return False
            return (now - next_dt) > timedelta(seconds=self.overdue_slack)
        if sched.trigger == "upstream":
            upstream = self.store.get_dataset(sched.upstream_dataset)
            if upstream is None or upstream.latest_version is None:
                return False
            behind = upstream.latest_version - (sched.watermark or 0)
            # More than one build cycle behind: one pending version is the
            # normal in-flight window, two says nothing is consuming them.
            return behind > 1
        return False

    # -- alerting (edge-triggered) --------------------------------------------

    def evaluate_and_alert(self, post=None) -> list[str]:
        """One alert pass: derive, diff against persisted state, fire edges.

        ``post`` injects the HTTP delivery for tests; default is a plain
        urllib POST with a 10s timeout. Returns the events fired.
        """
        names = [d.name for d in self.store.list_datasets()]
        current = self.dataset_health(names)
        previous = self.store.get_health_states()
        fired: list[str] = []
        now = utcnow_iso()
        for name, record in current.items():
            prev_status = previous.get(name, (None, None))[0]
            status = record.status.value
            if prev_status == status:
                continue
            self.store.set_health_state(name, status, now)
            event = None
            if record.status in RED_EVENTS:
                event = RED_EVENTS[record.status]
            elif (
                record.status == HealthStatus.healthy
                and prev_status in {s.value for s in RED_EVENTS}
            ):
                event = RECOVERY_EVENT
            if event is None:
                continue  # unknown<->healthy churn is not an alert
            self.store.add_health_event(name, event, status, now)
            fired.append(event)
            self._deliver(event, record, now, post=post)
        return fired

    def _payload(self, event: str, record: DatasetHealth, at: str) -> dict:
        """THE invariant: the payload is the viewer-level dump of the record.

        ``dump_as(..., Role.viewer)`` — never the ambient ContextVar, which a
        future caller could have raised — so `message`, `measured`, row counts,
        cursor values, schedule/source names and driver text are all withheld
        by the same mechanism the API uses, not by a hand-kept list here.
        """
        body = serialize.dump_as(record, Role.viewer)
        return {
            "event": event,
            "at": at,
            # A path, never an absolute URL: the payload must not teach the
            # recipient where the server lives.
            "link": "/health",
            **body,
        }

    def _deliver(self, event: str, record: DatasetHealth, at: str, post=None) -> None:
        """POST the event to every enabled webhook watching this dataset."""
        webhooks = [w for w in self.store.list_alert_webhooks() if w["enabled"]]
        if not webhooks:
            return
        payload = self._payload(event, record, at)
        for hook in webhooks:
            if hook["datasets"] and record.dataset not in hook["datasets"]:
                continue
            if hook["events"] and event not in hook["events"]:
                continue
            self.post_webhook(hook["name"], hook["url"], payload, post=post)

    def post_webhook(self, name: str, url: str, payload: dict, post=None) -> Optional[int]:
        """Deliver one payload; record status; never raise, never store the
        response body (attacker-controlled text aimed at the audit trail)."""
        now = utcnow_iso()
        try:
            if post is not None:
                status = post(url, payload)
            else:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_SECONDS) as resp:
                    status = resp.status
            self.store.record_webhook_delivery(name, now, int(status), None)
            return int(status)
        except Exception as exc:  # noqa: BLE001 - a dead endpoint must not kill the tick
            failure = Failure.from_exception(
                exc,
                code=FailureCode.ENDPOINT_UNREACHABLE,
                phase=Phase.execute,
                subject=f"webhook:{name}",
                driver="urllib",
            )
            self.store.record_webhook_delivery(name, now, None, failure)
            log.warning("alert webhook %s delivery failed", name)
            return None
