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

import logging
import os
import socket
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import laurelin
from laurelin.api import iceberg_routes  # noqa: F401 - registers routes on the shared router
from laurelin.api.app_routes import apps_router
from laurelin.api.auth_routes import (
    auth_router,
    groups_router,
    resolve_credential,
    tokens_router,
    users_router,
    workspaces_router,
)
from laurelin.api.context import WORKSPACE_COOKIE, WORKSPACE_HEADER
from laurelin.api.engine_routes import engines_router
from laurelin.api.export_routes import export_router
from laurelin.api.routes import router
from laurelin.api.schedule_routes import schedules_router
from laurelin.api.scim_routes import scim_router
from laurelin.api.source_routes import sources_router
from laurelin.catalog import DatasetCatalog
from laurelin.core import logging as laurelin_logging
from laurelin.core import metrics, scheduler, serialize
from laurelin.core.auth import AuthService
from laurelin.core.config import Workspace
from laurelin.core.control import ControlStore
from laurelin.core.db import MetadataStore
from laurelin.core.failure import (
    Failure,
    Phase,
    first_party_message,
    is_first_party,
)
from laurelin.core.federation import FederationError
from laurelin.core.fileperms import mkdir_private
from laurelin.core.limits import QueryRejected, QueryTimeout, QueryTooLarge
from laurelin.core.models import Role
from laurelin.export import (
    ExportRefused,
    ImportRefused,
    NeedsCredentials,
    require_pipelines_acknowledged,
)

log = logging.getLogger("laurelin.api")

_STATIC_DIR = Path(__file__).resolve().parents[1] / "ui" / "static"


# Kept as a name because a dozen handlers below call it; the implementation
# moved to core/failure.py so `routes.py` can share it without importing `app`.
_exc_message = first_party_message


def _catch_all_detail(exc: BaseException, phase: Phase) -> str:
    """The body for an exception nobody caught. R1's backstop.

    These two handlers exist so an ordinary `raise ValueError("...")` in a route
    becomes a 400 with a useful message. They also, until this change, turned
    **any** library's exception into a response body carrying that library's raw
    words, because `pyarrow.lib.ArrowInvalid` is a `ValueError` and
    `pyarrow.lib.ArrowKeyError` is a `KeyError`. Measured: an editor read the
    operator's S3 warehouse credential out of a 400 on
    `POST /datasets/{name}/iceberg`.

    Catch sites are still where a failure should be classified — a `Failure`
    built here knows nothing about which subject or which phase. This is the
    net under them, and it fails closed: first-party message, or nothing but a
    code and a `detail_ref`.
    """
    if is_first_party(exc):
        return _exc_message(exc)
    return Failure.from_exception(exc, phase=phase, subject="request").render_brief()


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

            # Imported schedules land disabled, but nothing stops an admin
            # enabling one before reading the pipelines that arrived with it —
            # and collect_transforms execs every file it finds. The guard belongs
            # on this path too, or the acknowledgement is only a UI convention.
            require_pipelines_acknowledged(workspace)
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
    async def audience_middleware(request: Request, call_next):
        """Publish the caller's effective role for the serializer. R2's plumbing.

        Set here, in an async middleware, rather than in a dependency: FastAPI
        runs sync dependencies and sync handlers in *separate* threadpool
        context copies, so a ContextVar set in a dependency would not reach the
        handler. Set in a middleware it propagates into every child context —
        verified on this tree, including sync handlers, which is what all of
        Laurelin's are.

        Resolution failures are swallowed on purpose. The default is
        `Role.viewer`, the lowest privilege, so a request whose role cannot be
        worked out serializes the *least*. Forgetting redacts more, never less;
        a rule whose failure mode is "too little disclosed" is the only kind
        that survives a round of attackers.
        """
        token = serialize.set_effective_role(_effective_role_for(request))
        try:
            return await call_next(request)
        finally:
            serialize.reset_effective_role(token)

    def _effective_role_for(request: Request) -> Role:
        """The caller's role in the active workspace, or viewer if unknown.

        Mirrors `auth_routes.require_user`: in single mode the account role, in
        multi mode the membership role (admin for a superadmin). It runs before
        the route's own gate, so it must not raise — a 401 or 403 is that gate's
        job, and this only decides how much of a *successful* response to fill
        in.
        """
        try:
            st = request.app.state
            if st.no_auth:
                return Role.admin
            user = resolve_credential(request)
            if user is None:
                return Role.viewer
            if st.mode != "multi":
                return user.role
            if user.superadmin:
                return Role.admin
            slug = (request.headers.get(WORKSPACE_HEADER)
                    or request.cookies.get(WORKSPACE_COOKIE))
            if not slug:
                return Role.viewer
            return st.control.member_role(slug, user.username) or Role.viewer
        except Exception:  # noqa: BLE001 - never fail a request over this
            return Role.viewer

    @app.middleware("http")
    async def observe(request: Request, call_next):
        """Assign a request id, time the request, and count it by route.

        The *route template* is the label — never the concrete path — so
        cardinality stays bounded by the route table rather than by however
        many datasets exist.
        """
        rid = request.headers.get("X-Request-ID") or laurelin_logging.new_request_id()
        token = laurelin_logging.request_id.set(rid)
        ws_token = laurelin_logging.workspace_slug.set(
            request.headers.get("X-Laurelin-Workspace")
        )
        started = time.perf_counter()
        status = "500"
        try:
            response = await call_next(request)
            status = str(response.status_code)
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            # The route template ("/datasets/{name}/rows"), not the concrete
            # path. FastAPI reports it without the router's /api/v1 prefix,
            # which is constant across the API and so loses nothing.
            # "unmatched" covers 404s, which have no route at all.
            route = request.scope.get("route")
            template = (
                getattr(route, "path_format", None)
                or getattr(route, "path", None)
                or "unmatched"
            )
            elapsed = time.perf_counter() - started
            metrics.http_requests.labels(
                method=request.method, route=template, status=status
            ).inc()
            metrics.http_duration.labels(
                method=request.method, route=template
            ).observe(elapsed)
            laurelin_logging.request_id.reset(token)
            laurelin_logging.workspace_slug.reset(ws_token)

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
        return JSONResponse(
            status_code=404, content={"detail": _catch_all_detail(exc, Phase.describe)}
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        return JSONResponse(
            status_code=400, content={"detail": _catch_all_detail(exc, Phase.execute)}
        )

    @app.exception_handler(FederationError)
    async def federation_error_handler(request: Request, exc: FederationError):
        """A scan of a federated table failed and no route caught it.

        `GET /datasets/{name}/rows` 500'd for every role when an upstream table
        was renamed away — the ordinary case — because `FederationError` had no
        handler anywhere. Starlette's bare 500 disclosed nothing, but the
        message it would have carried interpolated DuckDB's sentence, including
        the `LINE 1:` echo of the `postgres_scan(...)` call and therefore the
        DSN. A 502 with a `Failure` is both the honest status and the safe body.
        """
        failure = getattr(exc, "failure", None) or Failure.from_exception(
            exc, phase=Phase.execute, subject="dataset"
        )
        return JSONResponse(
            status_code=502,
            content={"detail": serialize.detail_for(failure, Role.admin)},
        )

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

    # Portability refusals. Every one of these is a deliberate stop rather than
    # a fault, and each message names the flag or the act that clears it — so
    # 409 (the state of the thing is wrong) rather than 400 (you typed it
    # wrong) or 500 (we broke). NeedsCredentials is the load-bearing one: a
    # dataset whose bytes did not survive the migration must refuse loudly,
    # because zero rows in a governance product is indistinguishable from a
    # working row policy.
    @app.exception_handler(ExportRefused)
    async def export_refused_handler(request: Request, exc: ExportRefused):
        return JSONResponse(status_code=409, content={"detail": _exc_message(exc)})

    @app.exception_handler(ImportRefused)
    async def import_refused_handler(request: Request, exc: ImportRefused):
        return JSONResponse(status_code=409, content={"detail": _exc_message(exc)})

    @app.exception_handler(NeedsCredentials)
    async def needs_credentials_handler(request: Request, exc: NeedsCredentials):
        return JSONResponse(status_code=409, content={"detail": _exc_message(exc)})

    @app.get("/health")
    def health() -> dict:
        """Liveness: the process is up."""
        return {"status": "ok", "version": laurelin.__version__}

    @app.get("/metrics")
    def prometheus_metrics(request: Request):
        """Prometheus exposition.

        Credentialed by default: metric *names* are harmless, but counts leak
        activity patterns, so scraping without auth is opt-in
        (``LAURELIN_METRICS_PUBLIC=1``) and meant for a port users can't reach.
        """
        if not metrics.enabled():
            detail = (
                "Metrics need the optional extra: pip install 'laurelin[metrics]'"
                if not metrics.available()
                else "Metrics are disabled (LAURELIN_METRICS=0)"
            )
            return JSONResponse(status_code=501, content={"detail": detail})
        if not metrics.public() and not app.state.no_auth:
            if resolve_credential(request) is None:
                return JSONResponse(
                    status_code=401, content={"detail": "Not authenticated"}
                )
        return Response(content=metrics.render(), media_type=metrics.content_type())

    @app.get("/health/ready")
    def ready():
        """Readiness: the identity/control store is reachable (for k8s probes /
        load balancers — a replica that can't reach Postgres should not serve)."""
        try:
            _identity_store().count_users()
        except Exception as exc:  # noqa: BLE001
            # R1, and this one is unauthenticated: `str(exc)[:120]` handed an
            # anonymous caller the first 120 characters of whatever the store
            # driver said, which on a Postgres control plane begins with the
            # connection string. A readiness probe needs one bit.
            log.warning("readiness probe failed", exc_info=exc)
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return {"status": "ready"}

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")
    app.include_router(tokens_router, prefix="/api/v1")
    app.include_router(groups_router, prefix="/api/v1")
    app.include_router(workspaces_router, prefix="/api/v1")
    app.include_router(scim_router, prefix="/api/v1")
    app.include_router(sources_router, prefix="/api/v1")
    app.include_router(schedules_router, prefix="/api/v1")
    app.include_router(engines_router, prefix="/api/v1")
    app.include_router(apps_router, prefix="/api/v1")
    app.include_router(export_router, prefix="/api/v1")
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
    # control.db holds *global* identity — every session token and API token on
    # the server, not one workspace's. It is created 0600 by the SQLite
    # backend; the root that contains it is 0700 when we are the ones creating
    # it, and left alone when it already exists.
    mkdir_private(root)
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
