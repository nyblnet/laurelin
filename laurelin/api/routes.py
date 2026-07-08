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

from laurelin.api.auth_routes import (
    require_admin,
    require_editor,
    require_user,
    require_viewer,
)
from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import DatasetVersionInfo, Grant, User
from laurelin.core.permissions import PermissionService
from laurelin.ontology import OntologyService, load_ontology
from laurelin.transforms import (
    Builder,
    PipelineFiles,
    TransformRegistry,
    collect_transforms,
)

router = APIRouter()

# RBAC guards (see docs/ARCHITECTURE.md): reads need viewer, mutations editor.
VIEWER = Depends(require_viewer)
EDITOR = Depends(require_editor)
ADMIN = Depends(require_admin)


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


def get_permissions(store: Annotated[MetadataStore, Depends(get_store)]) -> PermissionService:
    return PermissionService(store)


def get_pipeline_files(
    workspace: Annotated[Workspace, Depends(get_workspace)],
) -> PipelineFiles:
    return PipelineFiles(workspace.pipelines_dir)


def require_pipelines_unlocked(request: Request) -> None:
    if request.app.state.lock_pipelines:
        raise HTTPException(
            status_code=403,
            detail="Pipeline authoring is disabled on this server (--lock-pipelines)",
        )


WorkspaceDep = Annotated[Workspace, Depends(get_workspace)]
StoreDep = Annotated[MetadataStore, Depends(get_store)]
CatalogDep = Annotated[DatasetCatalog, Depends(get_catalog)]
RegistryDep = Annotated[TransformRegistry, Depends(get_registry)]
OntologyDep = Annotated[OntologyService, Depends(get_ontology_service)]
ActorDep = Annotated[str, Depends(get_actor)]
PermDep = Annotated[PermissionService, Depends(get_permissions)]
PipelineFilesDep = Annotated[PipelineFiles, Depends(get_pipeline_files)]
# The authenticated User object (not just a role gate), for per-type checks.
UserDep = Annotated[User, Depends(require_user)]


def _require_dataset_view(perms: PermissionService, user: User, name: str) -> None:
    if not perms.can_view_dataset(user, name):
        raise HTTPException(
            status_code=403, detail=f"You do not have access to dataset {name!r}"
        )


def _require_dataset_edit(perms: PermissionService, user: User, name: str) -> None:
    if not perms.can_edit_dataset(user, name):
        raise HTTPException(
            status_code=403, detail=f"You do not have edit access to dataset {name!r}"
        )


def _ot_permission(
    perms: PermissionService, user: User, service: "OntologyService", type_name: str
) -> tuple[bool, bool]:
    """Effective (view, edit) for an object type = ontology grant composed with
    the backing dataset's access."""
    ot = service.ontology.object_type(type_name)
    backing = ot.backing_dataset if ot is not None else ""
    return perms.object_type_permission(user, type_name, backing)


def _require_ot_view(perms, user, service, type_name: str) -> None:
    if not _ot_permission(perms, user, service, type_name)[0]:
        raise HTTPException(
            status_code=403,
            detail=f"You do not have access to object type {type_name!r}",
        )


def _require_ot_edit(perms, user, service, type_name: str) -> None:
    if not _ot_permission(perms, user, service, type_name)[1]:
        raise HTTPException(
            status_code=403,
            detail=f"You do not have edit access to object type {type_name!r}",
        )


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

@router.get("/datasets")
def list_datasets(store: StoreDep, perms: PermDep, user: UserDep) -> list[dict]:
    # Only datasets the user can view; each carries the user's effective access.
    out = []
    for d in store.list_datasets():
        can_view, can_edit = perms.dataset_permission(user, d.name)
        if not can_view:
            continue
        dd = _dump(d)
        dd["permissions"] = {"can_view": can_view, "can_edit": can_edit}
        out.append(dd)
    return out


@router.post("/datasets", dependencies=[EDITOR])
def create_dataset(
    body: DatasetCreateRequest, catalog: CatalogDep, store: StoreDep, actor: ActorDep
) -> dict:
    info = catalog.create_dataset(body.name, body.description)
    store.log_audit("dataset_created", {"dataset": info.name}, actor=actor)
    return _dump(info)


@router.get("/datasets/{name}")
def get_dataset(name: str, store: StoreDep, perms: PermDep, user: UserDep) -> dict:
    info = store.get_dataset(name)
    if info is None:
        raise KeyError(f"Dataset not found: {name!r}")
    _require_dataset_view(perms, user, name)
    can_view, can_edit = perms.dataset_permission(user, name)
    result = _dump(info)
    result["permissions"] = {"can_view": can_view, "can_edit": can_edit}
    result["versions"] = [_dump(v) for v in store.list_versions(name)]
    return result


@router.get("/datasets/{name}/schema")
def get_dataset_schema(
    name: str, store: StoreDep, perms: PermDep, user: UserDep, version: Optional[int] = None
) -> list[dict]:
    _require_dataset_view(perms, user, name)
    info = _version_info(store, name, version)
    return [c.model_dump() for c in info.schema_]


@router.get("/datasets/{name}/rows")
def get_dataset_rows(
    name: str,
    catalog: CatalogDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
    limit: int = Query(100, ge=0, le=10_000),
    offset: int = Query(0, ge=0),
    version: Optional[int] = None,
) -> dict:
    _require_dataset_view(perms, user, name)
    info = _version_info(store, name, version)
    rows = catalog.rows(name, limit=limit, offset=offset, version=version)
    return {"rows": rows, "row_count": info.row_count}


@router.post("/query")
def run_query(body: QueryRequest, catalog: CatalogDep, store: StoreDep, perms: PermDep, user: UserDep) -> dict:
    """Run a read-only SQL query over the datasets the user can view (each a view
    named after the dataset). Datasets the user cannot view are not registered,
    so referencing one fails as an unknown table. Syntax/binder errors -> 400."""
    allowed = perms.viewable_datasets(user, [d.name for d in store.list_datasets()])
    try:
        return catalog.query(body.sql, max_rows=body.max_rows, allowed=allowed)
    except Exception as exc:  # duckdb parser/binder/runtime errors
        raise HTTPException(status_code=400, detail=str(exc).strip())


@router.post("/datasets/{name}/upload")
def upload_dataset_file(
    name: str,
    catalog: CatalogDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
    file: UploadFile = File(...),
) -> dict:
    # Per-dataset edit. For a dataset with no grants this reduces to the old
    # editor-role requirement; a grant can elevate a viewer for one dataset.
    _require_dataset_edit(perms, user, name)
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

@router.get("/ontology/object-types")
def list_object_types(service: OntologyDep, perms: PermDep, user: UserDep) -> list[dict]:
    # Only types the user may view; each carries the user's effective permission.
    out = []
    for ot in service.list_object_types():
        can_view, can_edit = perms.object_type_permission(
            user, ot.api_name, ot.backing_dataset
        )
        if not can_view:
            continue
        d = _dump(ot)
        d["permissions"] = {"can_view": can_view, "can_edit": can_edit}
        out.append(d)
    return out


@router.get("/ontology/object-types/{name}")
def get_object_type(name: str, service: OntologyDep, perms: PermDep, user: UserDep) -> dict:
    ot = service.ontology.object_type(name)
    if ot is None:
        raise KeyError(f"Unknown object type: {name!r}")
    _require_ot_view(perms, user, service, name)
    can_view, can_edit = perms.object_type_permission(user, name, ot.backing_dataset)
    result = _dump(ot)
    result["permissions"] = {"can_view": can_view, "can_edit": can_edit}
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


@router.get("/ontology/objects/{type_name}")
def query_objects(
    type_name: str,
    request: Request,
    service: OntologyDep,
    perms: PermDep,
    user: UserDep,
    search: Optional[str] = None,
    limit: int = Query(100, ge=0, le=10_000),
    offset: int = Query(0, ge=0),
) -> dict:
    _require_ot_view(perms, user, service, type_name)
    filters = {
        key[len("filter."):]: value
        for key, value in request.query_params.items()
        if key.startswith("filter.")
    }
    return service.query(
        type_name, search=search, filters=filters or None, limit=limit, offset=offset
    )


@router.get("/ontology/objects/{type_name}/{pk}")
def get_object(
    type_name: str, pk: str, service: OntologyDep, perms: PermDep, user: UserDep
) -> dict:
    _require_ot_view(perms, user, service, type_name)
    obj = service.get(type_name, pk)
    if obj is None:
        raise KeyError(f"No {type_name!r} object with primary key {pk!r}")
    return obj


@router.get("/ontology/objects/{type_name}/{pk}/links/{link_name}")
def get_linked_objects(
    type_name: str,
    pk: str,
    link_name: str,
    service: OntologyDep,
    perms: PermDep,
    user: UserDep,
) -> dict:
    _require_ot_view(perms, user, service, type_name)
    # Only return links to objects on types the user may also view.
    link = service.ontology.link_type(link_name)
    if link is not None:
        other = link.to_type if link.from_type == type_name else link.from_type
        if not _ot_permission(perms, user, service, other)[0]:
            return {"objects": []}
    return {"objects": service.linked(type_name, pk, link_name)}


@router.get("/ontology/actions")
def list_actions(service: OntologyDep, perms: PermDep, user: UserDep) -> list[dict]:
    return [
        _dump(a)
        for a in service.ontology.actions
        if _ot_permission(perms, user, service, a.object_type)[0]
    ]


@router.post("/ontology/actions/{name}/apply")
def apply_action(
    name: str,
    body: ActionApplyRequest,
    service: OntologyDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
) -> dict:
    action = service.ontology.action(name)
    if action is None:
        raise KeyError(f"Unknown action: {name!r}")
    _require_ot_edit(perms, user, service, action.object_type)
    edit = service.apply_action(name, body.pk, body.parameters, actor=actor)
    return _dump(edit)


# ---------------------------------------------------------------------------
# Ontology permissions (admin) — per-object-type access grants
# ---------------------------------------------------------------------------

class GrantsRequest(BaseModel):
    grants: list[Grant] = Field(default_factory=list)


@router.get("/ontology/permissions", dependencies=[ADMIN])
def list_permissions(service: OntologyDep, store: StoreDep) -> list[dict]:
    """Grants for every object type (empty list = default open per global RBAC)."""
    by_type: dict[str, list[dict]] = {}
    for g in store.list_grants():
        by_type.setdefault(g["object_type"], []).append(
            {k: v for k, v in g.items() if k != "object_type"}
        )
    return [
        {"object_type": ot.api_name, "grants": by_type.get(ot.api_name, [])}
        for ot in service.list_object_types()
    ]


@router.put("/ontology/permissions/{type_name}", dependencies=[ADMIN])
def set_permissions(
    type_name: str,
    body: GrantsRequest,
    service: OntologyDep,
    store: StoreDep,
    perms: PermDep,
    actor: ActorDep,
) -> dict:
    if service.ontology.object_type(type_name) is None:
        raise KeyError(f"Unknown object type: {type_name!r}")
    perms.validate_grants(body.grants)
    store.set_grants_for_type(
        type_name, [g.model_dump(mode="json") for g in body.grants]
    )
    store.log_audit(
        "ontology_permissions_set",
        {"object_type": type_name, "grant_count": len(body.grants)},
        actor=actor,
    )
    return {"object_type": type_name, "grants": [g.model_dump(mode="json") for g in body.grants]}


# ---------------------------------------------------------------------------
# Dataset permissions (admin) — per-dataset access grants
# ---------------------------------------------------------------------------

@router.get("/dataset-permissions", dependencies=[ADMIN])
def list_dataset_permissions(store: StoreDep) -> list[dict]:
    """Grants for every dataset (empty list = default open per global RBAC)."""
    by_ds: dict[str, list[dict]] = {}
    for g in store.list_dataset_grants():
        by_ds.setdefault(g["dataset"], []).append(
            {k: v for k, v in g.items() if k != "dataset"}
        )
    return [
        {"dataset": d.name, "grants": by_ds.get(d.name, [])}
        for d in store.list_datasets()
    ]


@router.put("/datasets/{name}/permissions", dependencies=[ADMIN])
def set_dataset_permissions(
    name: str,
    body: GrantsRequest,
    store: StoreDep,
    perms: PermDep,
    actor: ActorDep,
) -> dict:
    if store.get_dataset(name) is None:
        raise KeyError(f"Dataset not found: {name!r}")
    perms.validate_grants(body.grants)
    store.set_grants_for_dataset(name, [g.model_dump(mode="json") for g in body.grants])
    store.log_audit(
        "dataset_permissions_set",
        {"dataset": name, "grant_count": len(body.grants)},
        actor=actor,
    )
    return {"dataset": name, "grants": [g.model_dump(mode="json") for g in body.grants]}


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@router.get("/audit", dependencies=[VIEWER])
def list_audit(store: StoreDep, limit: int = Query(100, ge=0, le=10_000)) -> list[dict]:
    return [_dump(e) for e in store.list_audit(limit)]


# ---------------------------------------------------------------------------
# Pipeline authoring (transform files)
#
# SECURITY: writing a pipeline file is code-execution-equivalent — the file is
# exec'd on every build/collection. Reads are viewer; writes/deletes are editor
# and can be disabled server-wide with --lock-pipelines. See docs/ARCHITECTURE.
# ---------------------------------------------------------------------------

class PipelineWriteRequest(BaseModel):
    content: str


class QueryTransformRequest(BaseModel):
    sql: str
    output: str
    name: Optional[str] = None


@router.get("/pipelines", dependencies=[VIEWER])
def list_pipelines(files: PipelineFilesDep) -> list[dict]:
    return files.list()


@router.get("/pipelines/{name}", dependencies=[VIEWER])
def read_pipeline(name: str, files: PipelineFilesDep) -> dict:
    return files.read(name)


@router.put("/pipelines/{name}", dependencies=[EDITOR, Depends(require_pipelines_unlocked)])
def write_pipeline(
    name: str, body: PipelineWriteRequest, files: PipelineFilesDep, store: StoreDep, actor: ActorDep
) -> dict:
    result = files.write(name, body.content)
    store.log_audit("pipeline_written", {"name": result["name"]}, actor=actor)
    return result


@router.delete("/pipelines/{name}", dependencies=[EDITOR, Depends(require_pipelines_unlocked)])
def delete_pipeline(name: str, files: PipelineFilesDep, store: StoreDep, actor: ActorDep) -> dict:
    files.delete(name)
    store.log_audit("pipeline_deleted", {"name": name}, actor=actor)
    return {"ok": True}


@router.post("/pipelines/from-query", dependencies=[EDITOR, Depends(require_pipelines_unlocked)])
def pipeline_from_query(
    body: QueryTransformRequest,
    files: PipelineFilesDep,
    store: StoreDep,
    actor: ActorDep,
) -> dict:
    """Create a pipeline file wrapping a workbench query as a SQL transform."""
    dataset_names = [d.name for d in store.list_datasets()]
    result = files.generate_sql_transform(
        body.sql, body.output, dataset_names, name=body.name
    )
    store.log_audit(
        "pipeline_written", {"name": result["name"], "source": "query"}, actor=actor
    )
    return result
