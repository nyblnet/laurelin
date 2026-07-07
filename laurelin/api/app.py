"""Application factory: ``create_app(workspace) -> FastAPI``.

Catalog and metadata store are constructed once per app; pipelines and
ontology are re-read per request (see routes.py dependencies). The static UI
is mounted at ``/`` after the API routes so ``/api`` and ``/health`` win.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        """If LAURELIN_TOKEN is set, /api/ paths require a matching bearer token."""
        token = os.environ.get("LAURELIN_TOKEN")
        if token and request.url.path.startswith("/api/"):
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {token}":
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Missing or invalid bearer token"},
                )
        return await call_next(request)

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
