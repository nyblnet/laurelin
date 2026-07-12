"""Application factory: ``create_app(workspace) -> FastAPI``.

Catalog and metadata store are constructed once per app; pipelines and
ontology are re-read per request (see routes.py dependencies). The static UI
is mounted at ``/`` after the API routes so ``/api`` and ``/health`` win.

Auth is ON by default (see docs/ARCHITECTURE.md): API routes resolve
credentials through FastAPI dependencies (auth_routes.py); this module only
adds the middleware that gates /docs, /redoc and /openapi.json — those are
plain Starlette routes with no dependency hooks.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import laurelin
from laurelin.api.auth_routes import (
    auth_router,
    groups_router,
    resolve_credential,
    tokens_router,
    users_router,
    workspaces_router,
)
from laurelin.api.routes import router
from laurelin.api.scim_routes import scim_router
from laurelin.catalog import DatasetCatalog
from laurelin.core.auth import AuthService
from laurelin.core.config import Workspace
from laurelin.core.control import ControlStore
from laurelin.core.db import MetadataStore

_STATIC_DIR = Path(__file__).resolve().parents[1] / "ui" / "static"


def _exc_message(exc: BaseException) -> str:
    if exc.args and isinstance(exc.args[0], str):
        return exc.args[0]
    return str(exc)


def _finalize(app: FastAPI) -> FastAPI:
    """Add the middleware, error handlers, routers, and static mount shared by
    both single- and multi-workspace apps."""
    from laurelin.core.oidc import OIDCConfig, OIDCProvider
    from laurelin.core.saml import SAMLConfig, SAMLProvider

    app.state.oidc_config = OIDCConfig.from_env()
    app.state.oidc_provider = OIDCProvider(app.state.oidc_config)
    app.state.saml_config = SAMLConfig.from_env()
    app.state.saml_provider = SAMLProvider(app.state.saml_config)

    def _identity_store():
        st = app.state
        return st.control if st.mode == "multi" else st.store

    @app.middleware("http")
    async def guard_api_docs(request: Request, call_next):
        """The API docs list the whole route surface, so they are gated behind
        the same credentials as /api/ routes. OPTIONS is exempt so CORS
        preflights — which never carry credentials — can succeed."""
        path = request.url.path
        gated = (
            path == "/openapi.json"
            or path.startswith("/docs")
            or path.startswith("/redoc")
        )
        if gated and not app.state.no_auth and request.method != "OPTIONS":
            if _identity_store().count_users() == 0:
                return JSONResponse(status_code=401, content={"detail": "setup required"})
            if resolve_credential(request) is None:
                return JSONResponse(
                    status_code=401, content={"detail": "Not authenticated"}
                )
        return await call_next(request)

    # Registered after the docs guard so CORS is the outermost layer and its
    # headers are applied to 401/403 responses too.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        msgs = "; ".join(
            f"{'.'.join(str(loc) for loc in e.get('loc', []))}: {e.get('msg', 'invalid')}"
            for e in exc.errors()
        )
        return JSONResponse(status_code=400, content={"detail": msgs or "Invalid request"})

    @app.exception_handler(KeyError)
    async def key_error_handler(request: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"detail": _exc_message(exc)})

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": _exc_message(exc)})

    @app.get("/health")
    def health() -> dict:
        """Liveness: the process is up."""
        return {"status": "ok", "version": laurelin.__version__}

    @app.get("/health/ready")
    def ready():
        """Readiness: the identity/control store is reachable (for k8s probes /
        load balancers — a replica that can't reach Postgres should not serve)."""
        try:
            _identity_store().count_users()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=503, content={"status": "unavailable", "detail": str(exc)[:120]})
        return {"status": "ready"}

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")
    app.include_router(tokens_router, prefix="/api/v1")
    app.include_router(groups_router, prefix="/api/v1")
    app.include_router(workspaces_router, prefix="/api/v1")
    app.include_router(scim_router, prefix="/api/v1")
    app.include_router(router, prefix="/api/v1")

    if _STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")

    return app


def create_app(
    workspace: Workspace,
    *,
    no_auth: bool = False,
    secure_cookies: bool = False,
    lock_pipelines: bool = False,
) -> FastAPI:
    """Single-workspace server (``serve --workspace``). Identity lives in the
    workspace's metadata.db; unchanged from earlier versions."""
    no_auth = no_auth or os.environ.get("LAURELIN_NO_AUTH") == "1"
    lock_pipelines = lock_pipelines or os.environ.get("LAURELIN_LOCK_PIPELINES") == "1"
    store = MetadataStore(workspace.metadata_path)
    catalog = DatasetCatalog(workspace, store)

    app = FastAPI(
        title="Laurelin",
        description=f"Laurelin workspace API — {workspace.name}",
        version=laurelin.__version__,
        docs_url="/docs",
    )
    app.state.mode = "single"
    app.state.workspace = workspace
    app.state.store = store
    app.state.catalog = catalog
    app.state.no_auth = no_auth
    app.state.secure_cookies = secure_cookies
    app.state.lock_pipelines = lock_pipelines
    app.state.auth = AuthService(store)
    return _finalize(app)


def create_server_app(
    root: Path,
    *,
    control_url: Optional[str] = None,
    no_auth: bool = False,
    secure_cookies: bool = False,
    lock_pipelines: bool = False,
) -> FastAPI:
    """Multi-workspace server (``serve --root``). Global identity + a workspace
    registry live in the control store — ``<root>/control.db`` (SQLite) by
    default, or a PostgreSQL database when ``control_url`` /
    ``LAURELIN_CONTROL_DATABASE_URL`` is a ``postgresql://`` URL (recommended for
    real multi-tenant deployments). Each workspace under ``<root>/<slug>`` keeps
    its own data and ACLs. The active workspace is selected per request via the
    X-Laurelin-Workspace header or laurelin_workspace cookie."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    no_auth = no_auth or os.environ.get("LAURELIN_NO_AUTH") == "1"
    lock_pipelines = lock_pipelines or os.environ.get("LAURELIN_LOCK_PIPELINES") == "1"
    control_url = control_url or os.environ.get("LAURELIN_CONTROL_DATABASE_URL")
    control = ControlStore(control_url) if control_url else ControlStore(root / "control.db")

    app = FastAPI(
        title="Laurelin",
        description="Laurelin multi-workspace server",
        version=laurelin.__version__,
        docs_url="/docs",
    )
    app.state.mode = "multi"
    app.state.root = root
    app.state.control = control
    app.state.control_auth = AuthService(control)
    app.state.ws_cache = {}  # slug -> (Workspace, MetadataStore, DatasetCatalog)
    app.state.no_auth = no_auth
    app.state.secure_cookies = secure_cookies
    app.state.lock_pipelines = lock_pipelines
    return _finalize(app)
