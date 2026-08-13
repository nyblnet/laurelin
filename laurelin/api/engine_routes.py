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
from laurelin.core import engines, serialize
from laurelin.core.failure import (
    Failure,
    FailureCode,
    Phase,
    probe_endpoint,
    safe_detail,
)
from laurelin.core.roles import Role

# The pre-flight verdicts that mean "this endpoint cannot be used at all", as
# opposed to "the socket was fine and something later went wrong".
_UNUSABLE_ENDPOINT = frozenset({
    FailureCode.CREDENTIAL_MALFORMED,
    FailureCode.ENDPOINT_UNRESOLVABLE,
    FailureCode.ENDPOINT_UNREACHABLE,
    FailureCode.ENDPOINT_TIMEOUT,
})

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
        raise HTTPException(status_code=400, detail=safe_detail(exc, subject="engine")) from None

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
        # R1. `redact_text(str(exc))` ran a shape-matcher over a driver's prose
        # and, when it could not tell, withheld the whole message — so the
        # operator's only diagnostic became the literal string
        # "***** (withheld)". A Failure is strictly better in both directions:
        # nothing from the driver is returned, *and* the operator gets a code
        # they can act on plus a ref that finds the full text in the log.
        failure = getattr(exc, "failure", None) or Failure.from_exception(
            exc, phase=Phase.connect, subject=f"engine:{name}",
            driver="adbc_flightsql", dsn=e["uri"], config=e.get("options"),
        )
        if failure.code is FailureCode.REMOTE_FAILED:
            # The ADBC driver connects **lazily**: `flight_sql.connect()` returns
            # a handle without touching the network, so a refused endpoint
            # surfaces from `query()` — down the execute path, where
            # `connect_failure`'s pre-flight never runs. Measured on this tree:
            # testing `grpc://127.0.0.1:1` reported REMOTE_FAILED "Laurelin
            # could not classify this one", for the single case the pre-flight
            # exists to classify.
            #
            # This route is a connectivity probe by definition, so when nothing
            # else classified it, ask the question the operator pressed the
            # button to ask. Same first-party facts as everywhere else: a
            # getaddrinfo and a TCP connect that *Laurelin* made.
            code, phase, endpoint = probe_endpoint(e["uri"], fallback_endpoint=failure.endpoint)
            # Only adopt the pre-flight's answer when it found the endpoint
            # *unusable*. If DNS and TCP both succeed, `probe_endpoint` reports
            # AUTH_REJECTED — a sound inference for a driver that failed while
            # connecting, and a wrong one here, where the failure came from a
            # query against a socket that was plainly up. REMOTE_FAILED plus the
            # log reference is the honest answer in that case; "rotate your
            # credential" would not be.
            if code in _UNUSABLE_ENDPOINT:
                failure = failure.model_copy(
                    update={"code": code, "phase": phase, "endpoint": endpoint}
                )
        # An engine is admin-authored and every engine route is ADMIN, so
        # `detail_for` renders in full here — it is written this way so the
        # rule is uniform and a future relaxation of the gate cannot
        # silently start disclosing the endpoint.
        return {"ok": False, "failure": serialize.dump(failure),
                "detail": serialize.detail_for(failure, Role.admin)}
    return {"ok": True}


@engines_router.delete("/engines/{name}", dependencies=[ADMIN])
def delete_engine(name: str, store: StoreDep, actor: ActorDep) -> dict:
    if not store.delete_engine(name):
        raise HTTPException(status_code=404, detail=f"Engine not found: {name!r}")
    store.log_audit("engine_deleted", {"engine": name}, actor=actor)
    return {"deleted": name}
