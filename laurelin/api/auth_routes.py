"""Authentication dependencies + /auth, /users, /tokens routers.

Credential resolution is a FastAPI dependency (``get_current_user``) so
route-level RBAC composes with it; ``require_role`` builds the per-route
guards used across the API. Only /docs gating lives in middleware (app.py).
"""

from __future__ import annotations

from typing import Annotated, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from laurelin.core.auth import THROTTLED, AuthService
from laurelin.core.db import MetadataStore
from laurelin.core.models import Role, User

SESSION_COOKIE = "laurelin_session"
SESSION_MAX_AGE = 7 * 24 * 3600

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------

def get_auth(request: Request) -> AuthService:
    return request.app.state.auth


def _implicit_admin(request: Request) -> User:
    """Every request in --no-auth mode acts as this synthetic admin. The
    username honors X-Laurelin-User so audit attribution still works locally."""
    username = request.headers.get("x-laurelin-user") or "anonymous"
    return User(id="no-auth", username=username, role=Role.admin)


def resolve_credential(request: Request) -> Optional[User]:
    """Resolve Bearer token or session cookie to a User (None if neither).

    Side effect: ``request.state.credential_kind`` is set to "none" | "bearer"
    | "session" so the CSRF check knows whether an ambient credential was used.
    """
    request.state.credential_kind = "none"
    if request.app.state.no_auth:
        return _implicit_admin(request)
    auth: AuthService = request.app.state.auth

    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and credentials.strip():
        user = auth.resolve_api_token(credentials.strip())
        if user is not None:
            request.state.credential_kind = "bearer"
        return user

    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        user = auth.resolve_session(cookie)
        if user is not None:
            request.state.credential_kind = "session"
        return user
    return None


def get_current_user(request: Request) -> Optional[User]:
    return resolve_credential(request)


CurrentUser = Annotated[Optional[User], Depends(get_current_user)]


def _origin_matches_host(origin: str, request: Request) -> bool:
    origin_host = urlsplit(origin).netloc
    host = request.headers.get("host", "")
    return bool(origin_host) and origin_host == host


def require_user(request: Request, user: CurrentUser) -> User:
    """401 without a valid credential (or in setup mode); 403 on CSRF
    origin mismatch for cookie-authenticated mutations."""
    if request.app.state.no_auth:
        assert user is not None
        return user
    store: MetadataStore = request.app.state.store
    if store.count_users() == 0:
        raise HTTPException(status_code=401, detail="setup required")
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if (
        request.method in _MUTATING_METHODS
        and getattr(request.state, "credential_kind", "none") == "session"
    ):
        origin = request.headers.get("origin")
        if origin is not None and not _origin_matches_host(origin, request):
            raise HTTPException(
                status_code=403, detail="CSRF check failed: Origin does not match host"
            )
    return user


AuthenticatedUser = Annotated[User, Depends(require_user)]


def require_role(role: Role):
    def dependency(user: AuthenticatedUser) -> User:
        if not user.role.covers(role):
            raise HTTPException(
                status_code=403,
                detail=f"Requires {role.value} role (you are {user.role.value})",
            )
        return user

    dependency.__name__ = f"require_{role.value}"
    return dependency


require_viewer = require_role(Role.viewer)
require_editor = require_role(Role.editor)
require_admin = require_role(Role.admin)


def _user_json(user: User) -> dict:
    return user.model_dump(mode="json")


# ---------------------------------------------------------------------------
# /auth
# ---------------------------------------------------------------------------

auth_router = APIRouter(prefix="/auth", tags=["auth"])


class CredentialsRequest(BaseModel):
    username: str
    password: str


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    secure = bool(request.app.state.secure_cookies) or (
        request.headers.get("x-forwarded-proto", "").lower() == "https"
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=secure,
    )


@auth_router.get("/status")
def auth_status(request: Request, user: CurrentUser) -> dict:
    """Never 401s — the UI probes this before deciding to show a login form."""
    if request.app.state.no_auth:
        return {"auth_required": False, "setup_required": False,
                "user": _user_json(user) if user else None}
    store: MetadataStore = request.app.state.store
    setup_required = store.count_users() == 0
    return {
        "auth_required": True,
        "setup_required": setup_required,
        "user": _user_json(user) if user else None,
    }


@auth_router.post("/setup")
def auth_setup(body: CredentialsRequest, request: Request) -> dict:
    store: MetadataStore = request.app.state.store
    auth: AuthService = request.app.state.auth
    user = auth.create_first_admin(body.username, body.password, actor=body.username)
    if user is None:
        raise HTTPException(status_code=409, detail="Setup already completed")
    store.log_audit("setup_completed", {"username": user.username}, actor=user.username)
    return _user_json(user)


@auth_router.post("/login")
def auth_login(body: CredentialsRequest, request: Request, response: Response) -> dict:
    store: MetadataStore = request.app.state.store
    auth: AuthService = request.app.state.auth
    result = auth.authenticate(body.username, body.password)
    if result is THROTTLED:
        store.log_audit("login_throttled", {"username": body.username}, actor=body.username)
        raise HTTPException(
            status_code=429, detail="Too many failed logins; try again shortly"
        )
    if result is None:
        store.log_audit("login_failed", {"username": body.username}, actor=body.username)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token, user = auth.login(result)
    _set_session_cookie(response, request, token)
    store.log_audit("login_succeeded", {"username": user.username}, actor=user.username)
    # The session token travels ONLY in the httpOnly cookie, never in the body.
    return _user_json(user)


@auth_router.post("/logout")
def auth_logout(request: Request, response: Response, user: AuthenticatedUser) -> dict:
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        auth: AuthService = request.app.state.auth
        auth.logout(cookie)
    response.delete_cookie(SESSION_COOKIE, path="/")
    request.app.state.store.log_audit("logout", {"username": user.username}, actor=user.username)
    return {"ok": True}


@auth_router.get("/me")
def auth_me(user: AuthenticatedUser) -> dict:
    return _user_json(user)


# ---------------------------------------------------------------------------
# /users (admin only)
# ---------------------------------------------------------------------------

users_router = APIRouter(prefix="/users", tags=["users"])


class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: Role = Role.viewer


class UserUpdateRequest(BaseModel):
    role: Optional[Role] = None
    password: Optional[str] = None
    disabled: Optional[bool] = None


@users_router.get("")
def list_users(
    request: Request, admin: Annotated[User, Depends(require_admin)]
) -> list[dict]:
    auth: AuthService = request.app.state.auth
    return [_user_json(u) for u in auth.list_users()]


@users_router.post("")
def create_user(
    body: UserCreateRequest,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
) -> dict:
    auth: AuthService = request.app.state.auth
    user = auth.create_user(body.username, body.password, body.role, actor=admin.username)
    return _user_json(user)


@users_router.patch("/{username}")
def update_user(
    username: str,
    body: UserUpdateRequest,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
) -> dict:
    auth: AuthService = request.app.state.auth
    target = auth.get_user(username)
    if target is None:
        raise HTTPException(status_code=404, detail=f"User not found: {username!r}")
    if target.id == admin.id:
        if body.disabled:
            raise HTTPException(status_code=400, detail="You cannot disable yourself")
        if body.role is not None and body.role != Role.admin:
            raise HTTPException(status_code=400, detail="You cannot demote yourself")
    updated = auth.update_user(
        target.username,
        role=body.role,
        password=body.password,
        disabled=body.disabled,
        actor=admin.username,
    )
    return _user_json(updated)


@users_router.delete("/{username}")
def delete_user(
    username: str,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
) -> dict:
    auth: AuthService = request.app.state.auth
    target = auth.get_user(username)
    if target is None:
        raise HTTPException(status_code=404, detail=f"User not found: {username!r}")
    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete yourself")
    auth.delete_user(target.username, actor=admin.username)
    return {"ok": True}


# ---------------------------------------------------------------------------
# /tokens
# ---------------------------------------------------------------------------

tokens_router = APIRouter(prefix="/tokens", tags=["tokens"])


class TokenCreateRequest(BaseModel):
    name: str


def _token_json(record: dict) -> dict:
    return {
        "id": record["id"],
        "name": record["name"],
        "username": record["username"],
        "created_at": record["created_at"],
        "last_used_at": record["last_used_at"],
    }


@tokens_router.get("")
def list_tokens(request: Request, user: AuthenticatedUser) -> list[dict]:
    auth: AuthService = request.app.state.auth
    records = auth.list_api_tokens(None if user.role == Role.admin else user)
    return [_token_json(r) for r in records]


@tokens_router.post("")
def create_token(
    body: TokenCreateRequest,
    request: Request,
    user: Annotated[User, Depends(require_editor)],
) -> dict:
    auth: AuthService = request.app.state.auth
    token, record = auth.create_api_token(user, body.name, actor=user.username)
    # The ONLY place a plaintext API token ever appears.
    return {"id": record["id"], "name": record["name"], "token": token}


@tokens_router.delete("/{token_id}")
def revoke_token(token_id: str, request: Request, user: AuthenticatedUser) -> dict:
    auth: AuthService = request.app.state.auth
    record = auth.get_api_token(token_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Token not found: {token_id!r}")
    if record["user_id"] != user.id and user.role != Role.admin:
        raise HTTPException(status_code=403, detail="You can only revoke your own tokens")
    auth.revoke_api_token(token_id, actor=user.username)
    return {"ok": True}


# ---------------------------------------------------------------------------
# /groups (admin) — named groups of users, usable as ontology-permission subjects
# ---------------------------------------------------------------------------

import re as _re

from laurelin.core.models import utcnow_iso

groups_router = APIRouter(prefix="/groups", tags=["groups"])

_GROUP_RE = _re.compile(r"^[a-z0-9][a-z0-9_.-]{1,31}$")


class GroupCreateRequest(BaseModel):
    name: str


class GroupMembersRequest(BaseModel):
    members: list[str]


@groups_router.get("")
def list_groups(request: Request, admin: Annotated[User, Depends(require_admin)]) -> list[dict]:
    return request.app.state.store.list_groups()


@groups_router.post("")
def create_group(
    body: GroupCreateRequest, request: Request, admin: Annotated[User, Depends(require_admin)]
) -> dict:
    store: MetadataStore = request.app.state.store
    name = body.name.strip().lower()
    if not _GROUP_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="Invalid group name: 2-32 chars, lowercase letters/digits/._-",
        )
    if store.group_exists(name):
        raise HTTPException(status_code=409, detail=f"Group already exists: {name!r}")
    store.create_group(name, utcnow_iso())
    store.log_audit("group_created", {"name": name}, actor=admin.username)
    return {"name": name, "members": []}


@groups_router.put("/{name}/members")
def set_group_members(
    name: str,
    body: GroupMembersRequest,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
) -> dict:
    store: MetadataStore = request.app.state.store
    if not store.group_exists(name):
        raise HTTPException(status_code=404, detail=f"Group not found: {name!r}")
    members = []
    for u in body.members:
        if store.get_user(u) is None:
            raise HTTPException(status_code=400, detail=f"Unknown user: {u!r}")
        members.append(u.lower())
    store.set_group_members(name, members)
    store.log_audit(
        "group_members_set", {"name": name, "count": len(members)}, actor=admin.username
    )
    return {"name": name, "members": sorted(set(members))}


@groups_router.delete("/{name}")
def delete_group(
    name: str, request: Request, admin: Annotated[User, Depends(require_admin)]
) -> dict:
    store: MetadataStore = request.app.state.store
    if not store.group_exists(name):
        raise HTTPException(status_code=404, detail=f"Group not found: {name!r}")
    store.delete_group(name)
    store.log_audit("group_deleted", {"name": name}, actor=admin.username)
    return {"ok": True}
