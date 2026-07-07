"""API routers and per-request dependencies.

Catalog and store live on ``app.state`` (built once in ``create_app``); the
transform registry and ontology are rebuilt per request so edits to
``pipelines/`` and ``ontology/`` show up without a server restart.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel, Field

from laurelin.api.auth_routes import require_editor, require_user, require_viewer
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import DatasetVersionInfo, User
from laurelin.ontology import OntologyService, load_ontology
from laurelin.transforms import Builder, TransformRegistry, collect_transforms

router = APIRouter()

# RBAC guards (see docs/ARCHITECTURE.md): reads need viewer, mutations editor.
VIEWER = Depends(require_viewer)
EDITOR = Depends(require_editor)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def get_workspace(request: Request) -> Workspace:
    return request.app.state.workspace


def get_store(request: Request) -> MetadataStore:
    return request.app.state.store


def get_catalog(request: Request) -> DatasetCatalog:
    return request.app.state.catalog


def get_registry(workspace: Annotated[Workspace, Depends(get_workspace)]) -> TransformRegistry:
    return collect_transforms(workspace.pipelines_dir)


def get_ontology_service(
    workspace: Annotated[Workspace, Depends(get_workspace)],
    catalog: Annotated[DatasetCatalog, Depends(get_catalog)],
    store: Annotated[MetadataStore, Depends(get_store)],
) -> OntologyService:
    ontology = load_ontology(workspace.ontology_dir)
    return OntologyService(workspace, catalog, store, ontology)


def get_actor(user: Annotated[User, Depends(require_user)]) -> str:
    """Audit/edit attribution: the authenticated username. In --no-auth mode
    the implicit admin's username honors the X-Laurelin-User header."""
    return user.username


WorkspaceDep = Annotated[Workspace, Depends(get_workspace)]
StoreDep = Annotated[MetadataStore, Depends(get_store)]
CatalogDep = Annotated[DatasetCatalog, Depends(get_catalog)]
RegistryDep = Annotated[TransformRegistry, Depends(get_registry)]
OntologyDep = Annotated[OntologyService, Depends(get_ontology_service)]
ActorDep = Annotated[str, Depends(get_actor)]


def _dump(model: BaseModel) -> dict:
    return model.model_dump(mode="json", by_alias=True)


def _version_info(
    store: MetadataStore, name: str, version: Optional[int]
) -> DatasetVersionInfo:
    if store.get_dataset(name) is None:
        raise KeyError(f"Dataset not found: {name!r}")
    info = store.get_version(name, version)
    if info is None:
        if version is None:
            raise KeyError(f"Dataset {name!r} has no versions")
        raise KeyError(f"Dataset {name!r} has no version {version}")
    return info


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class DatasetCreateRequest(BaseModel):
    name: str
    description: str = ""


class BuildRequest(BaseModel):
    targets: Optional[list[str]] = None


class ActionApplyRequest(BaseModel):
    pk: Optional[str] = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class QueryRequest(BaseModel):
    sql: str
    max_rows: int = Field(default=1000, ge=1, le=100_000)


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

@router.get("/workspace", dependencies=[VIEWER])
def get_workspace_info(workspace: WorkspaceDep) -> dict:
    return {
        "name": workspace.name,
        "description": workspace.description,
        "root": str(workspace.root),
    }


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------

@router.get("/datasets", dependencies=[VIEWER])
def list_datasets(store: StoreDep) -> list[dict]:
    return [_dump(d) for d in store.list_datasets()]


@router.post("/datasets", dependencies=[EDITOR])
def create_dataset(
    body: DatasetCreateRequest, catalog: CatalogDep, store: StoreDep, actor: ActorDep
) -> dict:
    info = catalog.create_dataset(body.name, body.description)
    store.log_audit("dataset_created", {"dataset": info.name}, actor=actor)
    return _dump(info)


@router.get("/datasets/{name}", dependencies=[VIEWER])
def get_dataset(name: str, store: StoreDep) -> dict:
    info = store.get_dataset(name)
    if info is None:
        raise KeyError(f"Dataset not found: {name!r}")
    result = _dump(info)
    result["versions"] = [_dump(v) for v in store.list_versions(name)]
    return result


@router.get("/datasets/{name}/schema", dependencies=[VIEWER])
def get_dataset_schema(
    name: str, store: StoreDep, version: Optional[int] = None
) -> list[dict]:
    info = _version_info(store, name, version)
    return [c.model_dump() for c in info.schema_]


@router.get("/datasets/{name}/rows", dependencies=[VIEWER])
def get_dataset_rows(
    name: str,
    catalog: CatalogDep,
    store: StoreDep,
    limit: int = Query(100, ge=0, le=10_000),
    offset: int = Query(0, ge=0),
    version: Optional[int] = None,
) -> dict:
    info = _version_info(store, name, version)
    rows = catalog.rows(name, limit=limit, offset=offset, version=version)
    return {"rows": rows, "row_count": info.row_count}


@router.post("/query", dependencies=[VIEWER])
def run_query(body: QueryRequest, catalog: CatalogDep) -> dict:
    """Run a read-only SQL query over the workspace's datasets (each exposed as
    a view named after the dataset). A syntax or binder error becomes a 400."""
    try:
        return catalog.query(body.sql, max_rows=body.max_rows)
    except Exception as exc:  # duckdb parser/binder/runtime errors
        raise HTTPException(status_code=400, detail=str(exc).strip())


@router.post("/datasets/{name}/upload", dependencies=[EDITOR])
def upload_dataset_file(
    name: str,
    catalog: CatalogDep,
    store: StoreDep,
    actor: ActorDep,
    file: UploadFile = File(...),
) -> dict:
    suffix = Path(file.filename or "").suffix
    max_bytes = int(os.environ.get("LAURELIN_MAX_UPLOAD_MB", "1024")) * 1024 * 1024
    written = 0
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        while chunk := file.file.read(1 << 20):
            written += len(chunk)
            if written > max_bytes:
                tmp.close()
                tmp_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"Upload exceeds {max_bytes // (1024 * 1024)} MB limit "
                    "(set LAURELIN_MAX_UPLOAD_MB to raise it)",
                )
            tmp.write(chunk)
    try:
        info = catalog.upload_file(name, tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    store.log_audit(
        "dataset_uploaded",
        {
            "dataset": name,
            "filename": file.filename,
            "version": info.version,
            "row_count": info.row_count,
        },
        actor=actor,
    )
    return _dump(info)


# ---------------------------------------------------------------------------
# Transforms, builds, lineage
# ---------------------------------------------------------------------------

@router.get("/transforms", dependencies=[VIEWER])
def list_transforms(registry: RegistryDep) -> list[dict]:
    return [
        {
            "name": spec.name,
            "output": spec.output.dataset,
            "inputs": [inp.dataset for inp in spec.inputs.values()],
            "kind": spec.kind,
        }
        for spec in registry.all()
    ]


@router.post("/builds", dependencies=[EDITOR])
def run_build(
    workspace: WorkspaceDep,
    catalog: CatalogDep,
    store: StoreDep,
    registry: RegistryDep,
    actor: ActorDep,
    body: Optional[BuildRequest] = None,
) -> dict:
    targets = body.targets if body else None
    if targets is not None and len(targets) == 0:
        raise HTTPException(
            status_code=400,
            detail="targets must be non-empty when provided; omit it to build everything",
        )
    store.log_audit("build_requested", {"targets": targets or []}, actor=actor)
    builder = Builder(workspace, catalog, store, registry)
    build = builder.build(targets)
    return _dump(build)


@router.get("/builds", dependencies=[VIEWER])
def list_builds(store: StoreDep) -> list[dict]:
    return [_dump(b) for b in store.list_builds()]


@router.get("/builds/{build_id}", dependencies=[VIEWER])
def get_build(build_id: str, store: StoreDep) -> dict:
    build = store.get_build(build_id)
    if build is None:
        raise KeyError(f"Build not found: {build_id!r}")
    return _dump(build)


@router.get("/lineage", dependencies=[VIEWER])
def get_lineage(store: StoreDep) -> dict:
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_nodes: set[tuple[str, str]] = set()
    seen_edges: set[tuple[str, str]] = set()

    def add_node(node_id: str, node_type: str) -> None:
        key = (node_id, node_type)
        if key not in seen_nodes:
            seen_nodes.add(key)
            nodes.append({"id": node_id, "type": node_type})

    def add_edge(src: str, dst: str) -> None:
        if (src, dst) not in seen_edges:
            seen_edges.add((src, dst))
            edges.append({"from": src, "to": dst})

    for edge in store.list_lineage():
        add_node(edge.upstream_dataset, "dataset")
        add_node(edge.transform_name, "transform")
        add_node(edge.downstream_dataset, "dataset")
        add_edge(edge.upstream_dataset, edge.transform_name)
        add_edge(edge.transform_name, edge.downstream_dataset)

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# Ontology
# ---------------------------------------------------------------------------

@router.get("/ontology/object-types", dependencies=[VIEWER])
def list_object_types(service: OntologyDep) -> list[dict]:
    return [_dump(ot) for ot in service.list_object_types()]


@router.get("/ontology/object-types/{name}", dependencies=[VIEWER])
def get_object_type(name: str, service: OntologyDep) -> dict:
    ot = service.ontology.object_type(name)
    if ot is None:
        raise KeyError(f"Unknown object type: {name!r}")
    result = _dump(ot)
    result["links"] = [
        _dump(link)
        for link in service.ontology.link_types
        if name in (link.from_type, link.to_type)
    ]
    result["actions"] = [
        _dump(action)
        for action in service.ontology.actions
        if action.object_type == name
    ]
    return result


@router.get("/ontology/objects/{type_name}", dependencies=[VIEWER])
def query_objects(
    type_name: str,
    request: Request,
    service: OntologyDep,
    search: Optional[str] = None,
    limit: int = Query(100, ge=0, le=10_000),
    offset: int = Query(0, ge=0),
) -> dict:
    filters = {
        key[len("filter."):]: value
        for key, value in request.query_params.items()
        if key.startswith("filter.")
    }
    return service.query(
        type_name, search=search, filters=filters or None, limit=limit, offset=offset
    )


@router.get("/ontology/objects/{type_name}/{pk}", dependencies=[VIEWER])
def get_object(type_name: str, pk: str, service: OntologyDep) -> dict:
    obj = service.get(type_name, pk)
    if obj is None:
        raise KeyError(f"No {type_name!r} object with primary key {pk!r}")
    return obj


@router.get("/ontology/objects/{type_name}/{pk}/links/{link_name}", dependencies=[VIEWER])
def get_linked_objects(
    type_name: str, pk: str, link_name: str, service: OntologyDep
) -> dict:
    return {"objects": service.linked(type_name, pk, link_name)}


@router.get("/ontology/actions", dependencies=[VIEWER])
def list_actions(service: OntologyDep) -> list[dict]:
    return [_dump(a) for a in service.ontology.actions]


@router.post("/ontology/actions/{name}/apply", dependencies=[EDITOR])
def apply_action(
    name: str, body: ActionApplyRequest, service: OntologyDep, actor: ActorDep
) -> dict:
    edit = service.apply_action(name, body.pk, body.parameters, actor=actor)
    return _dump(edit)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@router.get("/audit", dependencies=[VIEWER])
def list_audit(store: StoreDep, limit: int = Query(100, ge=0, le=10_000)) -> list[dict]:
    return [_dump(e) for e in store.list_audit(limit)]
