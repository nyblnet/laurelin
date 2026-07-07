"""Application factory: ``create_app(workspace) -> FastAPI``.

Catalog and metadata store are constructed once per app; pipelines and
ontology are re-read per request (see routes.py dependencies). The static UI
is mounted at ``/`` after the API routes so ``/api`` and ``/health`` win.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import laurelin
from laurelin.api.routes import router
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore

_STATIC_DIR = Path(__file__).resolve().parents[1] / "ui" / "static"


def _exc_message(exc: BaseException) -> str:
    if exc.args and isinstance(exc.args[0], str):
        return exc.args[0]
    return str(exc)


def create_app(workspace: Workspace) -> FastAPI:
    store = MetadataStore(workspace.metadata_path)
    catalog = DatasetCatalog(workspace, store)

    app = FastAPI(
        title="Laurelin",
        description=f"Laurelin workspace API — {workspace.name}",
        version=laurelin.__version__,
        docs_url="/docs",
    )
    app.state.workspace = workspace
    app.state.store = store
    app.state.catalog = catalog

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        """If LAURELIN_TOKEN is set, /api/ paths (and the API docs, which list
        the route surface) require a matching bearer token. OPTIONS is exempt
        so CORS preflights — which never carry Authorization — can succeed."""
        token = os.environ.get("LAURELIN_TOKEN")
        path = request.url.path
        protected = (
            path.startswith("/api/")
            or path == "/openapi.json"
            or path.startswith("/docs")
            or path.startswith("/redoc")
        )
        if token and protected and request.method != "OPTIONS":
            scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not secrets.compare_digest(
                credentials.strip(), token
            ):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Missing or invalid bearer token"},
                )
        return await call_next(request)

    # Registered after the auth middleware so CORS is the outermost layer and
    # its headers are applied to 401 responses too.
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
        return {"status": "ok", "version": laurelin.__version__}

    app.include_router(router, prefix="/api/v1")

    if _STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")

    return app
