"""Mode-aware request context shared by the routers.

The app runs in one of two modes (``app.state.mode``):

- ``"single"``: one workspace bound at startup (``serve --workspace``). Identity
  lives in that workspace's ``metadata.db``; the user's role is their account
  role. This is the original behavior — unchanged.
- ``"multi"``: many workspaces under a root (``serve --root``). Identity is
  global (``app.state.control``); the active workspace is selected per request
  via the ``X-Laurelin-Workspace`` header or ``laurelin_workspace`` cookie, and
  a user's *effective* role is their membership role in that workspace (or admin
  if they are a superadmin).

These helpers hide the difference so the route handlers stay mode-agnostic.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request

from laurelin.catalog import DatasetCatalog
from laurelin.core.auth import AuthService
from laurelin.core.backend import is_postgres_url
from laurelin.core.config import Workspace
from laurelin.core.control import ControlStore
from laurelin.core.db import MetadataStore

WORKSPACE_HEADER = "x-laurelin-workspace"
WORKSPACE_COOKIE = "laurelin_workspace"


def is_multi(request: Request) -> bool:
    return request.app.state.mode == "multi"


def control(request: Request) -> ControlStore:
    return request.app.state.control


def identity_auth(request: Request) -> AuthService:
    """The AuthService that owns identity (users/sessions/tokens)."""
    st = request.app.state
    return st.control_auth if st.mode == "multi" else st.auth


def identity_store(request: Request) -> MetadataStore:
    st = request.app.state
    return st.control if st.mode == "multi" else st.store


def active_slug(request: Request) -> str:
    slug = request.headers.get(WORKSPACE_HEADER) or request.cookies.get(WORKSPACE_COOKIE)
    if not slug:
        raise HTTPException(
            status_code=400,
            detail="No workspace selected (set the X-Laurelin-Workspace header "
            "or laurelin_workspace cookie)",
        )
    return slug


def _ensure_authenticated(request: Request) -> None:
    """Require a valid credential BEFORE any workspace-existence check runs, so an
    unauthenticated caller can't distinguish an existing workspace (would 401)
    from an absent one (would 404) — i.e. no anonymous slug enumeration. This
    matters because on some routes the store/catalog dependency resolves before
    the auth dependency."""
    st = request.app.state
    if st.no_auth:
        return
    if st.control.count_users() == 0:
        raise HTTPException(status_code=401, detail="setup required")
    # Lazy import avoids a context <-> auth_routes import cycle.
    from laurelin.api.auth_routes import resolve_credential

    if resolve_credential(request) is None:
        raise HTTPException(status_code=401, detail="Not authenticated")


def workspace_schema(slug: str) -> str:
    """Postgres schema holding one workspace's metadata."""
    return f"ws_{slug}"


def open_workspace_store(root: Path, control: MetadataStore, slug: str) -> MetadataStore:
    """The metadata store for a workspace.

    On a Postgres control plane, each workspace gets its own *schema* in the
    same database — so N replicas share one HA store. With a SQLite control
    plane (embedded mode) the workspace keeps its own file, which is correct
    for a single process but is why that shape cannot be scaled out.
    """
    if is_postgres_url(str(control.path)):
        return MetadataStore(str(control.path), schema=workspace_schema(slug))
    return MetadataStore(Workspace(root / slug).metadata_path)


def _bundle(request: Request, slug: str) -> tuple[Workspace, MetadataStore, DatasetCatalog]:
    """Load (and cache) the Workspace + store + catalog for a workspace slug."""
    st = request.app.state
    cache = st.ws_cache
    if slug not in cache:
        ws = Workspace(st.root / slug)
        if not ws.marker_path.exists():
            # Registered but never initialized on disk (e.g. manual DB edit).
            Workspace.init(ws.root, name=slug)
        store = open_workspace_store(st.root, st.control, slug)
        cache[slug] = (ws, store, DatasetCatalog(ws, store))
    return cache[slug]


def _resolve(request: Request, index: int):
    """Shared multi-mode resolution: authenticate, verify the workspace exists,
    then return the requested element of its (Workspace, store, catalog) bundle."""
    slug = active_slug(request)
    _ensure_authenticated(request)
    if request.app.state.control.get_workspace(slug) is None:
        raise HTTPException(status_code=404, detail=f"Unknown workspace: {slug!r}")
    return _bundle(request, slug)[index]


def active_workspace(request: Request) -> Workspace:
    st = request.app.state
    return st.workspace if st.mode == "single" else _resolve(request, 0)


def active_store(request: Request) -> MetadataStore:
    st = request.app.state
    return st.store if st.mode == "single" else _resolve(request, 1)


def active_catalog(request: Request) -> DatasetCatalog:
    st = request.app.state
    return st.catalog if st.mode == "single" else _resolve(request, 2)
