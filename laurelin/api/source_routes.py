"""Data-source (connector) routes.

Management (create/delete) is admin-only: postgres/http configs can embed
credentials and the file connector reads the server's filesystem. Editors can
list sources and trigger syncs (subject to edit access on the target dataset).

That split — **admin writes, editor reads** — is a privilege crossing, and R2
draws it through the middle of ``SourceInfo`` rather than around the route. An
editor gets the Laurelin-owned facts (name, connector kind, target dataset, last
sync time and status, and a structured ``Failure`` when it went wrong) and does
not get ``config`` at all. An admin gets ``config``, still run through
``redacted_config``.
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
from laurelin.connectors.connectors import _sync_failure
from laurelin.core import serialize
from laurelin.core.failure import safe_detail
from laurelin.core.models import Role, SourceInfo, utcnow_iso

sources_router = APIRouter(tags=["sources"])

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class SourceUpsertRequest(BaseModel):
    type: str
    dataset: str
    config: dict[str, Any] = Field(default_factory=dict)


def _public(source: SourceInfo, role: Role) -> dict:
    """A source as an API response.

    `SourceInfo` is admin-authored and editor-read, so `serialize.dump` already
    withholds `config` from an editor — the audience annotation does the work
    and this function only has to put the config *back* for the admin who wrote
    it. `redacted_config` still runs there, because "the person who typed it"
    and "the person reading this screen" are not necessarily the same admin.

    Note what is deliberately NOT here: a "redacted config" for editors. The old
    shape ran `redact_mapping`, which was then a denylist over key names in the
    *connector's* vocabulary, and round 3 read an ODBC keyword string out of a
    `path` key that no denylist covers. Not disclosing the dict is not a better
    denylist; it is the absence of one. (Task #54 has since inverted
    `redact_mapping` itself to an allowlist. That is why it is still tolerable
    on the admin branch above — it is not why editors are refused the dict, and
    the two decisions are independent.)
    """
    out = _dump(source)
    if role.covers(Role.admin):
        out["config"] = redacted_config(source.config)
    return out


@sources_router.get("/sources", dependencies=[EDITOR])
def list_sources(store: StoreDep, user: UserDep) -> list[dict]:
    return [_public(s, user.role) for s in store.list_sources()]


@sources_router.get("/sources/{name}", dependencies=[EDITOR])
def get_source(name: str, store: StoreDep, user: UserDep) -> dict:
    source = store.get_source(name)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Source not found: {name!r}")
    return _public(source, user.role)


@sources_router.put("/sources/{name}", dependencies=[ADMIN])
def upsert_source(
    name: str, body: SourceUpsertRequest, store: StoreDep, actor: ActorDep,
    user: UserDep,
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
        # A name, a connector type and a dataset name: three first-party facts
        # that an editor who owns sources has to be able to see.
        min_read_role=Role.editor,
    )
    saved = store.get_source(name)
    assert saved is not None
    return _public(saved, user.role)


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
        raise HTTPException(status_code=400, detail=safe_detail(exc, subject="source"))
    except Exception as exc:  # connection/parse failures from the external system
        # `sync_source` has already classified and stored this failure; this is
        # the second copy, and it is the one that reached the browser. Both are
        # the same Laurelin-authored record now, which is the point: two places
        # read by two routes at two privilege levels cannot disagree about what
        # a failure says.
        failure = _sync_failure(exc, source)
        # `detail_for`, not `render()`. This route has NO `dependencies=[...]`:
        # its only gate is `_require_dataset_edit`, and `permissions._evaluate`
        # grants edit from an explicit dataset grant regardless of role, or from
        # `role.covers(Role.editor)` when a dataset has no grants at all. So a
        # plain editor always reaches it, and a viewer holding a `can_edit`
        # grant does too — and both read
        #     "The host for source:crm could not be resolved at
        #      secret-db.internal.corp:55999. (psycopg/OperationalError/…)"
        # while the same editor's `GET /sources/crm` correctly carries no
        # `config` at all. `HTTPException(detail=...)` never passes through
        # `serialize.dump`, so R2 had no jurisdiction here until this call.
        # Reach includes MCP: `sync_source` is an exposed tool and
        # `mcp/client.py` surfaces a 502 `detail` verbatim into `LaurelinError`.
        raise HTTPException(
            status_code=502, detail=serialize.detail_for(failure, Role.admin)
        ) from None
    return _dump(info)
