"""Authentication dependencies + /auth, /users, /tokens routers.

Credential resolution is a FastAPI dependency (``get_current_user``) so
route-level RBAC composes with it; ``require_role`` builds the per-route
guards used across the API. Only /docs gating lives in middleware (app.py).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from laurelin.api.context import (
    active_slug,
    active_store,
    identity_auth,
    identity_store,
    is_multi,
)
from laurelin.core import serialize
from laurelin.core.auth import THROTTLED, AuthService
from laurelin.core.failure import Failure, FailureCode, Phase, safe_detail
from laurelin.core.models import Role, User, utcnow_iso

SESSION_COOKIE = "laurelin_session"
SESSION_MAX_AGE = 7 * 24 * 3600

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------

def _implicit_admin(request: Request) -> User:
    """Every request in --no-auth mode acts as this synthetic admin. The
    username honors X-Laurelin-User so audit attribution still works locally."""
    username = request.headers.get("x-laurelin-user") or "anonymous"
    return User(id="no-auth", username=username, role=Role.admin, superadmin=True)


def resolve_credential(request: Request) -> Optional[User]:
    """Resolve Bearer token or session cookie to a User (None if neither).

    Side effect: ``request.state.credential_kind`` is set to "none" | "bearer"
    | "session" so the CSRF check knows whether an ambient credential was used.
    """
    request.state.credential_kind = "none"
    if request.app.state.no_auth:
        return _implicit_admin(request)
    auth: AuthService = identity_auth(request)

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


def require_identity(request: Request, user: CurrentUser) -> User:
    """The authenticated global identity: 401 without a valid credential (or in
    setup mode); 403 on CSRF origin mismatch for cookie-authenticated mutations.
    This is workspace-independent — used by control-plane routes (/auth, /users,
    /tokens, /workspaces)."""
    if request.app.state.no_auth:
        assert user is not None
        return user
    if identity_store(request).count_users() == 0:
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


Identity = Annotated[User, Depends(require_identity)]


def require_user(request: Request, identity: Identity) -> User:
    """The authenticated user with their EFFECTIVE role for the active workspace.
    In single mode this is just the identity. In multi mode the role comes from
    workspace membership (or admin for a superadmin); non-members get 403. Used
    by all workspace-scoped routes."""
    if not is_multi(request) or request.app.state.no_auth:
        return identity
    if identity.superadmin:
        # Superadmins are admin in every workspace; still require the slug so the
        # active-workspace context is well-defined.
        active_slug(request)
        return identity.model_copy(update={"role": Role.admin})
    slug = active_slug(request)
    role = request.app.state.control.member_role(slug, identity.username)
    if role is None:
        raise HTTPException(
            status_code=403, detail=f"You are not a member of workspace {slug!r}"
        )
    return identity.model_copy(update={"role": role})


AuthenticatedUser = Annotated[User, Depends(require_user)]


def require_superadmin(request: Request, identity: Identity) -> User:
    """Server-level admin: manage workspaces + global users. In single mode this
    is the workspace admin (there is no separate server tier)."""
    if request.app.state.no_auth:
        return identity
    if not is_multi(request):
        if not identity.role.covers(Role.admin):
            raise HTTPException(
                status_code=403,
                detail=f"Requires admin role (you are {identity.role.value})",
            )
        return identity
    if not identity.superadmin:
        raise HTTPException(
            status_code=403, detail="Requires a server administrator (superadmin)"
        )
    return identity


Superadmin = Annotated[User, Depends(require_superadmin)]


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
    # Through the one serializer, like everything else. `User` is `Governed`
    # with every field PRESENTATION — they are identity facts the account
    # holder reads about themselves — so this is behaviour-preserving today
    # and fails closed for a field somebody adds tomorrow.
    return serialize.dump(user)


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


def _status_user(request: Request, user: User) -> dict:
    """User payload for /status, including their workspaces in multi mode."""
    data = _user_json(user)
    if is_multi(request):
        control = request.app.state.control
        data["workspaces"] = (
            [serialize.dump(w) | {"role": "admin"}
             for w in control.list_workspaces()]
            if user.superadmin
            else control.workspaces_for_user(user.username)
        )
    return data


@auth_router.get("/status")
def auth_status(request: Request, user: CurrentUser) -> dict:
    """Never 401s — the UI probes this before deciding to show a login form."""
    multi = is_multi(request)
    # The lock posture rides along on the boot probe so the UI can say
    # "Python authoring is locked here" up front, instead of letting an
    # author build something and discover the lock as a 403 on save.
    authoring = {
        "pipelines_locked": bool(request.app.state.lock_pipelines),
        "flows_locked": bool(request.app.state.lock_flows),
    }
    if request.app.state.no_auth:
        return {"auth_required": False, "setup_required": False, "multi": multi,
                "authoring": authoring,
                "oidc": _oidc_status(request), "saml": _saml_status(request),
                "user": _status_user(request, user) if user else None}
    setup_required = identity_store(request).count_users() == 0
    return {
        "auth_required": True,
        "setup_required": setup_required,
        "multi": multi,
        "authoring": authoring,
        "oidc": _oidc_status(request),
        "saml": _saml_status(request),
        "user": _status_user(request, user) if user else None,
    }


@auth_router.post("/setup")
def auth_setup(body: CredentialsRequest, request: Request) -> dict:
    store = identity_store(request)
    auth = identity_auth(request)
    # In multi mode the first account is a server superadmin.
    user = auth.create_first_admin(
        body.username, body.password, superadmin=is_multi(request), actor=body.username
    )
    if user is None:
        raise HTTPException(status_code=409, detail="Setup already completed")
    store.log_audit("setup_completed", {"username": user.username}, actor=user.username)
    return _user_json(user)


@auth_router.post("/login")
def auth_login(body: CredentialsRequest, request: Request, response: Response) -> dict:
    store = identity_store(request)
    auth = identity_auth(request)
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
    return _status_user(request, user)


def _oidc_status(request: Request) -> dict:
    cfg = getattr(request.app.state, "oidc_config", None)
    if cfg is None or not cfg.enabled:
        return {"enabled": False}
    return {"enabled": True, "provider_name": cfg.provider_name}


def _oidc_redirect_uri(request: Request) -> str:
    cfg = request.app.state.oidc_config
    if cfg.redirect_url:
        return cfg.redirect_url
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/v1/auth/oidc/callback"


@auth_router.get("/oidc/login")
def oidc_login(request: Request):
    from fastapi.responses import RedirectResponse

    from laurelin.core.oidc import make_pkce

    cfg = getattr(request.app.state, "oidc_config", None)
    if cfg is None or not cfg.enabled:
        raise HTTPException(status_code=404, detail="SSO is not configured")
    import secrets as _secrets

    state = _secrets.token_urlsafe(24)
    nonce = _secrets.token_urlsafe(16)
    verifier, challenge = make_pkce()
    redirect_uri = _oidc_redirect_uri(request)
    store = identity_store(request)
    store.purge_oidc_flows(  # opportunistic cleanup of flows older than ~10 min
        (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    )
    store.create_oidc_flow(state, nonce, verifier, redirect_uri)
    url = request.app.state.oidc_provider.authorization_url(redirect_uri, state, nonce, challenge)
    return RedirectResponse(url, status_code=302)


@auth_router.get("/oidc/callback")
def oidc_callback(request: Request, response: Response):
    from fastapi.responses import RedirectResponse

    from laurelin.core.oidc import OIDCError

    cfg = getattr(request.app.state, "oidc_config", None)
    if cfg is None or not cfg.enabled:
        raise HTTPException(status_code=404, detail="SSO is not configured")
    params = request.query_params
    if params.get("error"):
        raise HTTPException(status_code=400, detail=f"SSO error: {params.get('error')}")
    code, state = params.get("code"), params.get("state")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code/state")
    store = identity_store(request)
    flow = store.pop_oidc_flow(state)  # single-use; unknown/replayed state -> 400
    if flow is None:
        raise HTTPException(status_code=400, detail="Invalid or expired SSO state")
    provider = request.app.state.oidc_provider
    try:
        tokens = provider.exchange_code(code, flow["redirect_uri"], flow["code_verifier"])
        id_token = tokens.get("id_token")
        if not id_token:
            raise OIDCError("No id_token in token response")
        claims = provider.validate_id_token(id_token, flow["nonce"])
    except OIDCError as exc:
        # R1. `redact_text(str(exc))` was a redacted provider sentence, which is
        # still a provider sentence. What the trail gets now is the shape of the
        # failure; the server log gets the rest.
        failure = Failure.from_exception(
            exc, code=FailureCode.AUTH_REJECTED, phase=Phase.authenticate,
            subject="oidc:login", driver="requests",
        )
        store.log_audit(
            "oidc_login_failed", {"failure": failure.audit_projection()}, actor="oidc",
        )
        # Unauthenticated caller: the brief form. The endpoint here is the
        # IdP host out of an ADMIN-authored provider config.
        raise HTTPException(status_code=400, detail=failure.render_brief()) from None

    username = cfg.username_for(claims)
    if not username:
        raise HTTPException(status_code=400, detail="SSO token has no usable username claim")
    groups = cfg.groups_of(claims)
    auth = identity_auth(request)
    user = auth.provision_oidc_user(
        username, cfg.role_for(groups), cfg.is_superadmin(groups)
    )
    if user.disabled:
        raise HTTPException(status_code=403, detail="Account is disabled")
    token, user = auth.login(user)
    resp = RedirectResponse("/", status_code=302)
    _set_session_cookie(resp, request, token)
    store.log_audit("login_succeeded", {"username": user.username, "via": "sso"}, actor=user.username)
    return resp


def _saml_status(request: Request) -> dict:
    cfg = getattr(request.app.state, "saml_config", None)
    if cfg is None or not cfg.enabled:
        return {"enabled": False}
    return {"enabled": True, "provider_name": cfg.provider_name}


@auth_router.get("/saml/metadata")
def saml_metadata(request: Request):
    from fastapi.responses import Response as FastResponse

    cfg = getattr(request.app.state, "saml_config", None)
    if cfg is None or not cfg.enabled:
        raise HTTPException(status_code=404, detail="SAML is not configured")
    xml = request.app.state.saml_provider.metadata_xml()
    return FastResponse(content=xml, media_type="application/samlmetadata+xml")


@auth_router.get("/saml/login")
def saml_login(request: Request):
    from fastapi.responses import RedirectResponse

    cfg = getattr(request.app.state, "saml_config", None)
    if cfg is None or not cfg.enabled:
        raise HTTPException(status_code=404, detail="SAML is not configured")
    return RedirectResponse(request.app.state.saml_provider.login_redirect(), status_code=302)


@auth_router.post("/saml/acs")
async def saml_acs(request: Request):
    """Assertion Consumer Service — the IdP POSTs the SAMLResponse here."""
    from fastapi.responses import RedirectResponse

    cfg = getattr(request.app.state, "saml_config", None)
    if cfg is None or not cfg.enabled:
        raise HTTPException(status_code=404, detail="SAML is not configured")
    form = await request.form()
    saml_response = form.get("SAMLResponse")
    if not saml_response:
        raise HTTPException(status_code=400, detail="Missing SAMLResponse")
    store = identity_store(request)
    try:
        username, groups = request.app.state.saml_provider.parse_response(str(saml_response))
    except Exception as exc:  # noqa: BLE001 - any validation failure is an auth failure
        # R1, and this was the worst-shaped one in the tree: **unredacted**,
        # written to a VIEWER-gated audit route, from an **attacker-supplied**
        # `SAMLResponse` on an **unauthenticated** endpoint. A key called
        # `reason` is exactly what a backstop keyed on key *names* cannot help
        # with, which is why `_redacted_audit_details` is gone rather than
        # extended.
        failure = Failure.from_exception(
            exc, code=FailureCode.AUTH_REJECTED, phase=Phase.authenticate,
            subject="saml:login", driver="python",
        )
        store.log_audit(
            "saml_login_failed", {"failure": failure.audit_projection()}, actor="saml"
        )
        # Unauthenticated, and the input is attacker-supplied. Brief.
        raise HTTPException(status_code=400, detail=failure.render_brief()) from None
    auth = identity_auth(request)
    user = auth.provision_oidc_user(username, cfg.role_for(groups), cfg.is_superadmin(groups))
    if user.disabled:
        raise HTTPException(status_code=403, detail="Account is disabled")
    token, user = auth.login(user)
    resp = RedirectResponse("/", status_code=302)
    _set_session_cookie(resp, request, token)
    store.log_audit("login_succeeded", {"username": user.username, "via": "saml"}, actor=user.username)
    return resp


@auth_router.post("/logout")
def auth_logout(request: Request, response: Response, user: Identity) -> dict:
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        identity_auth(request).logout(cookie)
    response.delete_cookie(SESSION_COOKIE, path="/")
    identity_store(request).log_audit("logout", {"username": user.username}, actor=user.username)
    return {"ok": True}


@auth_router.get("/me")
def auth_me(request: Request, user: Identity) -> dict:
    return _status_user(request, user)


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
def list_users(request: Request, admin: Superadmin) -> list[dict]:
    auth = identity_auth(request)
    return [_user_json(u) for u in auth.list_users()]


@users_router.post("")
def create_user(
    body: UserCreateRequest,
    request: Request,
    admin: Superadmin,
) -> dict:
    auth = identity_auth(request)
    user = auth.create_user(body.username, body.password, body.role, actor=admin.username)
    return _user_json(user)


@users_router.patch("/{username}")
def update_user(
    username: str,
    body: UserUpdateRequest,
    request: Request,
    admin: Superadmin,
) -> dict:
    auth = identity_auth(request)
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
    admin: Superadmin,
) -> dict:
    auth = identity_auth(request)
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
    auth = identity_auth(request)
    records = auth.list_api_tokens(None if user.role == Role.admin else user)
    return [_token_json(r) for r in records]


@tokens_router.post("")
def create_token(
    body: TokenCreateRequest,
    request: Request,
    user: Annotated[User, Depends(require_editor)],
) -> dict:
    auth = identity_auth(request)
    token, record = auth.create_api_token(user, body.name, actor=user.username)
    # The ONLY place a plaintext API token ever appears.
    return {"id": record["id"], "name": record["name"], "token": token}


@tokens_router.delete("/{token_id}")
def revoke_token(token_id: str, request: Request, user: AuthenticatedUser) -> dict:
    auth = identity_auth(request)
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

groups_router = APIRouter(prefix="/groups", tags=["groups"])

_GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,31}$")


class GroupCreateRequest(BaseModel):
    name: str


class GroupMembersRequest(BaseModel):
    members: list[str]


@groups_router.get("")
def list_groups(request: Request, admin: Annotated[User, Depends(require_admin)]) -> list[dict]:
    return active_store(request).list_groups()


@groups_router.post("")
def create_group(
    body: GroupCreateRequest, request: Request, admin: Annotated[User, Depends(require_admin)]
) -> dict:
    store = active_store(request)
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
    store = active_store(request)
    users = identity_store(request)  # usernames are global identity, not per-workspace
    if not store.group_exists(name):
        raise HTTPException(status_code=404, detail=f"Group not found: {name!r}")
    members = []
    for u in body.members:
        if users.get_user(u) is None:
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
    store = active_store(request)
    if not store.group_exists(name):
        raise HTTPException(status_code=404, detail=f"Group not found: {name!r}")
    store.delete_group(name)
    store.log_audit("group_deleted", {"name": name}, actor=admin.username)
    return {"ok": True}


# ---------------------------------------------------------------------------
# /workspaces (multi-workspace control plane, superadmin only)
# ---------------------------------------------------------------------------

from laurelin.core.config import Workspace  # noqa: E402
from laurelin.core.control import ControlStore, validate_slug  # noqa: E402

workspaces_router = APIRouter(prefix="/workspaces", tags=["workspaces"])


class WorkspaceCreateRequest(BaseModel):
    slug: str
    name: str = ""
    description: str = ""


class WorkspaceUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class MemberRequest(BaseModel):
    username: str
    role: Role = Role.viewer


def _require_multi(request: Request) -> ControlStore:
    if not is_multi(request):
        raise HTTPException(
            status_code=404,
            detail="This server hosts a single workspace; workspace management "
            "is only available in multi-workspace mode (serve --root).",
        )
    return request.app.state.control


@workspaces_router.get("")
def list_workspaces(request: Request, admin: Superadmin) -> list[dict]:
    control = _require_multi(request)
    return [
        serialize.dump(w) | {"members": len(control.list_members(w.slug))}
        for w in control.list_workspaces()
    ]


@workspaces_router.post("")
def create_workspace(
    body: WorkspaceCreateRequest, request: Request, admin: Superadmin
) -> dict:
    control = _require_multi(request)
    try:
        slug = validate_slug(body.slug.strip().lower())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=safe_detail(exc, subject="auth"))
    if control.get_workspace(slug) is not None:
        raise HTTPException(status_code=409, detail=f"Workspace already exists: {slug!r}")
    # Create the workspace directory (idempotent if files already exist on disk).
    Workspace.init(request.app.state.root / slug, name=body.name or slug, description=body.description)
    info = control.create_workspace(slug, body.name or slug, body.description)
    control.log_audit("workspace_created", {"slug": slug}, actor=admin.username)
    return serialize.dump(info)


@workspaces_router.patch("/{slug}")
def update_workspace(
    slug: str, body: WorkspaceUpdateRequest, request: Request, admin: Superadmin
) -> dict:
    control = _require_multi(request)
    if control.get_workspace(slug) is None:
        raise HTTPException(status_code=404, detail=f"Workspace not found: {slug!r}")
    control.update_workspace(slug, name=body.name, description=body.description)
    control.log_audit("workspace_updated", {"slug": slug}, actor=admin.username)
    info = control.get_workspace(slug)
    assert info is not None
    return serialize.dump(info)


@workspaces_router.delete("/{slug}")
def delete_workspace(slug: str, request: Request, admin: Superadmin) -> dict:
    control = _require_multi(request)
    if control.get_workspace(slug) is None:
        raise HTTPException(status_code=404, detail=f"Workspace not found: {slug!r}")
    control.delete_workspace(slug)
    request.app.state.ws_cache.pop(slug, None)
    # Files on disk are intentionally left in place; unregistering is reversible.
    # HAZARD: re-creating the same slug later re-exposes this data — an operator
    # reusing a slug for a *different* tenant must purge <root>/<slug>/ first.
    control.log_audit("workspace_deleted", {"slug": slug}, actor=admin.username)
    return {
        "ok": True,
        "note": f"Data files under the '{slug}' directory were left on disk. "
        "Re-creating this slug will re-expose them; delete the directory manually "
        "before reusing the slug for different data.",
    }


@workspaces_router.get("/{slug}/members")
def list_members(slug: str, request: Request, admin: Superadmin) -> list[dict]:
    control = _require_multi(request)
    if control.get_workspace(slug) is None:
        raise HTTPException(status_code=404, detail=f"Workspace not found: {slug!r}")
    return control.list_members(slug)


@workspaces_router.put("/{slug}/members")
def set_member(slug: str, body: MemberRequest, request: Request, admin: Superadmin) -> dict:
    control = _require_multi(request)
    if control.get_workspace(slug) is None:
        raise HTTPException(status_code=404, detail=f"Workspace not found: {slug!r}")
    if identity_store(request).get_user(body.username) is None:
        raise HTTPException(status_code=400, detail=f"Unknown user: {body.username!r}")
    control.set_member(slug, body.username.lower(), body.role)
    control.log_audit(
        "workspace_member_set",
        {"slug": slug, "username": body.username, "role": body.role.value},
        actor=admin.username,
    )
    return {"slug": slug, "username": body.username.lower(), "role": body.role.value}


@workspaces_router.delete("/{slug}/members/{username}")
def remove_member(slug: str, username: str, request: Request, admin: Superadmin) -> dict:
    control = _require_multi(request)
    if control.get_workspace(slug) is None:
        raise HTTPException(status_code=404, detail=f"Workspace not found: {slug!r}")
    control.remove_member(slug, username)
    control.log_audit(
        "workspace_member_removed", {"slug": slug, "username": username}, actor=admin.username
    )
    return {"ok": True}
