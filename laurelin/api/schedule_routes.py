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
    _dump,
)
from laurelin.core import scheduler
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


@schedules_router.get("/schedules", dependencies=[EDITOR])
def list_schedules(store: StoreDep) -> list[dict]:
    return [_dump(s) for s in store.list_schedules()]


@schedules_router.get("/schedules/{name}", dependencies=[EDITOR])
def get_schedule(name: str, store: StoreDep) -> dict:
    info = store.get_schedule(name)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Schedule not found: {name!r}")
    return _dump(info)


@schedules_router.put("/schedules/{name}", dependencies=[EDITOR])
def upsert_schedule(
    name: str, body: ScheduleUpsertRequest, store: StoreDep, actor: ActorDep
) -> dict:
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid schedule name {name!r}: must match ^[a-z][a-z0-9_-]{{0,63}}$",
        )
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
        raise HTTPException(status_code=400, detail=str(exc))

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
    return _dump(saved)


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
