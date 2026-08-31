"""Health routes: the rollup, the event feed, the freshness declaration, and
admin-only alert-webhook configuration (task #74).

Disclosure decisions, stated where they are enforced:

* ``GET /health/datasets`` and ``GET /health/events`` are *authenticated*, not
  role-gated — filtering is per dataset via ``perms.viewable_datasets``, the
  ``GET /datasets`` precedent (routes.py). Deliberately NOT the
  ``/builds``/``/lineage`` precedent, which is VIEWER-gated but unfiltered;
  that pre-existing disclosure is neither widened nor fixed here. There is no
  unfiltered totals endpoint at all: different viewers legitimately see
  different counts, and a global count would leak existence deltas when a
  hidden dataset flips state.
* ``PUT /datasets/{name}/freshness`` is EDITOR: a freshness expectation is
  pipeline-author territory, exactly like expectations.py declarations, and it
  is tightening-shaped (it can only make health redder) — no approval needed.
* Webhook config is ADMIN, and the URL is **write-only**: it is a credential
  (a Slack-style URL carries its secret in the path — redaction.py documents
  this exact case), so reads come back with the WITHHELD marker, and the
  export secrets allowlist (``^url$``) already omits it from archives.
"""

from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from laurelin.api.routes import (
    ADMIN,
    EDITOR,
    ActorDep,
    PermDep,
    StoreDep,
    UserDep,
    _dump,
)
from laurelin.core import scheduler
from laurelin.core.health import RECOVERY_EVENT, RED_EVENTS, HealthService
from laurelin.core.models import AlertWebhookInfo, HealthEvent, utcnow_iso
from laurelin.core.redaction import WITHHELD

health_router = APIRouter(tags=["health"])

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_KNOWN_EVENTS = set(RED_EVENTS.values()) | {RECOVERY_EVENT}


def _service(store) -> HealthService:
    return HealthService(store, poll_seconds=scheduler.poll_seconds())


# ---------------------------------------------------------------------------
# Rollup + events (authenticated; per-dataset filtered)
# ---------------------------------------------------------------------------

@health_router.get("/health/datasets")
def health_rollup(store: StoreDep, perms: PermDep, user: UserDep) -> dict:
    """Health for every dataset the caller can view — and no other name.

    No role dependency beyond login: the filter is per dataset, so a viewer
    with two visible datasets gets a two-row answer, not a 403 and not the
    workspace's whole catalog.

    `others_exist` is the one bit that tells emptiness apart from withholding.
    Without it the page said "No datasets you can read." to a sole admin on a
    brand-new workspace — withholding language for plain emptiness, the exact
    inverse of the rule Datasets already enforces. It is the SAME unquantified
    boolean the lineage marks concede (`has_hidden_upstream`): never a count,
    never a name.
    """
    names = [d.name for d in store.list_datasets()]
    visible = perms.viewable_datasets(user, names)
    health = _service(store).dataset_health(sorted(visible))
    return {
        "datasets": [_dump(h) for _, h in sorted(health.items())],
        "others_exist": len(visible) != len(set(names)),
    }


@health_router.get("/health/events")
def health_events(
    store: StoreDep, perms: PermDep, user: UserDep,
    limit: int = Query(100, ge=1, le=1000),
) -> list[dict]:
    """Recent health transitions, filtered exactly like the rollup.

    The stored ``seq`` is a GLOBAL monotonic PK, assigned at insert across every
    dataset. Returning it verbatim after a per-dataset filter reintroduced the
    exact leak this module refuses to ship an unfiltered count endpoint to
    avoid: a viewer who saw ``seq`` 1 and 3 but not 2 could infer that one
    transition happened on a dataset she cannot see, and time it from the
    bracketing ``at`` values (the confirmed finding). So the global id never
    leaves the server — after filtering, ``seq`` is renumbered to a contiguous
    per-response ordinal (newest = highest). It stays a stable list key for the
    UI; it no longer encodes how many events exist workspace-wide, nor where the
    gaps are. Different viewers legitimately get different, gap-free sequences.
    """
    names = [d.name for d in store.list_datasets()]
    visible = perms.viewable_datasets(user, names)
    rows = [r for r in store.list_health_events(limit) if r["dataset"] in visible]
    n = len(rows)
    return [_dump(HealthEvent(**{**row, "seq": n - i})) for i, row in enumerate(rows)]


# ---------------------------------------------------------------------------
# Freshness declaration (editor)
# ---------------------------------------------------------------------------

class FreshnessRequest(BaseModel):
    # None clears the declaration. Bounded below by a minute: a sub-minute
    # freshness promise on a batch platform is a typo, not a policy.
    expected_fresh_seconds: Optional[int] = Field(None, ge=60)


@health_router.put("/datasets/{name}/freshness", dependencies=[EDITOR])
def set_freshness(
    name: str, body: FreshnessRequest, store: StoreDep, actor: ActorDep
) -> dict:
    if store.get_dataset(name) is None:
        raise KeyError(f"Dataset not found: {name!r}")
    store.set_dataset_freshness(name, body.expected_fresh_seconds)
    store.log_audit(
        "dataset_freshness_set",
        {"dataset": name, "expected_fresh_seconds": body.expected_fresh_seconds},
        actor=actor,
    )
    return {"dataset": name, "expected_fresh_seconds": body.expected_fresh_seconds}


# ---------------------------------------------------------------------------
# Alert webhooks (admin; URL write-only)
# ---------------------------------------------------------------------------

class WebhookUpsertRequest(BaseModel):
    # Absent/None on update keeps the stored URL — the UI cannot echo back a
    # value it was never given.
    url: Optional[str] = None
    datasets: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    enabled: bool = False


def _webhook_json(row: dict) -> dict:
    # The URL never leaves the server, not even to the admin who wrote it:
    # "the person who typed it" and "the person reading this screen" are not
    # necessarily the same admin. WITHHELD, not blank — a blank reads as
    # unset and invites a re-type.
    return _dump(AlertWebhookInfo(**{**row, "url": WITHHELD}))


@health_router.get("/alerts/webhooks", dependencies=[ADMIN])
def list_webhooks(store: StoreDep) -> list[dict]:
    return [_webhook_json(w) for w in store.list_alert_webhooks()]


@health_router.get("/alerts/webhooks/{name}", dependencies=[ADMIN])
def get_webhook(name: str, store: StoreDep) -> dict:
    row = store.get_alert_webhook(name)
    if row is None:
        raise KeyError(f"Webhook not found: {name!r}")
    return _webhook_json(row)


@health_router.put("/alerts/webhooks/{name}", dependencies=[ADMIN])
def put_webhook(
    name: str, body: WebhookUpsertRequest, store: StoreDep, actor: ActorDep
) -> dict:
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="Invalid webhook name (a-z 0-9 _ -, start with a letter, max 64)",
        )
    url = (body.url or "").strip() or None
    if url is not None and not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Webhook URL must be http(s)")
    if url == WITHHELD:
        # A client that PUT back what it read would overwrite the credential
        # with the marker; refuse loudly rather than break delivery silently.
        raise HTTPException(
            status_code=400,
            detail="That is the withheld marker, not a URL. Omit `url` to keep "
                   "the stored one.",
        )
    for ev in body.events:
        if ev not in _KNOWN_EVENTS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown event {ev!r}; expected one of {sorted(_KNOWN_EVENTS)}",
            )
    try:
        store.upsert_alert_webhook(
            name, url, body.datasets, body.events, body.enabled, created_by=actor
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Config write, not a grant/policy/principal change: audited, not gated
    # by approvals. min_read_role stays the default (admin).
    store.log_audit(
        "alert_webhook_set",
        {"name": name, "enabled": body.enabled, "datasets": body.datasets,
         "events": body.events, "url_changed": url is not None},
        actor=actor,
    )
    row = store.get_alert_webhook(name)
    assert row is not None
    return _webhook_json(row)


@health_router.delete("/alerts/webhooks/{name}", dependencies=[ADMIN])
def delete_webhook(name: str, store: StoreDep, actor: ActorDep) -> dict:
    if not store.delete_alert_webhook(name):
        raise KeyError(f"Webhook not found: {name!r}")
    store.log_audit("alert_webhook_deleted", {"name": name}, actor=actor)
    return {"ok": True}


@health_router.post("/alerts/webhooks/{name}/test", dependencies=[ADMIN])
def test_webhook(name: str, store: StoreDep, actor: ActorDep) -> dict:
    """Deliver a synthetic payload carrying no governed values at all —
    every field below is a constant, so a test cannot leak what a real alert
    is not allowed to carry either."""
    row = store.get_alert_webhook(name)
    if row is None:
        raise KeyError(f"Webhook not found: {name!r}")
    payload = {
        "event": "test",
        "dataset": "laurelin.test",
        "status": "healthy",
        "at": utcnow_iso(),
        "link": "/health",
    }
    status = _service(store).post_webhook(name, row["url"], payload)
    store.log_audit(
        "alert_webhook_tested", {"name": name, "status": status}, actor=actor
    )
    fresh = store.get_alert_webhook(name)
    if status is not None:
        return {"ok": True, "status": status}
    failure = (fresh or {}).get("last_delivery_failure")
    return {"ok": False, "failure": _dump(failure) if failure is not None else None}
