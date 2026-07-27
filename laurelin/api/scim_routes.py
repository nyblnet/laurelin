"""SCIM 2.0 provisioning (RFC 7643/7644) — a minimal but real subset so an IdP
(Okta, Entra ID, …) can push users and groups into Laurelin and, crucially,
*deprovision* them: setting a user inactive (or deleting them) immediately kills
their sessions and API tokens.

SCIM maps onto the existing identity store: a SCIM User is a Laurelin user
(``userName`` -> username, ``active`` -> not disabled), a SCIM Group is a
Laurelin group. New users get an unusable random password (they sign in via
SSO) and the ``viewer`` role by default; role elevation stays with admins /
OIDC group mapping.

Enabled by setting ``LAURELIN_SCIM_TOKEN``; the IdP authenticates with that as a
bearer token on every request. Identity is the *control* store in multi-workspace
mode, else the single workspace's store.
"""

from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from laurelin.api.context import identity_auth, identity_store
from laurelin.core.models import Role, User

scim_router = APIRouter(prefix="/scim/v2", tags=["scim"])

_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


def scim_enabled() -> bool:
    return bool(os.environ.get("LAURELIN_SCIM_TOKEN"))


def _scim_error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"schemas": [_ERROR_SCHEMA], "detail": detail, "status": str(status)},
        media_type="application/scim+json",
    )


def _authorize(request: Request) -> JSONResponse | None:
    token = os.environ.get("LAURELIN_SCIM_TOKEN")
    if not token:
        return _scim_error(404, "SCIM is not enabled")
    scheme, _, creds = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(creds.strip(), token):
        return _scim_error(401, "Invalid SCIM token")
    return None


# ---------------------------------------------------------------------------
# Resource <-> SCIM representation
# ---------------------------------------------------------------------------

def _user_to_scim(user: User, request: Request) -> dict:
    base = str(request.base_url).rstrip("/")
    return {
        "schemas": [_USER_SCHEMA],
        "id": user.id,
        "userName": user.username,
        "active": not user.disabled,
        "meta": {
            "resourceType": "User",
            "location": f"{base}/scim/v2/Users/{user.id}",
        },
    }


def _group_to_scim(group: dict, request: Request) -> dict:
    base = str(request.base_url).rstrip("/")
    return {
        "schemas": [_GROUP_SCHEMA],
        "id": group["name"],
        "displayName": group["name"],
        "members": [{"value": m, "display": m} for m in group.get("members", [])],
        "meta": {
            "resourceType": "Group",
            "location": f"{base}/scim/v2/Groups/{group['name']}",
        },
    }


def _list_response(resources: list[dict]) -> dict:
    return {
        "schemas": [_LIST_SCHEMA],
        "totalResults": len(resources),
        "startIndex": 1,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


def _scim_json(content: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content=content, media_type="application/scim+json")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@scim_router.get("/ServiceProviderConfig")
def service_provider_config(request: Request):
    if (err := _authorize(request)) is not None:
        return err
    return _scim_json({
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {"type": "oauthbearertoken", "name": "Bearer Token",
             "description": "Authenticate with the configured SCIM bearer token."}
        ],
    })


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def _find_user_by_scim_id(request: Request, scim_id: str) -> User | None:
    return identity_store(request).get_user_by_id(scim_id)


@scim_router.get("/Users")
def list_users(request: Request):
    if (err := _authorize(request)) is not None:
        return err
    store = identity_store(request)
    # Support the common Okta/Entra filter: userName eq "x"
    flt = request.query_params.get("filter", "")
    users = store.list_users()
    if "userName eq" in flt:
        wanted = flt.split('"')[1].lower() if '"' in flt else ""
        users = [u for u in users if u.username == wanted]
    return _scim_json(_list_response([_user_to_scim(u, request) for u in users]))


@scim_router.get("/Users/{scim_id}")
def get_user(scim_id: str, request: Request):
    if (err := _authorize(request)) is not None:
        return err
    user = _find_user_by_scim_id(request, scim_id)
    if user is None:
        return _scim_error(404, f"User {scim_id} not found")
    return _scim_json(_user_to_scim(user, request))


@scim_router.post("/Users")
async def create_user(request: Request):
    if (err := _authorize(request)) is not None:
        return err
    body = await request.json()
    username = (body.get("userName") or "").strip().lower()
    if not username:
        return _scim_error(400, "userName is required")
    auth = identity_auth(request)
    existing = auth.get_user(username)
    if existing is not None:
        # SCIM create on an existing user -> 409 (IdP will then PATCH/PUT it).
        return _scim_error(409, f"User {username} already exists")
    try:
        user = auth.create_user(
            username, secrets.token_urlsafe(32), Role.viewer, actor="scim"
        )
    except ValueError as exc:
        return _scim_error(400, str(exc))
    if body.get("active") is False:
        auth.update_user(user.username, disabled=True, actor="scim")
        user = auth.get_user(user.username)
    return _scim_json(_user_to_scim(user, request), status=201)


def _apply_active(request: Request, user: User, active) -> User:
    auth = identity_auth(request)
    if active is not None and bool(active) == user.disabled:
        # Deprovisioning: disabling immediately invalidates sessions + tokens
        # (resolve_session/resolve_api_token reject disabled users).
        auth.update_user(user.username, disabled=not bool(active), actor="scim")
        return auth.get_user(user.username)
    return user


@scim_router.put("/Users/{scim_id}")
async def replace_user(scim_id: str, request: Request):
    if (err := _authorize(request)) is not None:
        return err
    user = _find_user_by_scim_id(request, scim_id)
    if user is None:
        return _scim_error(404, f"User {scim_id} not found")
    body = await request.json()
    user = _apply_active(request, user, body.get("active"))
    return _scim_json(_user_to_scim(user, request))


@scim_router.patch("/Users/{scim_id}")
async def patch_user(scim_id: str, request: Request):
    if (err := _authorize(request)) is not None:
        return err
    user = _find_user_by_scim_id(request, scim_id)
    if user is None:
        return _scim_error(404, f"User {scim_id} not found")
    body = await request.json()
    # Handle the standard "replace {active: false}" deprovisioning patch.
    for op in body.get("Operations", []):
        if op.get("op", "").lower() != "replace":
            continue
        value = op.get("value")
        if isinstance(value, dict) and "active" in value:
            user = _apply_active(request, user, value["active"])
        elif op.get("path") == "active":
            user = _apply_active(request, user, value)
    return _scim_json(_user_to_scim(user, request))


@scim_router.delete("/Users/{scim_id}")
def delete_user(scim_id: str, request: Request):
    if (err := _authorize(request)) is not None:
        return err
    user = _find_user_by_scim_id(request, scim_id)
    if user is None:
        return _scim_error(404, f"User {scim_id} not found")
    identity_auth(request).delete_user(user.username, actor="scim")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

@scim_router.get("/Groups")
def list_groups(request: Request):
    if (err := _authorize(request)) is not None:
        return err
    groups = identity_store(request).list_groups()
    return _scim_json(_list_response([_group_to_scim(g, request) for g in groups]))


@scim_router.get("/Groups/{name}")
def get_group(name: str, request: Request):
    if (err := _authorize(request)) is not None:
        return err
    store = identity_store(request)
    group = next((g for g in store.list_groups() if g["name"] == name.lower()), None)
    if group is None:
        return _scim_error(404, f"Group {name} not found")
    return _scim_json(_group_to_scim(group, request))


def _members_from_scim(body: dict) -> list[str]:
    # SCIM member values are user ids; resolve them to usernames.
    return [m.get("value") for m in body.get("members", []) if m.get("value")]


def _resolve_member_usernames(request: Request, values: list[str]) -> list[str]:
    store = identity_store(request)
    out = []
    for v in values:
        user = store.get_user_by_id(v) or store.get_user(v)
        if user is not None:
            out.append(user.username)
    return out


@scim_router.post("/Groups")
async def create_group(request: Request):
    if (err := _authorize(request)) is not None:
        return err
    body = await request.json()
    name = (body.get("displayName") or "").strip().lower()
    if not name:
        return _scim_error(400, "displayName is required")
    from laurelin.core.models import utcnow_iso

    store = identity_store(request)
    if not store.group_exists(name):
        store.create_group(name, utcnow_iso())
    members = _resolve_member_usernames(request, _members_from_scim(body))
    if members:
        store.set_group_members(name, members)
    group = next(g for g in store.list_groups() if g["name"] == name)
    return _scim_json(_group_to_scim(group, request), status=201)


@scim_router.patch("/Groups/{name}")
async def patch_group(name: str, request: Request):
    if (err := _authorize(request)) is not None:
        return err
    store = identity_store(request)
    name = name.lower()
    if not store.group_exists(name):
        return _scim_error(404, f"Group {name} not found")
    body = await request.json()
    current = set(next(g["members"] for g in store.list_groups() if g["name"] == name))
    for op in body.get("Operations", []):
        action = op.get("op", "").lower()
        values = _resolve_member_usernames(
            request,
            [m.get("value") for m in (op.get("value") or []) if isinstance(m, dict) and m.get("value")],
        )
        if action == "add":
            current |= set(values)
        elif action == "remove":
            if op.get("path", "").startswith("members") and not values:
                current = set()  # remove all members
            else:
                current -= set(values)
        elif action == "replace" and op.get("path") == "members":
            current = set(values)
    store.set_group_members(name, sorted(current))
    group = next(g for g in store.list_groups() if g["name"] == name)
    return _scim_json(_group_to_scim(group, request))


@scim_router.delete("/Groups/{name}")
def delete_group(name: str, request: Request):
    if (err := _authorize(request)) is not None:
        return err
    store = identity_store(request)
    if not store.group_exists(name.lower()):
        return _scim_error(404, f"Group {name} not found")
    store.delete_group(name.lower())
    return Response(status_code=204)
