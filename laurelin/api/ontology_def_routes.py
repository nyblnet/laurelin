"""Ontology definition authoring routes (admin).

The ontology stays YAML-on-disk — ``load_ontology(workspace.ontology_dir)`` is
the single read path, executed per request — so these routes write files
through ``laurelin.ontology.authoring`` rather than inventing a second source
of truth. The write is governed because the *route* is governed: ADMIN role
gate, server-resolved actor in the audit row, and conflict refusal before a
duplicate api_name can brick the loader for the whole workspace.

This is also the surface the MCP authoring tools call. There is deliberately
no ontology lock flag: definitions are declarative data validated by closed
pydantic models — no expression language, no execution — the same posture as
markings and policies, which are strictly more powerful and have no lock.
"""

from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from laurelin.api.routes import (
    ADMIN,
    ActorDep,
    StoreDep,
    WorkspaceDep,
    _dump,
)
from laurelin.core.models import (
    ActionDef,
    ActionKind,
    ActionParameterDef,
    Cardinality,
    LinkTypeDef,
    ObjectTypeDef,
    PropertyDef,
)
from laurelin.ontology import load_ontology
from laurelin.ontology.authoring import (
    DefinitionConflict,
    delete_definition,
    upsert_definition,
)

ontology_def_router = APIRouter(tags=["ontology-authoring"])

# api_name doubles as the managed file's name, so the charset is a security
# boundary (no separators, no dots), not a style preference.
_API_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _check_api_name(api_name: str) -> None:
    if not _API_NAME_RE.match(api_name):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid api_name {api_name!r}: must match ^[a-z][a-z0-9_]*$ (max 64)",
        )


def _conflict(exc: DefinitionConflict) -> HTTPException:
    # DefinitionConflict messages are Laurelin-authored and name only the
    # filename inside ontology/, never a server path.
    return HTTPException(status_code=409, detail=str(exc))


class ObjectTypeUpsertRequest(BaseModel):
    backing_dataset: str
    primary_key: str
    properties: dict[str, PropertyDef] = Field(default_factory=dict)
    display_name: Optional[str] = None
    description: str = ""
    title_property: Optional[str] = None


class LinkTypeUpsertRequest(BaseModel):
    from_type: str
    to_type: str
    from_property: str
    to_property: str
    cardinality: Cardinality = Cardinality.one_to_many
    display_name: Optional[str] = None


class ActionUpsertRequest(BaseModel):
    object_type: str
    kind: ActionKind
    parameters: dict[str, ActionParameterDef] = Field(default_factory=dict)
    display_name: Optional[str] = None
    description: str = ""


# ---------------------------------------------------------------------------
# Object types
# ---------------------------------------------------------------------------

@ontology_def_router.put("/ontology/object-types/{api_name}", dependencies=[ADMIN])
def upsert_object_type(
    api_name: str,
    body: ObjectTypeUpsertRequest,
    workspace: WorkspaceDep,
    store: StoreDep,
    actor: ActorDep,
) -> dict:
    """Create or update an object type definition. ADMIN.

    The backing dataset must already exist (404 otherwise — migration order
    puts datasets first). Property names missing from the backing dataset's
    current schema are returned as ``warnings``, not refused: columns may
    arrive with a later build.
    """
    _check_api_name(api_name)
    if store.get_dataset(body.backing_dataset) is None:
        raise KeyError(f"Dataset not found: {body.backing_dataset!r}")
    model = ObjectTypeDef(
        api_name=api_name,
        backing_dataset=body.backing_dataset,
        primary_key=body.primary_key,
        properties=body.properties,
        display_name=body.display_name,
        description=body.description,
        title_property=body.title_property,
    )
    warnings: list[str] = []
    versions = store.list_versions(body.backing_dataset)
    if versions:
        latest = max(versions, key=lambda v: v.version)
        columns = {c.name for c in latest.schema_}
        wanted = set(body.properties) | {body.primary_key}
        if body.title_property:
            wanted.add(body.title_property)
        for missing in sorted(wanted - columns):
            warnings.append(
                f"Property {missing!r} is not a column of dataset "
                f"{body.backing_dataset!r} (latest version); it will read as "
                "empty until a build provides it"
            )
    try:
        upsert_definition(workspace.ontology_dir, "object_type", model)
    except DefinitionConflict as exc:
        raise _conflict(exc) from None
    store.log_audit(
        "object_type_written",
        {"object_type": api_name, "backing_dataset": body.backing_dataset,
         "properties": sorted(body.properties)},
        actor=actor,
    )
    return {"object_type": _dump(model), "warnings": warnings}


@ontology_def_router.delete("/ontology/object-types/{api_name}", dependencies=[ADMIN])
def delete_object_type(
    api_name: str, workspace: WorkspaceDep, store: StoreDep, actor: ActorDep
) -> dict:
    """Delete an API-managed object type definition. ADMIN.

    Refuses (409) to remove a definition living in a hand-written ontology
    file. Any ontology grants recorded for this type are deliberately left in
    place: they are inert once the type is gone, and deleting a grant record
    silently is how audit trails lie.
    """
    _check_api_name(api_name)
    try:
        delete_definition(workspace.ontology_dir, "object_type", api_name)
    except DefinitionConflict as exc:
        raise _conflict(exc) from None
    store.log_audit("object_type_deleted", {"object_type": api_name}, actor=actor)
    return {"deleted": api_name}


# ---------------------------------------------------------------------------
# Link types
# ---------------------------------------------------------------------------

@ontology_def_router.put("/ontology/link-types/{api_name}", dependencies=[ADMIN])
def upsert_link_type(
    api_name: str,
    body: LinkTypeUpsertRequest,
    workspace: WorkspaceDep,
    store: StoreDep,
    actor: ActorDep,
) -> dict:
    """Create or update a link type between two existing object types. ADMIN.

    Both endpoint object types must already be defined (404 otherwise), so
    author object types before their links.
    """
    _check_api_name(api_name)
    ontology = load_ontology(workspace.ontology_dir)
    for endpoint in (body.from_type, body.to_type):
        if ontology.object_type(endpoint) is None:
            raise KeyError(f"Unknown object type: {endpoint!r}")
    model = LinkTypeDef(
        api_name=api_name,
        from_type=body.from_type,
        to_type=body.to_type,
        from_property=body.from_property,
        to_property=body.to_property,
        cardinality=body.cardinality,
        display_name=body.display_name,
    )
    try:
        upsert_definition(workspace.ontology_dir, "link_type", model)
    except DefinitionConflict as exc:
        raise _conflict(exc) from None
    store.log_audit(
        "link_type_written",
        {"link_type": api_name, "from": body.from_type, "to": body.to_type},
        actor=actor,
    )
    return {"link_type": _dump(model)}


@ontology_def_router.delete("/ontology/link-types/{api_name}", dependencies=[ADMIN])
def delete_link_type(
    api_name: str, workspace: WorkspaceDep, store: StoreDep, actor: ActorDep
) -> dict:
    """Delete an API-managed link type definition. ADMIN. Refuses (409) to
    touch hand-written ontology files."""
    _check_api_name(api_name)
    try:
        delete_definition(workspace.ontology_dir, "link_type", api_name)
    except DefinitionConflict as exc:
        raise _conflict(exc) from None
    store.log_audit("link_type_deleted", {"link_type": api_name}, actor=actor)
    return {"deleted": api_name}


# ---------------------------------------------------------------------------
# Action types
# ---------------------------------------------------------------------------

@ontology_def_router.put("/ontology/action-types/{api_name}", dependencies=[ADMIN])
def upsert_action_type(
    api_name: str,
    body: ActionUpsertRequest,
    workspace: WorkspaceDep,
    store: StoreDep,
    actor: ActorDep,
) -> dict:
    """Create or update a write-back action on an existing object type. ADMIN.

    ``kind`` is one of create/update/delete; the object type must already be
    defined (404 otherwise).
    """
    _check_api_name(api_name)
    ontology = load_ontology(workspace.ontology_dir)
    if ontology.object_type(body.object_type) is None:
        raise KeyError(f"Unknown object type: {body.object_type!r}")
    model = ActionDef(
        api_name=api_name,
        object_type=body.object_type,
        kind=body.kind,
        parameters=body.parameters,
        display_name=body.display_name,
        description=body.description,
    )
    try:
        upsert_definition(workspace.ontology_dir, "action", model)
    except DefinitionConflict as exc:
        raise _conflict(exc) from None
    store.log_audit(
        "action_type_written",
        {"action": api_name, "object_type": body.object_type,
         "kind": body.kind.value},
        actor=actor,
    )
    return {"action": _dump(model)}


@ontology_def_router.delete("/ontology/action-types/{api_name}", dependencies=[ADMIN])
def delete_action_type(
    api_name: str, workspace: WorkspaceDep, store: StoreDep, actor: ActorDep
) -> dict:
    """Delete an API-managed action definition. ADMIN. Refuses (409) to touch
    hand-written ontology files."""
    _check_api_name(api_name)
    try:
        delete_definition(workspace.ontology_dir, "action", api_name)
    except DefinitionConflict as exc:
        raise _conflict(exc) from None
    store.log_audit("action_type_deleted", {"action": api_name}, actor=actor)
    return {"deleted": api_name}
