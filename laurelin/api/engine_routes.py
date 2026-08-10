"""Delegated engine routes.

An engine is a Flight SQL endpoint — Trino, Dremio, Databricks — that a
`@remote_transform` submits SQL to. Laurelin runs no cluster; it stores the
reduced result with lineage and policy intact.

Admin-only: an engine URI carries credentials and points the server at a remote
system, exactly like a connector source. Configs are validated before they are
stored (a bad URI fails at PUT, not at first build) and redacted in every
response.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from laurelin.api.routes import ADMIN, ActorDep, StoreDep
from laurelin.core import engines, redaction

engines_router = APIRouter(tags=["engines"])
_log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class EngineUpsertRequest(BaseModel):
    type: str = "flightsql"
    uri: str = ""
    options: dict[str, str] = Field(default_factory=dict)


@engines_router.get("/engines", dependencies=[ADMIN])
def list_engines(store: StoreDep) -> list[dict]:
    """Every configured engine, with credentials redacted."""
    return [
        engines.EngineConfig(
            name=e["name"], type=e["type"], uri=e["uri"], options=e["options"]
        ).redacted()
        | {"created_at": e["created_at"], "created_by": e["created_by"]}
        for e in store.list_engines()
    ]


@engines_router.put("/engines/{name}", dependencies=[ADMIN])
def upsert_engine(
    name: str, body: EngineUpsertRequest, store: StoreDep, actor: ActorDep
) -> dict:
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid engine name {name!r}: must match ^[a-z][a-z0-9_-]{{0,63}}$",
        )
    config = {"type": body.type, "uri": body.uri, "options": body.options}
    try:
        engines.validate_engine(config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    existing = store.get_engine(name)
    store.upsert_engine(
        name, body.type, body.uri, body.options,
        created_by=existing["created_by"] if existing else actor,
    )
    store.log_audit(
        "engine_updated" if existing else "engine_created",
        {"engine": name, "type": body.type},
        actor=actor,
    )
    saved = store.get_engine(name)
    assert saved is not None
    return engines.EngineConfig(
        name=saved["name"], type=saved["type"], uri=saved["uri"],
        options=saved["options"],
    ).redacted()


@engines_router.post("/engines/{name}/test", dependencies=[ADMIN])
def test_engine(name: str, store: StoreDep) -> dict:
    """Probe connectivity with a trivial query.

    A "does it work" button matters more here than anywhere else in the
    product: an engine's whole job is to be reachable, and the alternative to
    testing it here is discovering it's misconfigured when a scheduled build
    fails at 2am.
    """
    e = store.get_engine(name)
    if e is None:
        raise HTTPException(status_code=404, detail=f"Engine not found: {name!r}")
    config = engines.EngineConfig(
        name=e["name"], type=e["type"], uri=e["uri"], options=e["options"]
    )
    try:
        client = engines.connect(config, timeout_s=15.0)
        try:
            client.query("SELECT 1")
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 - report any failure to the operator
        # `_redact_uri` used to run over this whole string, which only ever
        # worked by accident: it is a driver's prose, not a URI. `redact_text`
        # masks the DSNs it can parse and withholds the message whole when the
        # rest of it still looks like a credential — a Flight SQL
        # "unauthenticated: invalid token <token>" is exactly that shape.
        #
        # The unredacted exception goes to the server log, because withholding
        # it from the browser is a disclosure decision, not a decision to
        # destroy the operator's only diagnostic.
        _log.warning("engine %r failed its connectivity test: %s", name, exc)
        detail = redaction.redact_text(str(exc))
        return {"ok": False, "detail": detail,
                "withheld": detail == redaction.WITHHELD}
    return {"ok": True}


@engines_router.delete("/engines/{name}", dependencies=[ADMIN])
def delete_engine(name: str, store: StoreDep, actor: ActorDep) -> dict:
    if not store.delete_engine(name):
        raise HTTPException(status_code=404, detail=f"Engine not found: {name!r}")
    store.log_audit("engine_deleted", {"engine": name}, actor=actor)
    return {"deleted": name}
