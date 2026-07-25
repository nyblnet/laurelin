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
import socket
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
from laurelin.api.schedule_routes import schedules_router
from laurelin.api.source_routes import sources_router
from laurelin.catalog import DatasetCatalog
from laurelin.core.auth import AuthService
from laurelin.core.config import Workspace
from laurelin.core.control import ControlStore
from laurelin.core import scheduler
from laurelin.core.db import MetadataStore
from laurelin.core.limits import QueryRejected, QueryTimeout, QueryTooLarge

_STATIC_DIR = Path(__file__).resolve().parents[1] / "ui" / "static"


def _exc_message(exc: BaseException) -> str:
    if exc.args and isinstance(exc.args[0], str):
        return exc.args[0]
    return str(exc)


def _scheduler_targets(app: FastAPI) -> list:
    """The workspaces this replica should poll, each with a way to run an action.

    Yields ``(label, store, run_action)``. The scheduler stays mode-agnostic:
    one workspace in single mode, every registered one in multi mode.
    """
    from laurelin.core.config import Workspace

    def runner(workspace: Workspace, store: MetadataStore, catalog: DatasetCatalog):
        def run_action(schedule) -> Optional[str]:
            if schedule.action == "sync":
                from laurelin.connectors import sync_source

                source = store.get_source(schedule.source)
                if source is None:
                    raise RuntimeError(f"Unknown source: {schedule.source!r}")
                sync_source(catalog, store, source, actor="scheduler")
                return None

            from laurelin.transforms import Builder, collect_transforms

            builder = Builder(
                workspace, catalog, store, collect_transforms(workspace.pipelines_dir)
            )
            targets = list(schedule.targets) or None
            builder.plan(targets)  # fail before creating a record
            build = store.create_build(targets or [])
            # Reuse the async path: the executor runs it, leases keep it
            # exactly-once, and the schedule records which build it started.
            app.state.build_executor.submit(
                builder.execute, build.id, targets, app.state.worker_id
            )
            return build.id

        return run_action

    st = app.state
    if st.mode == "single":
        return [("workspace", st.store, runner(st.workspace, st.store, st.catalog))]

    from laurelin.api.context import open_workspace_store

    targets = []
    for info in st.control.list_workspaces():
        try:
            workspace = Workspace(st.root / info.slug)
            store = open_workspace_store(st.root, st.control, info.slug)
            catalog = DatasetCatalog(workspace, store)
            targets.append((info.slug, store, runner(workspace, store, catalog)))
        except Exception:  # noqa: BLE001 - a broken workspace must not stop the rest
            continue
    return targets


def _finalize(app: FastAPI) -> FastAPI:
    """Add the middleware, error handlers, routers, and static mount shared by
    both single- and multi-workspace apps."""
    from concurrent.futures import ThreadPoolExecutor

    from laurelin.core.oidc import OIDCConfig, OIDCProvider
    from laurelin.core.saml import SAMLConfig, SAMLProvider

    # Builds run here, off the request thread (POST /builds returns a pending
    # build immediately). Version allocation is safe under concurrency (the
    # rename mutex in DatasetCatalog), so overlapping builds are wasteful but
    # never corrupting.
    app.state.build_executor = ThreadPoolExecutor(
        max_workers=int(os.environ.get("LAURELIN_BUILD_WORKERS", "2")),
        thread_name_prefix="laurelin-build",
    )
    # Identifies this process when claiming a build lease, so that with several
    # replicas serving a workspace a build still executes exactly once. The
    # hostname makes an abandoned lease traceable to a pod.
    app.state.worker_id = os.environ.get(
        "LAURELIN_WORKER_ID", f"{socket.gethostname()}:{os.getpid()}"
    )
    app.router.on_shutdown.append(
        lambda: app.state.build_executor.shutdown(wait=False)
    )

    # The scheduler runs in every replica; leases make firing exactly-once, so
    # no leader election is needed and a lost replica costs at most one window.
    app.state.scheduler = None
    if scheduler.enabled():
        app.state.scheduler = scheduler.Scheduler(
            open_stores=lambda: _scheduler_targets(app),
            worker_id=app.state.worker_id,
            poll_seconds=scheduler.poll_seconds(),
        )
        app.router.on_startup.append(app.state.scheduler.start)
        app.router.on_shutdown.append(app.state.scheduler.stop)

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

    # Resource limits. These are distinguished from ordinary 400s because the
    # remedy differs: narrow the query, versus retry it unchanged.
    @app.exception_handler(QueryTimeout)
    async def query_timeout_handler(request: Request, exc: QueryTimeout):
        return JSONResponse(status_code=504, content={"detail": _exc_message(exc)})

    @app.exception_handler(QueryTooLarge)
    async def query_too_large_handler(request: Request, exc: QueryTooLarge):
        return JSONResponse(status_code=400, content={"detail": _exc_message(exc)})

    @app.exception_handler(QueryRejected)
    async def query_rejected_handler(request: Request, exc: QueryRejected):
        # Refuse fast with Retry-After rather than queueing until everything is
        # slow — a rejected query is recoverable, a saturated replica isn't.
        return JSONResponse(
            status_code=503,
            content={"detail": _exc_message(exc)},
            headers={"Retry-After": "2"},
        )

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
    app.include_router(sources_router, prefix="/api/v1")
    app.include_router(schedules_router, prefix="/api/v1")
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
    database_url: Optional[str] = None,
) -> FastAPI:
    """Single-workspace server (``serve --workspace``). Identity lives in the
    workspace's metadata store.

    That store is the workspace's ``metadata.db`` by default. Pass
    ``database_url`` (or set ``LAURELIN_DATABASE_URL``) to keep it in
    PostgreSQL instead — required if you want to run more than one replica,
    since SQLite cannot be shared safely across hosts."""
    no_auth = no_auth or os.environ.get("LAURELIN_NO_AUTH") == "1"
    lock_pipelines = lock_pipelines or os.environ.get("LAURELIN_LOCK_PIPELINES") == "1"
    database_url = database_url or os.environ.get("LAURELIN_DATABASE_URL")
    store = MetadataStore(database_url or workspace.metadata_path)
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
