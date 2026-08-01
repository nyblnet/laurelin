"""API routers and per-request dependencies.

Catalog and store live on ``app.state`` (built once in ``create_app``); the
transform registry and ontology are rebuilt per request so edits to
``pipelines/`` and ``ontology/`` show up without a server restart.
"""

from __future__ import annotations

import os
import re
import tempfile
from contextlib import contextmanager
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
from laurelin.api.context import active_catalog, active_store, active_workspace
from laurelin.catalog import DatasetCatalog
from laurelin.catalog.catalog import suggest_dataset_name
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.limits import QueryRejected, QueryTimeout, QueryTooLarge
from laurelin.core.models import (
    ColumnMask,
    DashboardInfo,
    DashboardPanel,
    DatasetVersionInfo,
    Grant,
    RowPolicy,
    User,
    utcnow_iso,
)
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
    return active_workspace(request)


def get_store(request: Request) -> MetadataStore:
    return active_store(request)


def get_catalog(request: Request) -> DatasetCatalog:
    return active_catalog(request)


def get_registry(workspace: Annotated[Workspace, Depends(get_workspace)]) -> TransformRegistry:
    return collect_transforms(workspace.pipelines_dir)


def get_ontology_service(
    workspace: Annotated[Workspace, Depends(get_workspace)],
    catalog: Annotated[DatasetCatalog, Depends(get_catalog)],
    store: Annotated[MetadataStore, Depends(get_store)],
    perms: Annotated[PermissionService, Depends(get_permissions)],
    user: Annotated[User, Depends(require_user)],
) -> OntologyService:
    ontology = load_ontology(workspace.ontology_dir)
    # Bind the row-level-security / masking transform to this user so objects
    # (which are dataset rows) honor the backing dataset's policy.
    return OntologyService(
        workspace, catalog, store, ontology,
        policy=perms.query_policy_fn(user),
        # Lets object queries tell whether *this* backing dataset actually
        # needs per-user filtering; when it doesn't, they run in DuckDB.
        policy_for=perms.per_dataset_policy_fn(user),
        # …and lets a policy that *does* apply be pushed into the scan, so
        # row-level security doesn't force full materialization.
        plan_for=perms.arrow_policy_fn(user),
    )


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
    # wait=true blocks until the build finishes (small builds, tests, scripts);
    # the default returns the pending build immediately and a worker runs it.
    wait: bool = False


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
    policy = perms.row_policy_fn(user, name)
    if policy is None:
        rows = catalog.rows(name, limit=limit, offset=offset, version=version)
        return {"rows": rows, "row_count": info.row_count}
    # Row-level security / masking: filter the full table, then page in-memory so
    # row_count reflects only the rows this user may see.
    table = policy(catalog.read(name, version))
    rows = catalog.table_to_rows(table.slice(offset, limit))
    return {"rows": rows, "row_count": table.num_rows}


@router.post("/query")
def run_query(body: QueryRequest, catalog: CatalogDep, store: StoreDep, perms: PermDep, user: UserDep) -> dict:
    """Run a read-only SQL query over the datasets the user can view (each a view
    named after the dataset). Datasets the user cannot view are not registered,
    so referencing one fails as an unknown table; row-level security and column
    masking are applied to every registered dataset. Syntax/binder errors -> 400."""
    allowed = perms.viewable_datasets(user, [d.name for d in store.list_datasets()])
    try:
        return catalog.query(
            body.sql, max_rows=body.max_rows, allowed=allowed,
            plan_for=perms.arrow_policy_fn(user),
            sql_policy_for=perms.sql_policy_fn(user),
        )
    except (QueryTimeout, QueryTooLarge, QueryRejected):
        # Resource limits carry their own status codes; don't flatten them into
        # a generic 400 alongside syntax errors.
        raise
    except Exception as exc:  # duckdb parser/binder/runtime errors
        raise HTTPException(status_code=400, detail=str(exc).strip())


# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------
#
# A dashboard is saved SQL + presentation. Panels are executed by the client
# through POST /query, so every viewer sees their own ACL/RLS/markings-filtered
# result — storing a dashboard grants nobody any new read access.

_DASHBOARD_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class DashboardUpsertRequest(BaseModel):
    title: str = ""
    description: str = ""
    panels: list[DashboardPanel] = Field(default_factory=list)


@router.get("/dashboards", dependencies=[VIEWER])
def list_dashboards(store: StoreDep) -> list[dict]:
    return [_dump(d) for d in store.list_dashboards()]


@router.get("/dashboards/{name}", dependencies=[VIEWER])
def get_dashboard(name: str, store: StoreDep) -> dict:
    dash = store.get_dashboard(name)
    if dash is None:
        raise HTTPException(status_code=404, detail=f"Dashboard not found: {name!r}")
    return _dump(dash)


@router.put("/dashboards/{name}", dependencies=[EDITOR])
def upsert_dashboard(
    name: str, body: DashboardUpsertRequest, store: StoreDep, actor: ActorDep
) -> dict:
    if not _DASHBOARD_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid dashboard name {name!r}: must match ^[a-z][a-z0-9_-]{{0,63}}$",
        )
    if len(body.panels) > 50:
        raise HTTPException(status_code=400, detail="A dashboard is limited to 50 panels")
    existing = store.get_dashboard(name)
    info = DashboardInfo(
        name=name,
        title=body.title.strip(),
        description=body.description.strip(),
        panels=body.panels,
        created_at=existing.created_at if existing else utcnow_iso(),
        created_by=existing.created_by if existing else actor,
        updated_at=utcnow_iso(),
    )
    store.upsert_dashboard(info)
    store.log_audit(
        "dashboard_updated" if existing else "dashboard_created",
        {"dashboard": name, "panels": len(body.panels)},
        actor=actor,
    )
    return _dump(info)


@router.delete("/dashboards/{name}", dependencies=[EDITOR])
def delete_dashboard(name: str, store: StoreDep, actor: ActorDep) -> dict:
    if not store.delete_dashboard(name):
        raise HTTPException(status_code=404, detail=f"Dashboard not found: {name!r}")
    store.log_audit("dashboard_deleted", {"dashboard": name}, actor=actor)
    return {"deleted": name}


class FederatedDatasetRequest(BaseModel):
    source: dict[str, Any]
    description: str = ""


@router.put("/datasets/{name}/federated", dependencies=[ADMIN])
def register_federated_dataset(
    name: str,
    body: FederatedDatasetRequest,
    catalog: CatalogDep,
    store: StoreDep,
    actor: ActorDep,
) -> dict:
    """Register a table Laurelin governs but does not hold.

    Admin-only: a federated source carries credentials and points the server at
    a remote system. The source is validated and probed before it is stored, so
    an unreachable table fails here rather than at first query.
    """
    from laurelin.core.federation import FederationError, redacted_source

    existing = store.get_dataset(name)
    if existing is not None and not existing.is_federated:
        raise HTTPException(
            status_code=409,
            detail=f"Dataset {name!r} already exists as a managed dataset",
        )
    try:
        info = catalog.register_federated(name, body.source, body.description)
    except FederationError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    store.log_audit(
        "federated_dataset_registered",
        {"dataset": name, "type": body.source.get("type")},
        actor=actor,
    )
    out = _dump(info)
    out["source"] = redacted_source(info.source)
    return out


@router.post("/datasets/{name}/compact", dependencies=[EDITOR])
def compact_dataset(
    name: str,
    catalog: CatalogDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
) -> dict:
    """Merge an appended dataset's parts into a single file. Appends are cheap
    but accumulate parts; compaction trades one rewrite for a tidy layout."""
    _require_dataset_edit(perms, user, name)
    info = catalog.compact(name)
    store.log_audit(
        "dataset_compact_requested", {"dataset": name, "version": info.version}, actor=actor
    )
    return _dump(info)


# Rows shown in an import preview. Enough to see the shape of the data and
# spot a mis-inferred column; small enough that previewing a 5 GB file is
# instant.
_PREVIEW_ROWS = 50


@contextmanager
def _spooled_upload(file: UploadFile):
    """Stream an upload to a temp file, enforcing the size cap as it goes.

    Checked while writing rather than from Content-Length, which a client
    controls: the cap has to bound what actually lands on disk.
    """
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
        yield tmp_path
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post("/datasets/preview", dependencies=[EDITOR])
def preview_upload(catalog: CatalogDep, file: UploadFile = File(...)) -> dict:
    """Infer a file's schema and sample its rows without creating anything.

    Importing a file is a decision about types and column names, and making
    that decision blind — upload, then discover DuckDB read every column as
    VARCHAR — is how a dataset ends up wrong on version 1. Nothing here
    touches storage or the metadata store.
    """
    with _spooled_upload(file) as tmp_path:
        try:
            sample = catalog.parse_upload(tmp_path, limit=_PREVIEW_ROWS)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {
            "suggested_name": suggest_dataset_name(file.filename or "dataset"),
            "columns": [
                {"name": f.name, "type": str(f.type)} for f in sample.schema
            ],
            "rows": catalog.table_to_rows(sample),
            "sampled_rows": sample.num_rows,
            # A preview reads only the first rows, so it cannot state the
            # file's total without reading all of it. Saying "at least N"
            # beats implying a count we didn't take.
            "truncated": sample.num_rows >= _PREVIEW_ROWS,
        }


@router.post("/datasets/{name}/upload")
def upload_dataset_file(
    name: str,
    catalog: CatalogDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
    file: UploadFile = File(...),
    mode: str = Query("replace", pattern="^(replace|append)$"),
) -> dict:
    # Per-dataset edit. For a dataset with no grants this reduces to the old
    # editor-role requirement; a grant can elevate a viewer for one dataset.
    _require_dataset_edit(perms, user, name)
    with _spooled_upload(file) as tmp_path:
        info = catalog.upload_file(name, tmp_path, mode=mode)
    store.log_audit(
        "dataset_uploaded",
        {
            "dataset": name,
            "filename": file.filename,
            "mode": mode,
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
    request: Request,
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
    if body and body.wait:
        return _dump(builder.build(targets))
    # Async (default): validate the plan now so a bad target is still a 400,
    # create the pending record, and hand execution to the build pool.
    builder.plan(targets)  # ValueError -> 400 via handler
    store.reap_expired_builds()  # recover work stranded by a dead replica
    build = store.create_build(list(targets) if targets else [])
    request.app.state.build_executor.submit(
        builder.execute, build.id, targets, request.app.state.worker_id
    )
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
    # Index status, so the UI can show "indexed / stale / not indexed" on load
    # rather than only after a build. Freshness is the interesting bit: a stale
    # index is bypassed, so telling the user is the difference between "why is
    # this slow" and "oh, it needs a rebuild".
    state = service.index_state(ot)
    result["index"] = {
        "indexed": state is not None,
        "fresh": service.index_is_fresh(ot) if state is not None else False,
        "objects": state["object_count"] if state else 0,
    }
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


class MetricRequest(BaseModel):
    op: str = Field(description="count | count_distinct | sum | avg | min | max | median")
    property: Optional[str] = Field(default=None, description="Omit only for count")
    alias: Optional[str] = None


class AggregateRequest(BaseModel):
    group_by: list[str] = Field(default_factory=list)
    metrics: list[MetricRequest] = Field(default_factory=list)
    filters: dict[str, str] = Field(default_factory=dict)
    search: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=1000)


@router.post("/ontology/objects/{type_name}/aggregate")
def aggregate_objects(
    type_name: str,
    body: AggregateRequest,
    service: OntologyDep,
    perms: PermDep,
    user: UserDep,
) -> dict:
    """Group objects and compute metrics over them.

    POST rather than GET because the request is a structured document — a list
    of metrics — and encoding that in query parameters produces something
    nobody can read or validate. It is still a read: no state changes, and the
    same view permission applies.
    """
    _require_ot_view(perms, user, service, type_name)
    try:
        return service.aggregate(
            type_name,
            group_by=body.group_by,
            metrics=[m.model_dump() for m in body.metrics],
            filters=body.filters or None,
            search=body.search,
            limit=body.limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


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


@router.post("/ontology/object-types/{name}/index", dependencies=[EDITOR])
def build_object_index(
    name: str, service: OntologyDep, store: StoreDep, actor: ActorDep
) -> dict:
    """Materialize an object type into the index.

    Opt-in per type: indexing trades storage and build time for query speed,
    which is worth it for entities and wasteful for high-volume events. Once
    built, the index is refreshed automatically after each build and bypassed
    whenever it is stale.
    """
    count = service.reindex(name)
    store.log_audit("object_index_built", {"object_type": name, "objects": count},
                    actor=actor)
    return {"object_type": name, "objects": count,
            "state": store.object_index_state(name)}


@router.delete("/ontology/object-types/{name}/index", dependencies=[EDITOR])
def drop_object_index(name: str, store: StoreDep, actor: ActorDep) -> dict:
    store.drop_object_index(name)
    store.log_audit("object_index_dropped", {"object_type": name}, actor=actor)
    return {"dropped": name}


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
# Dataset policies (admin) — row-level security + column masking
# ---------------------------------------------------------------------------

class DatasetPolicyRequest(BaseModel):
    row_policy: Optional[RowPolicy] = None
    column_masks: list[ColumnMask] = Field(default_factory=list)


@router.get("/dataset-policies", dependencies=[ADMIN])
def list_dataset_policies(store: StoreDep) -> list[dict]:
    """Row-security / masking policy for every dataset (absent = no policy)."""
    policies = store.list_dataset_policies()
    return [
        {"dataset": d.name, "policy": policies.get(d.name)}
        for d in store.list_datasets()
    ]


@router.put("/datasets/{name}/policy", dependencies=[ADMIN])
def set_dataset_policy(
    name: str, body: DatasetPolicyRequest, store: StoreDep, actor: ActorDep
) -> dict:
    if store.get_dataset(name) is None:
        raise KeyError(f"Dataset not found: {name!r}")
    empty = body.row_policy is None and not body.column_masks
    policy = None if empty else {
        "row_policy": body.row_policy.model_dump(mode="json") if body.row_policy else None,
        "column_masks": [m.model_dump(mode="json") for m in body.column_masks],
    }
    store.set_dataset_policy(name, policy)
    store.log_audit(
        "dataset_policy_set",
        {
            "dataset": name,
            "row_policy": policy is not None and policy["row_policy"] is not None,
            "masked_columns": [m.column for m in body.column_masks],
        },
        actor=actor,
    )
    return {"dataset": name, "policy": policy}


# ---------------------------------------------------------------------------
# Classification markings (admin) — mandatory access control + lineage propagation
# ---------------------------------------------------------------------------

class MarkingCreateRequest(BaseModel):
    name: str
    description: str = ""


class DatasetMarkingsRequest(BaseModel):
    markings: list[str] = Field(default_factory=list)


class ClearancesRequest(BaseModel):
    markings: list[str] = Field(default_factory=list)


_MARKING_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")


@router.get("/markings", dependencies=[VIEWER])
def list_markings(store: StoreDep) -> list[dict]:
    return store.list_markings()


@router.post("/markings", dependencies=[ADMIN])
def create_marking(body: MarkingCreateRequest, store: StoreDep, actor: ActorDep) -> dict:
    name = body.name.strip().lower()
    if not _MARKING_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid marking name (a-z 0-9 _ . -, 1-48)")
    if store.marking_exists(name):
        raise HTTPException(status_code=409, detail=f"Marking already exists: {name!r}")
    store.create_marking(name, body.description)
    store.log_audit("marking_created", {"name": name}, actor=actor)
    return {"name": name, "description": body.description}


@router.delete("/markings/{name}", dependencies=[ADMIN])
def delete_marking(name: str, store: StoreDep, actor: ActorDep) -> dict:
    if not store.marking_exists(name):
        raise KeyError(f"Marking not found: {name!r}")
    store.delete_marking(name)
    store.recompute_all_markings()  # its removal ripples through effective sets
    store.log_audit("marking_deleted", {"name": name}, actor=actor)
    return {"ok": True}


@router.get("/dataset-markings", dependencies=[ADMIN])
def list_dataset_markings(store: StoreDep) -> list[dict]:
    """Explicit + effective (propagated) markings for every dataset."""
    return [
        {
            "dataset": d.name,
            "explicit": store.get_explicit_markings(d.name),
            "effective": store.get_effective_markings(d.name),
        }
        for d in store.list_datasets()
    ]


@router.put("/datasets/{name}/markings", dependencies=[ADMIN])
def set_dataset_markings(
    name: str, body: DatasetMarkingsRequest, store: StoreDep, actor: ActorDep
) -> dict:
    if store.get_dataset(name) is None:
        raise KeyError(f"Dataset not found: {name!r}")
    for m in body.markings:
        if not store.marking_exists(m):
            raise HTTPException(status_code=400, detail=f"Unknown marking: {m!r}")
    store.set_explicit_markings(name, body.markings)
    store.recompute_all_markings()  # propagate downstream through lineage
    store.log_audit(
        "dataset_markings_set", {"dataset": name, "markings": body.markings}, actor=actor
    )
    return {
        "dataset": name,
        "explicit": store.get_explicit_markings(name),
        "effective": store.get_effective_markings(name),
    }


@router.get("/users/{username}/clearances", dependencies=[ADMIN])
def get_clearances(username: str, store: StoreDep) -> dict:
    return {"username": username, "markings": store.get_clearances(username)}


@router.put("/users/{username}/clearances", dependencies=[ADMIN])
def set_clearances(
    username: str, body: ClearancesRequest, store: StoreDep, actor: ActorDep
) -> dict:
    for m in body.markings:
        if not store.marking_exists(m):
            raise HTTPException(status_code=400, detail=f"Unknown marking: {m!r}")
    store.set_clearances(username, body.markings)
    store.log_audit(
        "clearances_set", {"username": username, "markings": body.markings}, actor=actor
    )
    return {"username": username, "markings": store.get_clearances(username)}


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
