"""Object app routes.

An app is a *curated* view over one object type — the columns that matter, the
filters that scope it, the handful of actions an operator should reach for.
It adds no data access of its own: reading an app's objects goes through the
ordinary ontology endpoints, so the composed object-type permission (ontology
grant AND backing-dataset access, then markings and row policy) applies
exactly as it does everywhere else.

Definitions are validated against the live ontology on save, so an app can't
name a property or action that doesn't exist and fail later in front of a user.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from laurelin.api.routes import (
    ADMIN,
    VIEWER,
    ActorDep,
    OntologyDep,
    PermDep,
    StoreDep,
    UserDep,
    _dump,
    _require_ot_view,
)
from laurelin.core.models import ObjectAppInfo, utcnow_iso

apps_router = APIRouter(tags=["apps"])

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class ObjectAppRequest(BaseModel):
    title: str = ""
    description: str = ""
    object_type: str
    columns: list[str] = Field(default_factory=list)
    filters: dict[str, str] = Field(default_factory=dict)
    search_placeholder: str = ""
    actions: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)


@apps_router.get("/apps", dependencies=[VIEWER])
def list_apps(store: StoreDep, service: OntologyDep, perms: PermDep, user: UserDep) -> list[dict]:
    """Apps whose object type this user may see.

    An app is a presentation of an object type, so it inherits that type's
    visibility — listing one the caller can't open would leak the shape of the
    ontology.
    """
    out = []
    for app in store.list_object_apps():
        ot = service.ontology.object_type(app.object_type)
        backing = ot.backing_dataset if ot is not None else ""
        if ot is None or not perms.object_type_permission(user, app.object_type, backing)[0]:
            continue
        out.append(_dump(app))
    return out


@apps_router.get("/apps/{name}", dependencies=[VIEWER])
def get_app(
    name: str, store: StoreDep, service: OntologyDep, perms: PermDep, user: UserDep
) -> dict:
    app = store.get_object_app(name)
    if app is None:
        raise HTTPException(status_code=404, detail=f"App not found: {name!r}")
    # Same 403 as opening the object type directly — an app is not a side door.
    _require_ot_view(perms, user, service, app.object_type)
    return _dump(app)


@apps_router.put("/apps/{name}", dependencies=[ADMIN])
def upsert_app(
    name: str,
    body: ObjectAppRequest,
    store: StoreDep,
    service: OntologyDep,
    actor: ActorDep,
) -> dict:
    """Define an app. Admin-only: it shapes what a whole team sees."""
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid app name {name!r}: must match ^[a-z][a-z0-9_-]{{0,63}}$",
        )
    ot = service.ontology.object_type(body.object_type)
    if ot is None:
        raise HTTPException(
            status_code=400, detail=f"Unknown object type: {body.object_type!r}"
        )

    declared = set(ot.properties) | {ot.primary_key}
    for column in body.columns:
        if column not in declared:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown property {column!r} on object type {body.object_type!r}",
            )
    for prop in body.filters:
        if prop not in declared:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown filter property {prop!r} on {body.object_type!r}",
            )
    available = {
        a.api_name for a in service.ontology.actions
        if a.object_type == body.object_type
    }
    for action in body.actions:
        if action not in available:
            raise HTTPException(
                status_code=400,
                detail=f"Action {action!r} is not defined on {body.object_type!r}",
            )
    for link in body.links:
        lt = service.ontology.link_type(link)
        if lt is None or body.object_type not in (lt.from_type, lt.to_type):
            raise HTTPException(
                status_code=400,
                detail=f"Link {link!r} does not involve {body.object_type!r}",
            )

    existing = store.get_object_app(name)
    info = ObjectAppInfo(
        name=name,
        title=body.title.strip() or name,
        description=body.description.strip(),
        object_type=body.object_type,
        columns=body.columns,
        filters=body.filters,
        search_placeholder=body.search_placeholder.strip(),
        actions=body.actions,
        links=body.links,
        created_at=existing.created_at if existing else utcnow_iso(),
        created_by=existing.created_by if existing else actor,
        updated_at=utcnow_iso(),
    )
    store.upsert_object_app(info)
    store.log_audit(
        "app_updated" if existing else "app_created",
        {"app": name, "object_type": body.object_type},
        actor=actor,
    )
    return _dump(info)


@apps_router.delete("/apps/{name}", dependencies=[ADMIN])
def delete_app(name: str, store: StoreDep, actor: ActorDep) -> dict:
    if not store.delete_object_app(name):
        raise HTTPException(status_code=404, detail=f"App not found: {name!r}")
    store.log_audit("app_deleted", {"app": name}, actor=actor)
    return {"deleted": name}
