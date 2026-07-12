"""Data-source (connector) routes.

Management (create/delete) is admin-only: postgres/http configs can embed
credentials and the file connector reads the server's filesystem. Editors can
list sources and trigger syncs (subject to edit access on the target dataset);
secret-bearing config values are redacted in every response.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from laurelin.api.routes import (
    ADMIN,
    EDITOR,
    ActorDep,
    CatalogDep,
    PermDep,
    StoreDep,
    UserDep,
    _dump,
    _require_dataset_edit,
)
from laurelin.connectors import redacted_config, sync_source, validate_source
from laurelin.core.models import SourceInfo, utcnow_iso

sources_router = APIRouter(tags=["sources"])

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class SourceUpsertRequest(BaseModel):
    type: str
    dataset: str
    config: dict[str, Any] = Field(default_factory=dict)


def _public(source: SourceInfo) -> dict:
    out = _dump(source)
    out["config"] = redacted_config(source.config)
    return out


@sources_router.get("/sources", dependencies=[EDITOR])
def list_sources(store: StoreDep) -> list[dict]:
    return [_public(s) for s in store.list_sources()]


@sources_router.get("/sources/{name}", dependencies=[EDITOR])
def get_source(name: str, store: StoreDep) -> dict:
    source = store.get_source(name)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Source not found: {name!r}")
    return _public(source)


@sources_router.put("/sources/{name}", dependencies=[ADMIN])
def upsert_source(
    name: str, body: SourceUpsertRequest, store: StoreDep, actor: ActorDep
) -> dict:
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid source name {name!r}: must match ^[a-z][a-z0-9_]*$",
        )
    if not _NAME_RE.match(body.dataset):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid dataset name {body.dataset!r}: must match ^[a-z][a-z0-9_]*$",
        )
    validate_source(body.type, body.config)  # ValueError -> 400 via handler
    existing = store.get_source(name)
    info = SourceInfo(
        name=name,
        type=body.type,
        dataset=body.dataset,
        config=body.config,
        created_at=existing.created_at if existing else utcnow_iso(),
        created_by=existing.created_by if existing else actor,
    )
    store.upsert_source(info)
    store.log_audit(
        "source_updated" if existing else "source_created",
        {"source": name, "type": body.type, "dataset": body.dataset},
        actor=actor,
    )
    saved = store.get_source(name)
    assert saved is not None
    return _public(saved)


@sources_router.delete("/sources/{name}", dependencies=[ADMIN])
def delete_source(name: str, store: StoreDep, actor: ActorDep) -> dict:
    if not store.delete_source(name):
        raise HTTPException(status_code=404, detail=f"Source not found: {name!r}")
    store.log_audit("source_deleted", {"source": name}, actor=actor)
    return {"deleted": name}


@sources_router.post("/sources/{name}/sync")
def sync_source_route(
    name: str,
    store: StoreDep,
    catalog: CatalogDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
) -> dict:
    source = store.get_source(name)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Source not found: {name!r}")
    _require_dataset_edit(perms, user, source.dataset)
    try:
        info = sync_source(catalog, store, source, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # connection/parse failures from the external system
        raise HTTPException(
            status_code=502, detail=f"Sync failed: {type(exc).__name__}: {exc}"
        )
    return _dump(info)
