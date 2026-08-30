"""Schedule routes.

Editor-gated, like builds: a schedule runs pipeline code, so creating one is a
write to the pipeline surface rather than a read. Definitions are validated on
save, so a bad cron expression fails at PUT rather than silently never firing.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from laurelin.api.routes import (
    EDITOR,
    ActorDep,
    StoreDep,
    WorkspaceDep,
    _dump,
    get_registry,
)
from laurelin.core import authoring_hints, scheduler
from laurelin.core.failure import safe_detail
from laurelin.core.models import ScheduleInfo, utcnow_iso

schedules_router = APIRouter(tags=["schedules"])

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class ScheduleUpsertRequest(BaseModel):
    enabled: bool = True
    trigger: str = "cron"
    cron: str = ""
    upstream_dataset: str = ""
    action: str = "build"
    targets: list[str] = Field(default_factory=list)
    source: str = ""


def _with_build_outcome(store: StoreDep, dumped: dict) -> dict:
    """`last_status` as the reader needs it, not as the recorder wrote it.

    The scheduler records "succeeded" when a build-action firing *queues* its
    build — correct for the firing, and a lie on the page: the row said
    succeeded, green, all night, while the build it launched failed on an
    expectation a hundred milliseconds later. The truthful outcome already
    exists on the builds table, so project through it at read time: a
    pending/running build reads as "running", and the terminal states read as
    themselves. Nothing stored changes; sync actions (which run inline) and
    plan failures (which record "failed" with no build) pass through untouched.
    """
    build_id = dumped.get("last_build_id")
    if dumped.get("action") == "build" and dumped.get("last_status") and build_id:
        build = store.get_build(build_id)
        if build is not None:
            status = str(build.status.value)
            dumped["last_status"] = (
                "running" if status in ("pending", "running") else status
            )
    return dumped


@schedules_router.get("/schedules", dependencies=[EDITOR])
def list_schedules(store: StoreDep) -> list[dict]:
    return [_with_build_outcome(store, _dump(s)) for s in store.list_schedules()]


@schedules_router.get("/schedules/{name}", dependencies=[EDITOR])
def get_schedule(name: str, store: StoreDep) -> dict:
    info = store.get_schedule(name)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Schedule not found: {name!r}")
    return _with_build_outcome(store, _dump(info))


@schedules_router.put("/schedules/{name}", dependencies=[EDITOR])
def upsert_schedule(
    name: str, body: ScheduleUpsertRequest, store: StoreDep, actor: ActorDep,
    workspace: WorkspaceDep,
) -> dict:
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid schedule name {name!r}: must match ^[a-z][a-z0-9_-]{{0,63}}$",
        )
    # Every free-form field, not just `targets`. A target is stored and
    # returned verbatim and the schedules editor writes it back, which is why
    # this gate exists — and `source` and `upstream_dataset` sit beside it in
    # the same row, are dumped by the same `_dump`, and had no gate at all.
    # Measured: the exact string this gate was written to stop,
    # `s3://key:SCHEDSEKRET@bucket/t`, was refused as a target and accepted as
    # a `source`, then handed back in full by GET /schedules. A guard on one
    # field of a record is a guard on none of it.
    #
    # R2 demoted this from a 400 to a warning. Every ScheduleInfo field is
    # OPERATIONAL and both schedule routes are editor-gated, so a credential
    # pasted here is no longer readable one privilege level down — which is
    # what the gate existed to prevent. What is left is an authoring hint, and
    # `authoring_hints.credential_in_free_text` is explicitly not a boundary.
    checked = [("target", target) for target in body.targets]
    checked += [("source", body.source), ("upstream dataset", body.upstream_dataset)]
    warnings = [
        {"field": label,
         "hint": f"this {label} looks like it embeds a credential. Put it in a "
                 "registered source or an object-store profile and name that here."}
        for label, value in checked
        if authoring_hints.credential_in_free_text(value)
    ]
    # Referent existence, warned rather than refused, in the same save-time
    # posture as cron validation: a bad cron 400s at PUT, but a typo'd source
    # or a target no transform produces used to save silently and surface only
    # when the schedule fired and the queued run failed — hours later, in a
    # build log nobody was watching. Warnings, not 400s, because a schedule
    # may legitimately be authored before its flow (import order) — but never
    # silently.
    if body.action == "sync" and body.source.strip():
        if store.get_source(body.source.strip()) is None:
            warnings.append({
                "field": "source",
                "hint": f"no source named {body.source.strip()!r} is registered; "
                        "this schedule will fail when it fires unless one is "
                        "created first.",
            })
    if body.trigger == "upstream" and body.upstream_dataset.strip():
        if store.get_dataset(body.upstream_dataset.strip()) is None:
            warnings.append({
                "field": "upstream dataset",
                "hint": f"no dataset named {body.upstream_dataset.strip()!r} "
                        "exists; this schedule will never fire until one does.",
            })
    if body.action == "build" and body.targets:
        try:
            registry = get_registry(workspace)
        except HTTPException:
            # A broken pipeline file must not block schedule authoring; the
            # registry 409 is already reported by every graph-touching route.
            registry = None
        if registry is not None:
            unknown = [t for t in body.targets if registry.by_output(t) is None]
            if unknown:
                # "pipeline", the settled word — this sentence renders in the
                # UI's warning box, and it used to mix three retired nouns
                # ("transform", "flow", "pipeline") for the same concept.
                warnings.append({
                    "field": "target",
                    "hint": "no pipeline produces "
                            + ", ".join(repr(t) for t in unknown)
                            + "; builds queued by this schedule will fail until "
                            "a pipeline with that output exists.",
                })
    existing = store.get_schedule(name)
    info = ScheduleInfo(
        name=name,
        enabled=body.enabled,
        trigger=body.trigger,
        cron=body.cron.strip(),
        upstream_dataset=body.upstream_dataset.strip(),
        action=body.action,
        targets=body.targets,
        source=body.source.strip(),
        created_at=existing.created_at if existing else utcnow_iso(),
        created_by=existing.created_by if existing else actor,
        watermark=existing.watermark if existing else None,
    )
    try:
        scheduler.validate(info)
    except scheduler.ScheduleError as exc:
        raise HTTPException(status_code=400, detail=safe_detail(exc, subject="schedule"))

    # A cron schedule needs its first firing time, or it would never be due.
    if info.trigger == "cron" and info.enabled:
        info.next_run_at = scheduler.next_fire(info.cron)

    store.upsert_schedule(info)
    store.log_audit(
        "schedule_updated" if existing else "schedule_created",
        {"schedule": name, "trigger": info.trigger, "action": info.action},
        actor=actor,
    )
    saved = store.get_schedule(name)
    assert saved is not None
    return _dump(saved) | {"warnings": warnings}


@schedules_router.delete("/schedules/{name}", dependencies=[EDITOR])
def delete_schedule(name: str, store: StoreDep, actor: ActorDep) -> dict:
    if not store.delete_schedule(name):
        raise HTTPException(status_code=404, detail=f"Schedule not found: {name!r}")
    store.log_audit("schedule_deleted", {"schedule": name}, actor=actor)
    return {"deleted": name}


@schedules_router.post("/schedules/{name}/run", dependencies=[EDITOR])
def run_schedule_now(name: str, store: StoreDep, actor: ActorDep) -> dict:
    """Fire a schedule immediately, without waiting for its window.

    Makes the schedule due; the scheduler picks it up on its next poll, so the
    run goes through exactly the same claim-and-record path as a natural
    firing rather than a parallel one that could behave differently.
    """
    info = store.get_schedule(name)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Schedule not found: {name!r}")
    if not info.enabled:
        raise HTTPException(
            status_code=409, detail=f"Schedule {name!r} is disabled"
        )
    info.next_run_at = utcnow_iso()
    if info.trigger == "upstream":
        info.watermark = None  # treat the current version as unseen
    store.upsert_schedule(info)
    store.log_audit("schedule_run_requested", {"schedule": name}, actor=actor)
    return {"queued": name, "due_at": info.next_run_at}
