"""API routers and per-request dependencies.

Catalog and store live on ``app.state`` (built once in ``create_app``); the
transform registry and ontology are rebuilt per request so edits to
``pipelines/`` and ``ontology/`` show up without a server restart.
"""

from __future__ import annotations

import logging
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
from pydantic import BaseModel, Field, ValidationError

from laurelin.api.auth_routes import (
    require_admin,
    require_editor,
    require_user,
    require_viewer,
)
from laurelin.api.context import active_catalog, active_store, active_workspace
from laurelin.catalog import DatasetCatalog
from laurelin.catalog.catalog import suggest_dataset_name
from laurelin.core import audience, authoring_hints, fileperms, serialize
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.failure import (
    Failure,
    FailureCode,
    Phase,
    driver_of,
    first_party_message,
    is_first_party,
    safe_detail,
)
from laurelin.core.federation import FederationError
from laurelin.core.limits import QueryRejected, QueryTimeout, QueryTooLarge
from laurelin.core.models import (
    ColumnMask,
    DashboardInfo,
    DashboardPanel,
    DatasetVersionInfo,
    Grant,
    Role,
    RowPolicy,
    User,
    utcnow_iso,
)
from laurelin.core.permissions import PermissionService
from laurelin.ontology import OntologyService, load_ontology
from laurelin.transforms import (
    Builder,
    PipelineError,
    PipelineFiles,
    TransformRegistry,
    collect_transforms,
)
from laurelin.transforms.flow_compile import kind_of
from laurelin.transforms.flow_files import FlowFiles, compile_flow_now, eject_sql
from laurelin.transforms.flow_governance import (
    check_flow_governance,
    check_flow_sources,
    masked_columns_for,
    output_will_be_restricted,
    restrict_output_to_author,
)
from laurelin.transforms.flow_ir import FlowDef, FlowRefused

log = logging.getLogger("laurelin.api")

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
    # collect_transforms EXECs every .py in pipelines/, so a workspace whose
    # pipelines arrived in an import stays empty until an admin acknowledges
    # them. Empty rather than a 409: listing transforms, lineage and the build
    # history are how an operator reviews what just landed, and 409-ing all of
    # them would hide the workspace behind the very control meant to protect it.
    # POST /builds refuses out loud instead — see run_build.
    from laurelin.export import pipelines_acknowledged

    if not pipelines_acknowledged(workspace):
        return TransformRegistry()
    try:
        return collect_transforms(workspace.pipelines_dir)
    except PipelineError as exc:
        # One pipeline file that will not import used to take out every route
        # that depends on the registry — `GET /transforms` (VIEWER) and
        # `POST /builds` among them — as an unhandled 500, because
        # `PipelineError` has no handler. Measured by putting a raising file in
        # `pipelines/`: a viewer's Pipeline page rendered "Error 500: Internal
        # Server Error" and nothing else.
        #
        # 409 and not 500: the request is fine, the state of the workspace is
        # not, and the message says which file and where to fix it. **Only
        # `exc.pipeline` is used** — a Laurelin identifier. `str(exc)` carries
        # the raising library's own sentence and the server's absolute path
        # (see PipelineError), and this route is readable by a viewer.
        log.warning("pipeline collection failed", exc_info=exc)
        raise HTTPException(
            status_code=409,
            detail=(
                f"Pipeline file {exc.pipeline!r} does not import, so the "
                "transform graph could not be built. An editor can open it "
                "under Transforms to see why; the reason is in the server log."
            ),
        ) from None


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
        # The policy as *data*, which is what the edit overlay needs: an
        # overlay row never passes through the policied scan, so the questions
        # about it are "which columns are masked for this user" and "does this
        # assignment leave their allowlist", not "run this table through".
        decide_for=lambda dataset, columns: perms.decide(dataset, columns, user),
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


def _stamp_pipeline_author(
    request: Request, store: MetadataStore, name: str, author: str
) -> None:
    """Record who authored a pipeline file, server-side, so the Builder checks
    every build against them — *unless* the server runs ``--no-auth``.

    Under ``--no-auth`` every request is the implicit admin ``anonymous``,
    which is not a real store user. On a workspace that also has real users
    (an imported one, or an authed workspace restarted with ``--no-auth``)
    stamping ``anonymous`` made every subsequent build refuse: ``_author_user``
    saw users present and a recorded author who is none of them, and failed
    closed. A no-auth server enforces no ACLs, so there is nothing to check a
    build against — the file belongs in the operator-trusted bucket. Clear any
    stale stamp so a re-save there is genuinely unchecked, rather than leaving
    a previous authed session's stamp to refuse.
    """
    if request.app.state.no_auth:
        store.delete_pipeline_author(name)
        return
    store.set_pipeline_author(name, author)


def require_flows_unlocked(request: Request) -> None:
    # A separate flag with a separate sentence, because the two locks close
    # different holes: `--lock-pipelines` closes code execution (a pipeline
    # file is exec'd as the server), `--lock-flows` closes no-code authoring
    # (a flow compiles to bound, schema-checked SQL and cannot reach exec).
    # A hardened server sets the first and usually not the second.
    if request.app.state.lock_flows:
        raise HTTPException(
            status_code=403,
            detail="Flow authoring is disabled on this server (--lock-flows)",
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


# `_dump` moved to laurelin/core/serialize.py, where R2 is enforced, and is
# re-exported here so the six routers that import it need no churn. Left in the
# API layer it would have protected the REST surface and nothing else — the MCP
# client, the CLI and the export writer serialize the same models.
_dump = serialize.dump


def stored_instruction_error(
    exc: BaseException, *, subject: str, author: Role
) -> HTTPException:
    """Running a *stored* instruction failed. What may the runner be told?

    The two routes R2 added — `POST /dashboards/{n}/panels/{id}/run` and
    `GET /apps/{n}/objects` — exist because the instruction has to stay on the
    server. Their failure paths handed part of it straight back, which made each
    of them an oracle for the very fields the read path withholds. Measured, as
    a plain viewer, against panels saved by an editor:

        run gb -> 400 "Unknown group_by property 'postgresql://svc:PANELSECRET@…'"
        run mp -> 400 "Unknown property 'postgresql://svc:PANELSECRET@…'"
        run fk -> 500 (a duckdb BinderException nobody caught)

    and against an app whose ontology had drifted:

        GET /apps/ops/objects -> 400 "Unknown filter property 'status' for …"

    Every one of those messages is *first-party* — Laurelin wrote the sentence —
    so R1 was satisfied and R2 was not. Structure fixes R1's problem; only
    privilege fixes R2's. So the message that names the offending field goes to
    a principal who could have authored it, and everyone else gets a
    `Failure` — which subject, which phase, which code, and a `detail_ref`.

    `author` is the level that wrote the instruction: editor for a dashboard
    panel, admin for an object app.
    """
    first_party = is_first_party(exc)
    failure = Failure.from_exception(
        exc, phase=Phase.execute, subject=subject,
        # A first-party ValueError here always means the same thing: the stored
        # definition no longer matches the ontology. Saying REMOTE_FAILED would
        # send an operator looking for a remote system that is fine.
        code=FailureCode.DEFINITION_STALE if first_party else None,
    )
    if first_party and serialize.effective_role().covers(author):
        # They could have written it, so they may read why it did not run.
        return HTTPException(status_code=400, detail=first_party_message(exc))
    return HTTPException(status_code=400, detail=failure.render_brief())


def _driver_failure(exc: Exception, source: Any, subject: str) -> Failure:
    """A remote system's failure, as a 502 body may carry it.

    Every registration route probes the remote source before accepting it, and
    every driver under those routes quotes the connection string it was handed
    back into its error — DuckDB's postgres extension prefixes its IO Error with
    the entire DSN, and echoes the offending statement in a `LINE 1:` block, so
    a DSN inside `postgres_scan(...)` appears *twice*.

    None of it is kept. The probe is still the point of the route, so a
    :class:`Failure` comes back: which phase, which code, our own host:port, and
    a `detail_ref` that finds the driver's exact words in the server log.
    """
    inner = exc.__cause__ if isinstance(exc, FederationError) and exc.__cause__ else exc
    failure = getattr(exc, "failure", None)
    if failure is not None:
        return failure
    return Failure.from_exception(
        # `driver="duckdb"` was hardcoded here, and this function serves the
        # ClickHouse and StarRocks registration routes too: a chdb `RuntimeError`
        # was filed under duckdb, pointing an operator at a log that has no such
        # line. Read off the raising class's module instead, with duckdb as the
        # fallback because that is what the federated path really uses.
        inner, phase=Phase.connect, subject=subject,
        driver=driver_of(inner, "duckdb"),
        dsn=str((source or {}).get("url", "")), config=source, preflight=True,
    )


# `_redacted_audit_details` lived here and is DELETED. It was a confidentiality
# boundary resting on `redact_text` over an open bag, and a backstop that
# inspects *key names* cannot help a key called `reason` — which is exactly what
# `auth_routes.py:413` put an attacker-supplied SAML parse failure into, with no
# redaction at all, on an unauthenticated route. The bag is now gated by
# privilege instead: `audit_log.min_read_role`, default admin, filtered in
# `MetadataStore.list_audit`. See §6 of the build spec for the argument.


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
def get_workspace_info(workspace: WorkspaceDep, user: UserDep) -> dict:
    """Name and description for everyone; the server-side path for an admin.

    `root` is the server's filesystem layout. A viewer reading it learns where
    the process keeps its data — deployment information they cannot act on and
    were never meant to have. The admin screens that show it (file security,
    export) are admin-gated already.
    """
    out = {"name": workspace.name, "description": workspace.description}
    if user.role.covers(Role.admin):
        out["root"] = str(workspace.root)
    return out


@router.get("/workspace/file-security", dependencies=[ADMIN])
def get_workspace_file_security(
    request: Request, workspace: WorkspaceDep, store: StoreDep
) -> dict:
    """On-disk permissions of the files that hold this workspace's secrets.

    Exists because the repair is deliberately partial: Laurelin strips world
    access from a metadata database it inherits, but leaves group access alone
    (``laurelin/core/fileperms.py`` argues why). That leaves a residual the
    operator has to decide about, and a WARNING in a log nobody tails is not a
    decision anybody makes. So the mode is a number on the admin screen.

    The structured fields carry file *names*, never the store's path: on a
    PostgreSQL store that "path" is a DSN with a password in it, and no field
    here should be one refactor away from printing it. ``note`` does name a
    local file, because an operator cannot run ``chmod`` on a basename — and it
    is only ever produced by the SQLite backend, so it cannot be a DSN. The
    workspace root is not a secret in any case; ``GET /workspace`` returns it
    to every viewer, while this route is admin-only.
    """
    backend = store.backend
    entries: list[dict] = []

    def add_db(db_backend, path, label: str) -> Optional[str]:
        if not db_backend.is_file_backed:
            return None
        db = Path(str(path))
        # -wal and -shm exist only between a write and a checkpoint. They are
        # reported when present because they hold the same rows as the database
        # and the same secrets; they are not an error when absent.
        for suffix in ("", "-wal", "-shm"):
            entry = fileperms.describe(db.with_name(db.name + suffix))
            if entry["exists"]:
                entry["name"] = label + suffix
                entries.append(entry)
        return db_backend.permission_note

    note = add_db(backend, store.path, "metadata.db")
    # control.db too, and it is the more serious of the two: it holds every
    # user, session token and API token for *every* workspace on the server
    # (see `create_server_app`). It was invisible here — the report was built
    # from the per-workspace store alone — so on an upgraded multi-workspace
    # deployment the file left group-readable at 0640 got a WARNING in the log
    # and nothing else, which is exactly the outcome `fileperms.py` argues is
    # not a decision anyone makes. Reported under the workspace's admin route
    # because there is no other admin surface, and a server admin is a
    # workspace admin.
    control = getattr(request.app.state, "control", None)
    if control is not None:
        note = add_db(control.backend, control.path, "control.db") or note
    entries.append(fileperms.describe(workspace.marker_path))
    # The Iceberg catalog, which is credential-bearing by `core/iceberg.py`'s
    # own argument — it pre-creates the file 0600 because "a warehouse URI
    # recorded in it can carry a credential" — and was absent from this report
    # entirely. `ensure_private_file` does repair an inherited one to 0640, and
    # the residual group access it deliberately leaves behind is the number
    # this route exists to put on a screen. On a PostgreSQL store the omission
    # was starker: the report listed one file while a SQLite catalog sat beside
    # it on disk, under a card reading "the metadata store is postgres, so it
    # keeps no file on this host".
    catalog_db = workspace.root / "iceberg-catalog.db"
    for suffix in ("", "-wal", "-shm"):
        entry = fileperms.describe(catalog_db.with_name(catalog_db.name + suffix))
        if entry["exists"]:
            entries.append(entry)
    directory = fileperms.describe(workspace.root, name=".")
    # The note was written once, when the store was opened. The modes above
    # were read just now. An operator who acts on the advice — runs the chmod
    # the note asked for — must not be told off by a stale sentence sitting
    # next to a table that already shows 0600; a warning that outlives its
    # cause is how operators learn to ignore warnings. So the live stat has the
    # last word: no file still exposed, no note.
    if note and not any(e["world_accessible"] or e["group_accessible"] for e in entries):
        note = None
    return {
        "dialect": backend.dialect,
        "file_backed": backend.is_file_backed,
        # A PostgreSQL store keeps nothing on this host. Saying so is the point:
        # a green "0600" for a file that does not exist would be a lie.
        "store_is_remote": not backend.is_file_backed,
        "strict_mode": fileperms.strict_mode(),
        "directory": directory,
        "files": entries,
        "note": note,
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
    return [serialize.dump(c) for c in info.schema_]


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
    ds = store.get_dataset(name)
    if ds is None:
        raise KeyError(f"Dataset not found: {name!r}")
    policy = perms.row_policy_fn(user, name)

    if ds.scans_at_source:
        # A source-scanned dataset has no local parts to page, so the
        # version-based path below cannot serve it. Page at the source instead.
        # row_count is left null: counting would mean a full scan of someone
        # else's system, and faking a number is worse than admitting it's
        # unknown. Iceberg moves onto this branch too — `catalog.rows` has
        # always ignored `version` for source-scanned datasets, so the version
        # path was pairing the *current* rows with an *old* version's count,
        # which is worse than null.
        if policy is None:
            rows = catalog.rows(name, limit=limit, offset=offset)
        else:
            table = policy(catalog.read(name))
            rows = catalog.table_to_rows(table.slice(offset, limit))
        return {"rows": rows, "row_count": None}

    info = _version_info(store, name, version)
    if policy is None:
        rows = catalog.rows(name, limit=limit, offset=offset, version=version)
        return {"rows": rows, "row_count": info.row_count}
    # Row-level security / masking: filter the full table, then page in-memory so
    # row_count reflects only the rows this user may see.
    table = policy(catalog.read(name, version))
    rows = catalog.table_to_rows(table.slice(offset, limit))
    return {"rows": rows, "row_count": table.num_rows}


def _execute_sql(
    catalog: DatasetCatalog,
    store: MetadataStore,
    perms: PermissionService,
    user: User,
    sql: str,
    max_rows: int,
    params: Optional[list] = None,
    server_authored: bool = False,
) -> dict:
    """Run one read-only query **as this user**. The only SQL execution path.

    Extracted from `run_query` so that `POST /dashboards/.../run` cannot drift
    from it: two policy paths is one policy path plus a bug waiting for someone
    to fix only one of them. Datasets the user cannot view are not registered,
    so referencing one fails as an unknown table; row-level security and column
    masking are applied to every dataset that is.

    ``params`` are bound positionally. The flow builder's preview is the only
    caller that passes any: a compiled flow carries every author-supplied value
    as a parameter and none as text, and routing it through *this* function
    rather than a second one is what keeps preview on the same ACL / row-level
    security / column-masking path as the workbench.
    """
    allowed = perms.viewable_datasets(user, [d.name for d in store.list_datasets()])
    try:
        return catalog.query(
            sql, max_rows=max_rows, allowed=allowed,
            plan_for=perms.arrow_policy_fn(user),
            sql_policy_for=perms.sql_policy_fn(user),
            params=params,
            server_authored=server_authored,
        )
    except (QueryTimeout, QueryTooLarge, QueryRejected):
        # Resource limits carry their own status codes and their own
        # Laurelin-authored messages; don't flatten them into a generic 400
        # alongside syntax errors, and don't route them through `Failure` —
        # they are already first-party text.
        raise
    except Exception as exc:  # duckdb parser/binder/runtime errors
        # R1, and this is what stops the 400 body echoing the SQL back:
        # measured on this tree, a viewer's 400 contained `S3KRET` and DuckDB's
        # full `LINE 1:` echo of the statement. A typo in an editor's panel must
        # not hand the withheld query text to the viewer running it.
        failure = Failure.from_exception(
            exc, phase=Phase.execute, subject="query", driver="duckdb",
        )
        # `detail_for`, because a query over a *federated* dataset fails inside
        # a scan whose endpoint was rebuilt from an ADMIN-authored source
        # config, and this route is viewer-reachable.
        http = HTTPException(
            status_code=400, detail=serialize.detail_for(failure, Role.admin)
        )
        # The exception's *class name* — the same fact `Failure.exc_class`
        # already stores, and never its message — so `_flow_execution` can
        # substitute a sentence written for someone who does not write SQL.
        # Attached rather than passed back, so this function keeps exactly one
        # return shape for its other callers.
        http._flow_exc_class = type(exc).__name__  # type: ignore[attr-defined]
        raise http from None


@router.post("/query")
def run_query(body: QueryRequest, catalog: CatalogDep, store: StoreDep, perms: PermDep, user: UserDep) -> dict:
    """Run a read-only SQL query over the datasets the user can view."""
    return _execute_sql(catalog, store, perms, user, body.sql, body.max_rows)


# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------
#
# A dashboard is saved SQL + presentation, and R2 splits it along exactly that
# line: an EDITOR authors the query, a VIEWER reads the picture.
#
# The old invariant — "storing a dashboard grants nobody any new read access" —
# survives, for a NEW REASON, and the distinction matters. It used to be true
# because the *client* executed the panel through POST /query with the viewer's
# own credentials. It is now true because the *server* executes the stored panel
# through POST /dashboards/{name}/panels/{id}/run, **as the caller**, down the
# same `_execute_sql` path with the same ACL / row-level security / column
# masking. Two viewers with different row policies still get different rows from
# the same panel; neither of them ever receives the SQL.

_DASHBOARD_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class DashboardUpsertRequest(BaseModel):
    title: str = ""
    description: str = ""
    # Raw dicts, not `DashboardPanel`, and that is load-bearing rather than
    # lazy. `DashboardPanel` validates "exactly one of sql / object_type", so a
    # panel that arrives with its OPERATIONAL half *missing* — which is exactly
    # what a client round-tripping the viewer projection sends — would be
    # rejected by the model before `_preserve_operational` ever got the chance
    # to fill it back in from the stored record. Merge first, validate after.
    panels: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/dashboards", dependencies=[VIEWER])
def list_dashboards(store: StoreDep) -> list[dict]:
    return [_dump(d) for d in store.list_dashboards()]


@router.get("/dashboards/{name}", dependencies=[VIEWER])
def get_dashboard(name: str, store: StoreDep) -> dict:
    dash = store.get_dashboard(name)
    if dash is None:
        raise HTTPException(status_code=404, detail=f"Dashboard not found: {name!r}")
    return _dump(dash)


class PanelRunRequest(BaseModel):
    max_rows: int = Field(default=1000, ge=1, le=100_000)


def _stored_panel(store: MetadataStore, name: str, panel_id: str):
    dash = store.get_dashboard(name)
    if dash is None:
        raise HTTPException(status_code=404, detail=f"Dashboard not found: {name!r}")
    panel = next((p for p in dash.panels if p.id == panel_id), None)
    if panel is None:
        raise HTTPException(
            status_code=404, detail=f"Panel not found: {panel_id!r}"
        )
    return dash, panel


@router.post("/dashboards/{name}/panels/{panel_id}/run", dependencies=[VIEWER])
def run_dashboard_panel(
    name: str,
    panel_id: str,
    body: PanelRunRequest,
    catalog: CatalogDep,
    store: StoreDep,
    perms: PermDep,
    service: OntologyDep,
    user: UserDep,
) -> dict:
    """Run one stored panel and return its rows. The viewer's half of R2.

    This is what makes withholding `sql` affordable: a viewer needs a
    dashboard's RESULTS, not its query text. The server loads the panel it
    stored and executes it **as the caller** — `_execute_sql` for a SQL panel,
    `service.aggregate` behind `_require_ot_view` for an object panel — so the
    caller's ACL, row-level security and column masking apply exactly as they
    did when the browser was the thing running the query.

    Returns `{columns, rows, row_count, truncated}` for **both** panel kinds,
    and nothing else. That normalization is not cosmetic. `service.aggregate`
    speaks `{groups, group_count, truncated}`, and the browser used to convert
    it — deriving the column order from the panel's own `group_by` and
    `metrics`. Those are exactly the fields R2 stops sending, so a client that
    still had to do the conversion could not: an object panel rendered as an
    empty box for every viewer, which is trading a security bug for a broken
    product. Found by running it, not by reading it.

    It does not echo the statement back on failure either: a syntax error in an
    editor's panel would otherwise hand the viewer the text they were not given
    (`_execute_sql` raises through `Failure.render()`).
    """
    _dash, panel = _stored_panel(store, name, panel_id)
    if panel.is_object_panel:
        _require_ot_view(perms, user, service, panel.object_type)
        try:
            agg = service.aggregate(
                panel.object_type,
                group_by=panel.group_by,
                metrics=panel.metrics,
                filters=panel.filters or None,
                search=panel.search,
                limit=body.max_rows,
            )
        except (QueryTimeout, QueryTooLarge, QueryRejected):
            raise
        except HTTPException:
            raise
        except Exception as exc:
            # `except ValueError` was what stood here, and it was wrong twice
            # over. It let the ontology's own "Unknown group_by property {prop}"
            # hand a viewer the panel field they were never sent, and it did not
            # catch `duckdb.BinderException` at all — which is not a ValueError,
            # so an ordinary dropped column turned a viewer's dashboard into a
            # bare 500. The SQL branch below has always converted everything;
            # this branch now agrees with it.
            raise stored_instruction_error(
                exc, subject=f"panel:{panel_id}", author=Role.editor
            ) from None
        groups = agg.get("groups", [])
        # Same order the client used to build: the grouping keys, then one
        # column per metric under its alias.
        columns = list(panel.group_by) + [
            m.get("alias") or m.get("op") or f"metric_{i}"
            for i, m in enumerate(panel.metrics)
        ]
        return {
            "columns": columns,
            "rows": groups,
            "row_count": len(groups),
            "truncated": bool(agg.get("truncated", False)),
        }
    if panel.is_flow_panel:
        # Source C: a stored Flow IR document, compiled per run against the
        # live schema and executed **as the caller** down the same
        # `_execute_sql` path as a SQL panel — the caller's ACL (an unviewable
        # source fails as an unknown table), row-level security and column
        # masks apply on every run, so two viewers get different rows from the
        # same panel. Stored as IR rather than compiled SQL deliberately: this
        # route's SQL branch carries no parameter list, so stored compiled
        # text would silently drop its bound values.
        try:
            flow = FlowDef.from_json(panel.flow)
            # Sources are view-checked against the CALLER before the compiler
            # can name a column — the same disclosure ordering as
            # `_compiled_flow` — because `resolve_column`'s refusal enumerates
            # a schema, and an editor-role caller may read this panel's
            # refusal text (`stored_instruction_error` below) without being
            # able to read the dataset it describes.
            check_flow_sources(store, perms, user, flow)
            # `top` is the panel's own top-N — part of the question, bound as
            # `LIMIT ?` at the terminal only. When it fits under `max_rows`
            # the limit is exactly `top` and `truncated` stays false;
            # otherwise `max_rows + 1`, so `catalog.query`'s independent
            # fetch-and-compare can actually fire (see preview_flow).
            top = panel.top
            limit = (top if top is not None and top <= body.max_rows
                     else body.max_rows + 1)
            compiled = compile_flow_now(catalog, flow, limit=limit)
        except Exception as exc:
            # `FlowRefused` included: its message names steps, columns and —
            # on a source refusal — dataset schemas, which is authoring text.
            # The editor who could have written this panel gets the sentence;
            # a viewer gets the laundered `Failure` brief.
            raise stored_instruction_error(
                exc, subject=f"panel:{panel_id}", author=Role.editor
            ) from None
        return _execute_sql(
            catalog, store, perms, user, compiled.sql, body.max_rows,
            params=compiled.params, server_authored=True,
        )
    return _execute_sql(catalog, store, perms, user, panel.sql, body.max_rows)


def _check_flow_panel(
    catalog: DatasetCatalog,
    store: MetadataStore,
    perms: PermissionService,
    user: User,
    panel: DashboardPanel,
) -> None:
    """Validate a *flow* panel at save time; a no-op for the other two kinds.

    Tier A, the author's view rights on the sources (before compilation — the
    same disclosure ordering as `_compiled_flow`), Tier B against the live
    schema, and then the chart bindings against the compiled result schema.
    The binding check is the one only the server can hold: the Explore UI's
    dropdowns are populated from a preview, but a flow edited later can drift
    away from bindings saved earlier, and a client picker cannot see that.

    These are EDITOR routes and the flow is the caller's own submission, so
    `FlowRefused` is served verbatim (`_flow_or_400`) exactly as the authoring
    routes under /flows serve it. (SQL panels stay unvalidated — their text
    cannot be checked without executing it — and object panels are validated
    by the ontology at run time.)
    """
    if not panel.is_flow_panel:
        return
    flow = _flow_or_400(FlowDef.from_json, panel.flow)
    compiled = _flow_or_400(_compiled_flow, catalog, store, perms, user, flow)
    produced = set(compiled.schema)
    bindings = [("x", panel.x), ("series", panel.series)]
    bindings += [("y", column) for column in panel.y]
    for label, bound in bindings:
        if bound and bound not in produced:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"This panel binds {label} to column {bound!r}, but the "
                    "flow's result does not produce it. Its columns are: "
                    + ", ".join(repr(c) for c in compiled.schema) + "."
                ),
            )


@router.post("/dashboards/{name}/panels", dependencies=[EDITOR])
def add_dashboard_panel(
    name: str, panel: DashboardPanel, catalog: CatalogDep, store: StoreDep,
    perms: PermDep, user: UserDep, actor: ActorDep
) -> dict:
    """Append one panel. Exists so a client never has to re-PUT the whole board.

    The whole-board PUT survives for title/description and full replacement, but
    every incremental edit goes through these three routes — a client that only
    ever sends the panel it changed cannot blank the panel it did not.
    """
    dash = store.get_dashboard(name)
    if dash is None:
        raise HTTPException(status_code=404, detail=f"Dashboard not found: {name!r}")
    if any(p.id == panel.id for p in dash.panels):
        raise HTTPException(
            status_code=409, detail=f"Panel {panel.id!r} already exists"
        )
    if len(dash.panels) >= 50:
        raise HTTPException(status_code=400, detail="A dashboard is limited to 50 panels")
    _check_flow_panel(catalog, store, perms, user, panel)
    return _save_panels(store, dash, [*dash.panels, panel], actor)


@router.put("/dashboards/{name}/panels/{panel_id}", dependencies=[EDITOR])
def update_dashboard_panel(
    name: str, panel_id: str, panel: DashboardPanel, catalog: CatalogDep,
    store: StoreDep, perms: PermDep, user: UserDep, actor: ActorDep
) -> dict:
    dash, _existing = _stored_panel(store, name, panel_id)
    updated = panel.model_copy(update={"id": panel_id})
    _check_flow_panel(catalog, store, perms, user, updated)
    return _save_panels(
        store, dash,
        [updated if p.id == panel_id else p for p in dash.panels],
        actor,
    )


@router.delete("/dashboards/{name}/panels/{panel_id}", dependencies=[EDITOR])
def delete_dashboard_panel(
    name: str, panel_id: str, store: StoreDep, actor: ActorDep
) -> dict:
    dash, _panel = _stored_panel(store, name, panel_id)
    return _save_panels(
        store, dash, [p for p in dash.panels if p.id != panel_id], actor
    )


def _save_panels(
    store: MetadataStore, dash: DashboardInfo, panels: list[DashboardPanel], actor: str
) -> dict:
    info = dash.model_copy(update={
        "panels": _label_panels(panels), "updated_at": utcnow_iso(),
    })
    store.upsert_dashboard(info)
    store.log_audit(
        "dashboard_updated", {"dashboard": info.name, "panels": len(info.panels)},
        actor=actor, min_read_role=Role.editor,
    )
    return _dump(info) | {"warnings": _panel_warnings(info.panels)}


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
    panels = _preserve_operational(body.panels, existing)
    info = DashboardInfo(
        name=name,
        title=body.title.strip(),
        description=body.description.strip(),
        panels=panels,
        created_at=existing.created_at if existing else utcnow_iso(),
        created_by=existing.created_by if existing else actor,
        updated_at=utcnow_iso(),
    )
    store.upsert_dashboard(info)
    store.log_audit(
        "dashboard_updated" if existing else "dashboard_created",
        {"dashboard": name, "panels": len(panels)},
        actor=actor,
        # Nothing here but a name and a count, both first-party.
        min_read_role=Role.editor,
    )
    return _dump(info) | {"warnings": _panel_warnings(panels)}


def _label_panels(panels: list[DashboardPanel]) -> list[DashboardPanel]:
    """Give every panel a non-empty title, at WRITE time.

    `Dashboards.tsx` fell back to `p.sql.slice(0, 48)` for the label and
    `Workbench.tsx` ships an empty title by default, so unlabeled panels are the
    norm — withholding `sql` without this produces exactly the empty-box failure
    the brief forbids. Fixing it here rather than at read time means `title` is
    simply always present, always PRESENTATION, with no computed field and no
    serialization magic.
    """
    out = []
    for n, panel in enumerate(panels, start=1):
        out.append(panel if panel.title.strip()
                   else panel.model_copy(update={"title": f"Panel {n}"}))
    return out


def _preserve_operational(
    panels: list[dict], existing: Optional[DashboardInfo]
) -> list[DashboardPanel]:
    """Belt and braces against a read-modify-write client blanking a query.

    `Dashboards.tsx` and `Workbench.tsx` both re-PUT *every* panel from a
    fetched record. If any principal ever receives a trimmed panel — an editor
    demoted mid-session, a stale tab, a script written against the viewer
    projection — saving one panel would blank the SQL of every other. The
    per-panel routes are the real fix; this is the one that survives an old
    client.

    **Absence is the signal, not emptiness.** For a submitted panel whose `id`
    matches a stored one, an OPERATIONAL field the request does not mention at
    all inherits the stored value; a field the request *sends* is honoured,
    including when what it sends is `""` or `[]` or `{}`.

    That distinction is exact, and it is the whole rule. `serialize._projection`
    **omits** operational keys rather than blanking them — a documented,
    tested property — so a client round-tripping a trimmed record has those keys
    absent, and inherits. A client deliberately clearing one sends it, and
    clears it.

    The earlier version tested `not merged.get(field)`, which cannot tell those
    two apart, and the cost was not theoretical. Measured: an editor clearing a
    panel's `group_by` got `200` and the old value back — a write silently
    rejected with a success status. Converting a SQL panel to an object panel
    was unreachable entirely: the server re-inserted the stored `sql`, then
    returned `400 "A panel draws from either sql or object_type, not both"`,
    blaming the editor for sending something the server had just added. The
    shipped UI escapes both by using the per-panel routes, so the break landed
    on API and script clients and on the whole-board route the CHANGELOG
    documents for full replacement.

    Raises ``ValueError`` (→ 400) if a merged panel still does not validate, so
    the shape rules of `DashboardPanel` are enforced on what is actually
    stored rather than on what was sent.
    """
    stored = {p.id: p for p in (existing.panels if existing else [])}
    out = []
    for raw in panels:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="Each panel must be an object")
        merged = dict(raw)
        prior = stored.get(str(raw.get("id", "")))
        if prior is not None:
            for field in _OPERATIONAL_PANEL_FIELDS:
                if field not in merged and getattr(prior, field):
                    merged[field] = getattr(prior, field)
        try:
            out.append(DashboardPanel.model_validate(merged))
        except ValidationError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"panel {raw.get('id', '?')!r}: {exc.errors()[0]['msg']}",
            ) from None
    return _label_panels(out)


# The panel fields a viewer never receives, and therefore the fields a
# round-tripping client can silently drop. Derived from the annotations rather
# than restated, so a field added to DashboardPanel joins this list by default.
_OPERATIONAL_PANEL_FIELDS = tuple(
    f for f in DashboardPanel.model_fields
    if audience.field_audience(DashboardPanel, f) is not audience.Audience.PRESENTATION
)


def _panel_warnings(panels: list[DashboardPanel]) -> list[dict]:
    """Non-blocking authoring hints. **Not a security control** — see
    laurelin/core/authoring_hints.py.

    This used to be a 400 that refused the save. It is a banner now because
    nothing depends on it any more: the SQL is not served to a viewer whatever
    it contains, so the matcher being wrong costs an editor a yellow box instead
    of costing a viewer a password. Blocking would keep an undecidable test on
    the critical path of a legitimate save — and `password = 'x'` in a WHERE
    clause is a legitimate save.
    """
    out = []
    for i, panel in enumerate(panels):
        if authoring_hints.credential_in_free_text(panel.sql):
            out.append({
                "field": f"panels[{i}].sql",
                "hint": "this looks like a connection string with a credential "
                        "in it. Viewers cannot read panel SQL, but a credential "
                        "in a stored query is still a credential in a database "
                        "— register the remote table as a federated dataset and "
                        "select from that instead.",
            })
    return out


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
    from laurelin.core.federation import FederationError

    existing = store.get_dataset(name)
    if existing is not None and not existing.is_federated:
        raise HTTPException(
            status_code=409,
            detail=f"Dataset {name!r} already exists as a managed dataset",
        )
    try:
        info = catalog.register_federated(name, body.source, body.description)
    except FederationError as exc:
        # DuckDB's postgres extension prefixes its IO Error with the whole
        # connection string, so this 502 body carried a live password into the
        # browser, the reverse proxy's access log and the UI's error box — the
        # line directly below this one calls `redacted_source` on the success
        # path for the same dict. Admin-only, so not a privilege crossing, but
        # the operator never asked for their credential to be echoed back.
        raise HTTPException(
            status_code=502,
            detail=serialize.detail_for(
                _driver_failure(exc, body.source, f"dataset:{name}"), Role.admin
            ),
        ) from None
    store.log_audit(
        "federated_dataset_registered",
        {"dataset": name, "type": body.source.get("type")},
        actor=actor,
    )
    # `_dump` puts the redacted source back for an admin itself
    # (`serialize._apply_dataset_source_rule`), so re-attaching it here was both
    # redundant and a bypass of the one serialization point — the exact shape
    # `test_no_api_module_serializes_a_model_outside_the_serializer` exists to
    # forbid. These three routes are ADMIN, so nothing leaked; it was one
    # relaxed gate away from leaking.
    return _dump(info)


@router.put("/datasets/{name}/clickhouse", dependencies=[ADMIN])
def register_clickhouse_dataset(
    name: str,
    body: FederatedDatasetRequest,
    catalog: CatalogDep,
    store: StoreDep,
    actor: ActorDep,
) -> dict:
    """Register a read-only table scanned by embedded ClickHouse.

    Admin-only, for the same reason federation is, and one more: chdb has no
    equivalent of the filesystem sandbox the DuckDB path runs behind (see
    laurelin/core/clickhouse.py), so the set of people who may point the server
    at a path is deliberately the smallest one.
    """
    from laurelin.core.clickhouse import ClickHouseError, available

    if not available():
        raise HTTPException(
            status_code=501,
            detail="ClickHouse support needs chdb: pip install 'laurelin[clickhouse]'",
        )
    existing = store.get_dataset(name)
    if existing is not None and not existing.is_clickhouse:
        raise HTTPException(
            status_code=409,
            detail=f"Dataset {name!r} already exists as a {existing.kind} dataset",
        )
    try:
        info = catalog.register_clickhouse(name, body.source, body.description)
    except ClickHouseError as exc:
        # chdb rewrites `s3://k:pw@bucket/x` to `s3:/k:pw@bucket/x` in its error
        # text, which removes the `://` every shape rule in `redaction` keys on
        # — so this one is caught by substituting the credentials the config
        # says we handed over, not by recognising a URL.
        raise HTTPException(
            status_code=502,
            detail=serialize.detail_for(
                _driver_failure(exc, body.source, f"dataset:{name}"), Role.admin
            ),
        ) from None
    store.log_audit(
        "clickhouse_dataset_registered",
        {"dataset": name, "type": body.source.get("type")},
        actor=actor,
    )
    # `_dump` puts the redacted source back for an admin itself
    # (`serialize._apply_dataset_source_rule`), so re-attaching it here was both
    # redundant and a bypass of the one serialization point — the exact shape
    # `test_no_api_module_serializes_a_model_outside_the_serializer` exists to
    # forbid. These three routes are ADMIN, so nothing leaked; it was one
    # relaxed gate away from leaking.
    return _dump(info)


@router.put("/datasets/{name}/starrocks", dependencies=[ADMIN])
def register_starrocks_dataset(
    name: str,
    body: FederatedDatasetRequest,
    catalog: CatalogDep,
    store: StoreDep,
    actor: ActorDep,
) -> dict:
    """Register a read-only table served by a StarRocks cluster.

    Admin-only, and for a sharper reason than federation's: the source carries
    a DSN with credentials for a *remote* engine on which stacked statements
    execute (see laurelin/core/starrocks.py). The account those credentials name
    should hold SELECT and nothing else; the set of people who may point the
    server at one is deliberately the smallest.
    """
    from laurelin.core.starrocks import StarRocksError, available

    if not available():
        raise HTTPException(
            status_code=501,
            detail="StarRocks support needs a MySQL-protocol client: "
                   "pip install 'laurelin[starrocks]'",
        )
    existing = store.get_dataset(name)
    if existing is not None and not existing.is_starrocks:
        raise HTTPException(
            status_code=409,
            detail=f"Dataset {name!r} already exists as a {existing.kind} dataset",
        )
    try:
        info = catalog.register_starrocks(name, body.source, body.description)
    except StarRocksError as exc:
        # `str(StarRocksError)` is now `failure.render()`, so this route joins
        # the others by construction rather than by remembering to.
        raise HTTPException(
            status_code=502,
            detail=serialize.detail_for(exc.failure, Role.admin)
            if getattr(exc, "failure", None) else str(exc),
        ) from None
    store.log_audit(
        "starrocks_dataset_registered",
        {"dataset": name, "table": body.source.get("table")},
        actor=actor,
    )
    # `_dump` puts the redacted source back for an admin itself
    # (`serialize._apply_dataset_source_rule`), so re-attaching it here was both
    # redundant and a bypass of the one serialization point — the exact shape
    # `test_no_api_module_serializes_a_model_outside_the_serializer` exists to
    # forbid. These three routes are ADMIN, so nothing leaked; it was one
    # relaxed gate away from leaking.
    return _dump(info)


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
            raise HTTPException(status_code=400, detail=safe_detail(exc, subject="request")) from None
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
    from laurelin.export import require_pipelines_acknowledged

    targets = body.targets if body else None
    if targets is not None and len(targets) == 0:
        raise HTTPException(
            status_code=400,
            detail="targets must be non-empty when provided; omit it to build everything",
        )
    # ImportRefused -> 409, with a message naming the acknowledgement route. A
    # build is the moment imported pipeline code would actually run.
    require_pipelines_acknowledged(workspace)
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
    # The materialization is shared and unpoliced, so its counters describe
    # *every* tenant's objects. A user whose row-level security shows them two
    # of four objects was told the type had four, and `applied_seq` adds a
    # global write-volume signal beside it. Both are operator numbers: they go
    # to callers the backing dataset's policy does not narrow.
    unpoliced = service._policy_for_dataset(ot.backing_dataset) is None
    result["index"] = {
        "indexed": state is not None,
        "fresh": service.index_is_fresh(ot) if state is not None else False,
        "objects": (state["object_count"] if state else 0) if unpoliced else None,
        # "Stale, rebuild it" and "behind by 3, catching up" are different
        # things to tell someone, and only one of them is a problem.
        "lag": (state["lag"] if state else 0) if unpoliced else None,
        "store": state["store"] if state else None,
        "applied_seq": (state["applied_seq"] if state else 0) if unpoliced else None,
        # Which *kind* of not-fresh, which lag alone could never say. A store
        # that is a version behind and also holds unapplied edits reported
        # "behind by 3" and climbing, while `catch_up` bailed on the version
        # mismatch before replaying anything — so the only true remedy, a
        # rebuild, was the one the UI did not name.
        #
        # Deliberately *not* withheld from a policied caller, unlike the
        # counters above. A dataset version number counts versions, not rows:
        # it says nothing about how many objects exist or who owns them, and
        # viewing this object type already requires view on the backing
        # dataset, whose version list is on its own page.
        "dataset_version": state["dataset_version"] if state else None,
        "current_dataset_version": state["current_dataset_version"] if state else None,
        "stale_version": state["stale_version"] if state else None,
        "stale_definition": state["stale_definition"] if state else None,
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
            # serialize-ok: the caller's own request body on its way into a
            # query, not a stored record on its way out.
            metrics=[m.model_dump() for m in body.metrics],
            filters=body.filters or None,
            search=body.search,
            limit=body.limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=safe_detail(exc, subject="request")) from None


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
    name: str,
    service: OntologyDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
) -> dict:
    """Materialize an object type into the index.

    Opt-in per type: indexing trades storage and build time for query speed,
    which is worth it for entities and wasteful for high-volume events. Once
    built, the index is refreshed automatically after each build and bypassed
    whenever it is stale.

    Gated per object type, like every other ``/ontology`` route. The global
    EDITOR role alone was the whole check here, and it let two things through
    that the routes on either side of it refuse. A global editor with *no*
    grant on this type — 403 on ``GET /ontology/object-types/{name}`` and 403
    on its objects — got a 200 here carrying the global object count and the
    whole state block. And a caller with view but a row policy showing them
    three of six objects got ``objects: 6``, the exact number
    ``get_object_type`` deliberately withholds from a policied caller two
    hundred lines above ("operator numbers: they go to callers the backing
    dataset's policy does not narrow"). Rebuilding is a write to the shared
    materialization, so the requirement is *edit*, not view.
    """
    ot = service.ontology.object_type(name)
    if ot is None:
        raise KeyError(f"Unknown object type: {name!r}")
    _require_ot_edit(perms, user, service, name)
    count = service.reindex(name)
    # Verify the digest of what we just built. A detector nobody reads is not a
    # detector, and a rebuild is the one moment where recomputing it is free —
    # every row is already in hand.
    digest_ok = service.verify_digest(ot) if count else True
    store.log_audit("object_index_built",
                    {"object_type": name, "objects": count, "digest_ok": digest_ok},
                    actor=actor)
    # The counters are the ones `get_object_type` guards: a policied caller is
    # told the rebuild happened, not how many rows everyone else has.
    unpoliced = service._policy_for_dataset(ot.backing_dataset) is None
    return {"object_type": name,
            "objects": count if unpoliced else None,
            "digest_ok": digest_ok,
            "state": store.object_index_state(name) if unpoliced else None}


@router.post("/ontology/object-types/{name}/writeback", dependencies=[EDITOR])
def writeback_object_type(
    name: str,
    service: OntologyDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
    allow_transform_backed: bool = False,
) -> dict:
    """Fold the edit overlay into a new version of the backing dataset.

    Authorization is the subtle part: this rewrites a *dataset*, so EDITOR on
    the object type is not enough. Someone with edit rights on an object type
    but not its backing dataset can already record edits; letting them rewrite
    the dataset is a different privilege, and dataset ACLs are what express it.
    """
    ot = service.ontology.object_type(name)
    if ot is None:
        raise KeyError(f"Unknown object type: {name!r}")
    _require_ot_edit(perms, user, service, name)
    _require_dataset_edit(perms, user, ot.backing_dataset)
    # Runs unpoliced inside the service; see OntologyService.writeback.
    return service.writeback(name, actor=actor,
                             allow_transform_backed=allow_transform_backed)


def _edit_log_report(service, perms, user, name: str, keep: int) -> dict:
    """Shared body of the two edit-log routes: gate, then plan.

    The counters are write-volume numbers about a *shared* log, so they follow
    the rule the index block above set: a caller whose row policy narrows the
    backing dataset is told the log exists, not how much of it there is.
    """
    ot = service.ontology.object_type(name)
    if ot is None:
        raise KeyError(f"Unknown object type: {name!r}")
    _require_ot_edit(perms, user, service, name)
    if service._policy_for_dataset(ot.backing_dataset) is not None:
        return {"object_type": name, "backing_dataset": ot.backing_dataset,
                "withheld": True}
    plan = service.prune_plan(name, keep)
    plan.pop("prunable_ids")  # the count and the bytes are the answer; the
    # id list is an implementation detail of executing the plan.
    return {**plan, "withheld": False}


@router.get("/ontology/object-types/{name}/edit-log", dependencies=[EDITOR])
def object_edit_log(
    name: str,
    service: OntologyDep,
    perms: PermDep,
    user: UserDep,
    keep: int = Query(0, ge=0),
) -> dict:
    """What this object type's edit log holds, and what pruning it would free.

    Read-only, and the only way to see the log's size before deciding anything:
    the log is never trimmed on its own, so a workspace using the ontology as
    an application database grows this table forever. ``keep`` is a hypothesis
    — "if I retained the newest N folded edits, what would go?" — so the same
    plan can be inspected before it is executed.
    """
    return _edit_log_report(service, perms, user, name, keep)


@router.post("/ontology/object-types/{name}/edit-log/prune", dependencies=[ADMIN])
def prune_object_edit_log(
    name: str,
    service: OntologyDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
    keep: int = Query(..., ge=0),
    dry_run: bool = False,
) -> dict:
    """Delete folded edits this type's log no longer needs. ADMIN.

    A rank above writeback deliberately. Folding is a data operation an editor
    performs and every effect of it stays visible; pruning deletes the record
    of *who changed what*, and that record is the reason this system is worth
    auditing. The safety condition is enforced in the service and again in the
    store — see ``OntologyService.prune_plan`` — so the gate here is about
    authority over history, not about correctness.
    """
    ot = service.ontology.object_type(name)
    if ot is None:
        raise KeyError(f"Unknown object type: {name!r}")
    _require_ot_edit(perms, user, service, name)
    return service.prune_object_edits(name, keep, actor=actor, dry_run=dry_run)


@router.delete("/ontology/object-types/{name}/index", dependencies=[EDITOR])
def drop_object_index(
    name: str,
    service: OntologyDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
) -> dict:
    """Drop one object type's index.

    Same gate as building it, and it needs one more than build does: a caller
    with no grant on this type could delete the index of a type they cannot
    read, and the owner's next query silently fell back to a full scan.
    """
    if service.ontology.object_type(name) is None:
        raise KeyError(f"Unknown object type: {name!r}")
    _require_ot_edit(perms, user, service, name)
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
        # serialize-ok: request body -> store.
        type_name, [g.model_dump(mode="json") for g in body.grants]
    )
    store.log_audit(
        "ontology_permissions_set",
        {"object_type": type_name, "grant_count": len(body.grants)},
        actor=actor,
    )
    # serialize-ok: echoes back the grants this admin just sent.
    return {"object_type": type_name,
            "grants": [g.model_dump(mode="json") for g in body.grants]}


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
    # serialize-ok: request body -> store.
    store.set_grants_for_dataset(name, [g.model_dump(mode="json") for g in body.grants])
    store.log_audit(
        "dataset_permissions_set",
        {"dataset": name, "grant_count": len(body.grants)},
        actor=actor,
    )
    # serialize-ok: echoes back the grants this admin just sent.
    return {"dataset": name,
            "grants": [g.model_dump(mode="json") for g in body.grants]}


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
    name: str, body: DatasetPolicyRequest, store: StoreDep, actor: ActorDep,
    workspace: WorkspaceDep,
) -> dict:
    """Set (or clear) the row policy and column masks on a dataset.

    The write always succeeds, but a row policy on a dataset some transform
    READS comes back with a warning naming the transforms: flow governance
    refuses row-policied sources unconditionally (a flow's output is a new
    dataset without the policy, so building would launder every row), which
    means those flows stop building — and stop being editable or previewable —
    the moment this policy lands. Measured on this tree: the breakage
    otherwise surfaces only on the next scheduled build, hours after the
    admin's 200. Policying a flow's *output* is fine.
    """
    if store.get_dataset(name) is None:
        raise KeyError(f"Dataset not found: {name!r}")
    empty = body.row_policy is None and not body.column_masks
    policy = None if empty else {
        # serialize-ok: request body -> store.
        "row_policy": body.row_policy.model_dump(mode="json") if body.row_policy else None,
        # serialize-ok: request body -> store.
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
    warnings: list[str] = []
    if body.row_policy is not None:
        # Best-effort: a broken pipeline file must not block a governance
        # write (get_registry 409s on one), so collection failure just means
        # no warning. The refusal itself is enforced at build/author time by
        # flow_governance either way.
        try:
            registry = get_registry(workspace)
        except HTTPException:
            registry = None
        if registry is not None:
            readers = sorted(
                spec.name for spec in registry.all()
                if any(inp.dataset == name for inp in spec.inputs.values())
            )
            if readers:
                warnings.append(
                    f"Dataset {name!r} is read by transform(s) "
                    f"{', '.join(repr(r) for r in readers)}. A row policy on "
                    "a transform's source refuses its builds, previews and "
                    "edits (fail-closed: the output would launder the policied "
                    "rows). Those transforms will fail until the policy is "
                    "removed or they are repointed at an unpolicied copy."
                )
    return {"dataset": name, "policy": policy, "warnings": warnings}


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

@router.get("/audit", dependencies=[EDITOR])
def list_audit(
    store: StoreDep, user: UserDep, limit: int = Query(100, ge=0, le=10_000)
) -> list[dict]:
    """The audit trail, filtered to the rows this principal may read.

    **Was VIEWER-gated.** The argument for moving it, in the order the
    objections come:

    1. *The bag is the bug, not its contents.* `log_audit` takes an open dict.
       Rounds 2 and 3 were both "a writer put driver text in the bag"; the SAML
       writer at `auth_routes.py:413` did it with no redaction at all, on an
       unauthenticated route, from attacker-supplied input. A backstop that
       inspects key *names* cannot help a key called `reason`.
    2. *The worst leak here was never a credential, so no matcher was going to
       catch it.* `ontology/service.py` logged `"parameters": payload` on
       `action_applied`, and a viewer holding a **403** on the object type read
       that object's property values off `/audit`. Governed data, one privilege
       level below its own ACL, in a governance product. That key is dropped.
    3. *The viewer's stated need survives.* "A user should see what was done" is
       served exactly by `/audit/mine`, which discloses nothing new by
       construction: you cannot learn a secret from a row you wrote.
    4. *The honest counter.* "What was done *to* me" is not "what I did", and a
       viewer whose object an editor edited may want to know. Agreed — but that
       is a notification feature, and shipping it through a raw details bag is
       precisely how three rounds of leaks happened. It is unbuilt; the task
       number that used to be cited here now belongs to unrelated work, which
       is the same reason the CHANGELOG stopped citing line numbers.
    5. *Why not keep VIEWER with structured details?* Because "structured" is
       not "non-sensitive": `parameters` was already structured. Structure fixes
       R1's problem; only privilege fixes R2's.

    `min_read_role` also closes the editor↔admin crossing — an editor used to
    read `source_updated` details written by an admin.
    """
    return [_dump(entry) for entry in store.list_audit(limit, role=user.role)]


@router.get("/audit/mine", dependencies=[VIEWER])
def list_own_audit(
    store: StoreDep, user: UserDep, limit: int = Query(100, ge=0, le=10_000)
) -> list[dict]:
    """What *this* user did — through the same serializer as every other route.

    This used to be `_dump(entry) | {"details": entry.details}`: an explicit
    override of the projection, in the module that defines the single
    serialization point. The justification was that "the details came from their
    own request", and it was false. A viewer triggering a source sync supplies a
    *name*; Laurelin builds the rest of the bag from an ADMIN-authored connector
    config, and the whole `Failure` — endpoint, driver, `detail_ref`, rendered
    message — came back to them. Pre-migration rows were worse: the bag held a
    driver's raw sentence, the migration stamped it `admin` for exactly that
    reason, and this route served it to a demoted viewer anyway.

    A viewer now gets the header of their own rows — who, what, when — and the
    `details` of any row whose writer declared a level they cover. That is the
    stated need ("a user should see what was done") without an open bag
    crossing a privilege boundary on the strength of a comment.
    """
    return [_dump(entry) for entry in store.list_audit(limit, actor=user.username)]


# ---------------------------------------------------------------------------
# Pipeline authoring (transform files)
#
# SECURITY: writing a pipeline file is code-execution-equivalent — the file is
# exec'd on every build/collection. Reads AND writes are editor (R2 raised the
# reads; see the note on the GET routes below), and writes/deletes can be
# disabled server-wide with --lock-pipelines. See docs/ARCHITECTURE.
# ---------------------------------------------------------------------------

class PipelineWriteRequest(BaseModel):
    content: str


class QueryTransformRequest(BaseModel):
    sql: str
    output: str
    name: Optional[str] = None


# R2 raised both of these from VIEWER to EDITOR. They return `exec`-ed Python —
# this module's own SECURITY note directly above says writing one is
# code-execution-equivalent — and I confirmed live that a viewer read a
# driver-authored sentinel out of GET /pipelines/{name}. If you cannot write it,
# you cannot read it, and a viewer cannot write a pipeline.
#
# The viewer's legitimate need here is *lineage*, and it is already served
# structurally and stays VIEWER: GET /transforms names every transform, GET
# /lineage gives the edges. Names and edges, not authored prose.
@router.get("/pipelines", dependencies=[EDITOR])
def list_pipelines(files: PipelineFilesDep) -> list[dict]:
    return files.list()


@router.get("/pipelines/{name}", dependencies=[EDITOR])
def read_pipeline(name: str, files: PipelineFilesDep) -> dict:
    return files.read(name)


@router.put("/pipelines/{name}", dependencies=[EDITOR, Depends(require_pipelines_unlocked)])
def write_pipeline(
    request: Request,
    name: str,
    body: PipelineWriteRequest,
    files: PipelineFilesDep,
    store: StoreDep,
    actor: ActorDep,
) -> dict:
    result = files.write(name, body.content)
    # Server-side author stamp, same contract as FlowFiles.write: the recorded
    # author is whose read access every build of this file is checked against
    # (Builder._check_input_entitlement), so it is never taken from the body.
    # A re-save by a different editor re-stamps the WHOLE file to the new
    # saver: every transform in a multi-transform .py builds as its last saver.
    _stamp_pipeline_author(request, store, result["name"], actor)
    store.log_audit("pipeline_written", {"name": result["name"]}, actor=actor)
    return result


@router.delete("/pipelines/{name}", dependencies=[EDITOR, Depends(require_pipelines_unlocked)])
def delete_pipeline(name: str, files: PipelineFilesDep, store: StoreDep, actor: ActorDep) -> dict:
    files.delete(name)
    # A deleted file's stamp goes with it. A DISK edit to a stamped file does
    # NOT clear the stamp — the stamp means "last API author", and staying
    # checked is the fail-closed direction; an operator who wants the file
    # operator-trusted deletes it here and rewrites it on disk.
    store.delete_pipeline_author(name[:-3] if name.endswith(".py") else name)
    store.log_audit("pipeline_deleted", {"name": name}, actor=actor)
    return {"ok": True}


@router.post("/pipelines/from-query", dependencies=[EDITOR, Depends(require_pipelines_unlocked)])
def pipeline_from_query(
    request: Request,
    body: QueryTransformRequest,
    files: PipelineFilesDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
) -> dict:
    """Create a pipeline file wrapping a workbench query as a SQL transform."""
    # Filtered through the caller's view rights before the regex sees it. The
    # inference is a word-boundary match over the query text, so an unfiltered
    # list let a generated pipeline *declare an input its author cannot view* —
    # which then builds as the system and lands the data in a new dataset. The
    # regex is still a guess (see generate_sql_transform); this at least bounds
    # what it can guess at.
    dataset_names = sorted(
        perms.viewable_datasets(user, [d.name for d in store.list_datasets()])
    )
    result = files.generate_sql_transform(
        body.sql, body.output, dataset_names, name=body.name
    )
    # Same server-side author stamp as PUT above; a generated file is checked
    # at every build against the user who saved the query, not the regex's
    # guess about what it reads.
    _stamp_pipeline_author(request, store, result["name"], actor)
    store.log_audit(
        "pipeline_written", {"name": result["name"], "source": "query"}, actor=actor
    )
    return result


# ---------------------------------------------------------------------------
# Flows: the no-code pipeline builder
#
# A flow is a declarative artifact (`pipelines/<name>.flow.json`) compiled to
# SQL in memory. It is NOT exec'd — that is the whole point, and the reason an
# analyst may author one where they may not author Python.
#
# Every route here is EDITOR, not VIEWER, for the same reason the pipeline
# routes above are: if you cannot write it, you cannot read it. A viewer's
# legitimate need is lineage, and `GET /transforms` and `GET /lineage` already
# serve that structurally — a flow appears there as a transform with
# `kind: "flow"`, with no change to either route.
#
# Writes are gated on `--lock-flows`, NOT on `--lock-pipelines`. That is the
# "new flag and a new decision" an earlier revision of this comment reserved,
# and this is the decision: `--lock-pipelines` locks *code* — a pipeline file
# is Python exec'd as the server process — while a flow is a closed IR
# compiled to parameterised SQL (every value bound, every identifier checked
# against the live schema, refused by `check_flow_governance` if it would
# launder a mask) and re-checked against its recorded author at every build.
# Locking flows alongside Python left hardened deployments with no authoring
# path at all, which meant nobody set the flag and every editor kept RCE.
# Operators who want the old total lockdown set both flags.
#
# Eject is the exception: it is both a flow gesture and a `.py` write, so it
# refuses if EITHER flag is set.
# ---------------------------------------------------------------------------

def get_flow_files(workspace: WorkspaceDep) -> FlowFiles:
    return FlowFiles(workspace.pipelines_dir)


FlowFilesDep = Annotated[FlowFiles, Depends(get_flow_files)]

#: Flow previews are capped far below the workbench's 1000/100000. A canvas
#: that previews as you build competes for the same process-wide admission
#: semaphore (`limits.py`: 8 slots) as dashboards, object reads and row pages,
#: and the failure mode of losing that race is `QueryRejected` + `Retry-After`
#: for *everyone else on the replica*. 200 rows is enough to see the shape of
#: an answer, which is what a preview is for.
FLOW_PREVIEW_MAX_ROWS = 200


class FlowWriteRequest(BaseModel):
    flow: dict


class FlowPreviewRequest(BaseModel):
    # The DRAFT flow, not a saved one. Previewing an unsaved draft is the whole
    # interaction; requiring a save first would make the canvas useless.
    flow: dict
    node_id: Optional[str] = None
    max_rows: int = 50


def _flow_or_400(fn, *args, **kwargs):
    """Run a flow operation, converting `FlowRefused` to a 400.

    `FlowRefused`'s message is first-party by construction (see its docstring):
    it is Laurelin's own sentence, it names the author's own step and column,
    and it is raised *before* any SQL exists — so unlike the DuckDB errors that
    `_execute_sql` has to launder through `Failure`, it can be served verbatim.
    That is the whole point: a non-programmer needs to be told what to fix.
    """
    try:
        return fn(*args, **kwargs)
    except FlowRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _compiled_flow(catalog, store, perms, author: str, flow, **kwargs):
    """Sources-check, then compile. The ordering is a disclosure control.

    `flow_compile.resolve_column` names a dataset's available columns when an
    identifier misses — deliberately, because that is the single most useful
    thing to tell an author who mistyped one. Those names are a schema, and a
    schema is information. Checking view rights *first* means the compiler is
    only ever pointed at datasets this author may read.
    """
    check_flow_sources(store, perms, author, flow)
    return compile_flow_now(catalog, flow, **kwargs)


#: Exception class -> what to tell a person who does not write SQL, when a
#: compiled flow still fails inside DuckDB.
#:
#: The static checks in `flow_compile` catch the mistakes that were actually
#: measured, before the flow is even saved. This is the residue, and it exists
#: because the generic pipe is *false* here: `_execute_sql` classifies a
#: DuckDB binder error as `column_missing`, whose Laurelin-authored sentence
#: was "A column referenced does not exist on the remote system" — about a
#: column visibly present in the picker directly below, on a query that ran in
#: embedded DuckDB, where there is no remote system at all.
#:
#: Keyed on `type(exc).__name__`, which is the same fact `Failure.exc_class`
#: records, and mapped to text Laurelin wrote. No driver prose is read, kept or
#: returned; R1 is intact.
_FLOW_EXEC_ADVICE: dict[str, str] = {
    "ConversionException": (
        "One of the values in this data could not be converted to the type a "
        "step asked for. Check any 'cast' step: converting a column to a "
        "number or a date only works if every value in it really is one."
    ),
    "BinderException": (
        "A step combines columns whose kinds do not fit together — summing "
        "text, or comparing text with a number or a date. Check the columns "
        "the last step uses, and add a 'cast' step if one of them needs "
        "converting."
    ),
    "InvalidInputException": (
        "A step was given a value it cannot use. Check the values in the last "
        "step you changed."
    ),
    "OutOfRangeException": (
        "A number in this data is too large or too small for the type a step "
        "asked for. Check any 'cast' step converting to a whole number."
    ),
}


def _flow_execution(run, *args, **kwargs):
    """Run a compiled flow, translating engine errors into flow language.

    Wraps the shared `_execute_sql` rather than replacing it: preview must stay
    on the one policy path (ACL-as-unknown-table, row-level security, column
    masks, the sandbox pragma, admission control), and the only thing that
    differs for a flow is who is reading the error. An operator debugging a
    federated connector wants the failure code; an analyst who has never seen
    SQL wants to know which step to change.
    """
    try:
        return run(*args, **kwargs)
    except HTTPException as exc:
        advice = _FLOW_EXEC_ADVICE.get(getattr(exc, "_flow_exc_class", ""))
        if advice is None:
            raise
        raise HTTPException(status_code=400, detail=advice) from None


def _flow_sources_of(entry: dict) -> list[str]:
    """The datasets a flow reads, from a listing entry *or* a raw read result.

    Walks the raw node list rather than `FlowDef.source_datasets()`, because
    `FlowFiles.read` deliberately echoes back a structurally-invalid flow so
    the builder can repair it — and a broken flow must still be gated on what
    it names. Anything unparseable yields the sentinel below, which no caller
    can be granted, so a file we cannot read the sources of is a file nobody
    but an admin sees.
    """
    if entry.get("sources") is not None:
        return list(entry["sources"])
    flow = entry.get("flow")
    if not isinstance(flow, dict):
        return ["\0unreadable"]
    nodes = flow.get("nodes")
    if not isinstance(nodes, list):
        return ["\0unreadable"]
    out = []
    for n in nodes:
        if not isinstance(n, dict) or n.get("kind") != "source":
            continue
        dataset = (n.get("params") or {}).get("dataset")
        out.append(dataset if isinstance(dataset, str) else "\0unreadable")
    return out


def _may_read_flow(store, perms, user, entry: dict) -> bool:
    """May this caller see a flow's *definition*?

    A flow's IR is not metadata about a dataset, it is a description of one:
    the source dataset names, the column names it picks, and the literal values
    it filters on. `GET /flows/{name}/sql` already withholds the values —
    "a filter constant can be a customer name … a different entitlement from
    reading the dataset", in its own docstring — and orders `check_flow_sources`
    before compilation for the same reason.

    Its two sibling read routes did neither, so the protection was decorative.
    Measured: `merger_targets` granted to alice; bob was 403 on the dataset's
    rows, 403 on its schema, 403 on `/flows/schema`, and 400 on `/flows/{name}/
    sql` — and `GET /flows/{name}` handed him the column name `codename` and
    the filter value `'PROJECT ORCHID'` in a 200.
    """
    return all(
        perms.can_view_dataset(user, dataset)
        for dataset in _flow_sources_of(entry)
    )


def _reject_workspace_cycle(registry, flow) -> None:
    """Refuse a flow that would put a cycle into the *workspace's* DAG.

    `_validate_graph` checks the shape of the flow's own steps; this checks the
    edge the flow adds to everything else, by walking `registry.by_output` from
    each source the way `Builder.plan` does.

    It matters because the failure is not local. Measured: an editor saved a
    flow named `loop_ds` whose only source was `loop_ds`. `PUT` returned 200
    happily; the cycle was then detected in `Builder.plan`, which runs for
    *every* build — so `POST /builds` with no targets, the path the scheduler
    takes, returned `400 Cycle detected in transform DAG` and every unrelated
    transform in the workspace stopped building until somebody found and
    deleted the flow. `registry.by_output` was already being consulted three
    lines above, for the duplicate-producer check.
    """
    seen = set()

    def walk(name: str, path: list[str]) -> None:
        producer = (
            flow if name == flow.output else None
        ) or registry.by_output(name)
        if producer is None:
            return
        producer_name = getattr(producer, "name", None)
        if producer_name in seen:
            return
        seen.add(producer_name)
        if producer is flow:
            sources = flow.source_datasets()
        else:
            sources = [i.dataset for i in producer.inputs.values()]
        for source in sources:
            if source == flow.output:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "This flow's result would feed back into itself, "
                        "through " + " -> ".join([*path, source]) + ". A "
                        "dataset cannot be built from itself, and saving this "
                        "would stop every build in the workspace, not just "
                        "this one."
                    ),
                )
            walk(source, [*path, source])

    walk(flow.output, [flow.output])


@router.get("/flows", dependencies=[EDITOR])
def list_flows(files: FlowFilesDep, store: StoreDep, perms: PermDep,
               user: UserDep) -> list[dict]:
    """Flows this caller may read the definition of. See `_may_read_flow`.

    Filtered rather than redacted: an entry's `sources` and `name` are the two
    things a listing is *for*, and a flow's name is also the name of the
    dataset it produces, so a redacted row would still be an answer to "what
    restricted datasets exist here". A caller who cannot see a flow still gets
    a clear 409 if they try to claim its output name.
    """
    return [e for e in files.list() if _may_read_flow(store, perms, user, e)]


# Declared before `/flows/{name}`: FastAPI matches in declaration order, and a
# literal path segment registered after a parameterised one never wins.
@router.get("/flows/schema", dependencies=[EDITOR])
def flow_dataset_schema(
    dataset: str, catalog: CatalogDep, perms: PermDep, user: UserDep
) -> dict:
    """Column names of one dataset, for the builder's column picker.

    Gated on view rights for that specific dataset, because a column list is a
    schema. This is also the only route that serves a *source-scanned*
    dataset's schema: `GET /datasets/{name}/schema` resolves through the
    version row, and a federated / ClickHouse / StarRocks / Iceberg dataset has
    none — so that route answers "has no versions" for exactly the datasets a
    remotely-backed flow wants to read.
    """
    _require_dataset_view(perms, user, dataset)
    try:
        types = catalog.column_types(dataset)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from None
    # `kinds` alongside the names, so the step forms can offer "Total of" only
    # for columns that hold numbers. The information was always available in
    # `ColumnSchema.type`; the form offering `sum` over a column of names, and
    # the flow then saving and failing its build, was a UI that had not been
    # given it.
    return {
        "dataset": dataset,
        "columns": list(types),
        "kinds": {name: kind_of(t) for name, t in types.items()},
    }


@router.post("/flows/preview", dependencies=[EDITOR])
def preview_flow(
    body: FlowPreviewRequest,
    catalog: CatalogDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
) -> dict:
    """Run a draft flow (or a prefix of it) **as the caller**.

    Preview is not an oracle for the build, and the UI says so permanently:

    * **Preview is policied; the build is not.** This runs through
      `_execute_sql` with the caller's row-level security and column masks
      applied; the build runs as the system with `sql_policy_for=None`, which
      `catalog.source_table` documents as deliberately fail-open. The direction
      is fail-safe — a preview can never show *more* than the caller may read —
      but a policied analyst previewing 12 rows and building 12 million is the
      default experience, not an edge case.
    * **Masked columns break preview arithmetic.** A `redact` mask renders as
      the string `'***'`, so a sum over it errors or returns nonsense for
      exactly the authors who most need the preview. `masked_columns` says
      which ones, per source, so the UI can annotate rather than let DuckDB
      surface a binder error.
    * Preview reads the latest version now; the build reads whatever is latest
      then.

    Governance here is checked WITHOUT the output-column rule that `PUT` and
    the build apply. Previewing a prefix whose masked column the finished flow
    drops is legitimate — the caller sees `***`, which is the mask working —
    whereas letting that column reach a *built output* is the laundering this
    feature refuses. Strict at save and at build, permissive-but-policied here.
    """
    flow = _flow_or_400(FlowDef.from_json, body.flow, name=body.flow.get("name"))
    max_rows = max(1, min(int(body.max_rows), FLOW_PREVIEW_MAX_ROWS))
    compiled = _flow_or_400(
        _compiled_flow, catalog, store, perms, user, flow,
        upto=body.node_id,
        # One more than we will show, so `truncated` can be *true*. The limit
        # goes into the SQL (spec 5.3: at the previewed terminal only, never
        # pushed upstream), and `catalog.query` independently fetches
        # `max_rows + 1` and compares — so with a limit of exactly `max_rows`
        # the comparison could never fire and `truncated` was structurally
        # always false. Measured on this repo's own demo data: a 56-row
        # dataset previewed as "50 rows · 7 columns", no marker; a 5,000-group
        # aggregate as "200 rows · 2 columns", with 4,800 groups missing and
        # the number reading as the size of the answer.
        limit=max_rows + 1,
    )
    _flow_or_400(check_flow_governance, store, perms, user, flow)
    result = _flow_execution(
        _execute_sql, catalog, store, perms, user, compiled.sql, max_rows,
        params=compiled.params,
        # A compiled flow is server-authored SQL over a closed IR, so a
        # federated / ClickHouse / StarRocks / Iceberg source is registered for
        # it even when the ad-hoc workbench is not allowed to reach one. See
        # `catalog.query`.
        server_authored=True,
    )
    masked: dict[str, list[str]] = {}
    for ds in flow.source_datasets():
        columns = sorted(masked_columns_for(perms, user, ds))
        if columns:
            masked[ds] = columns
    return result | {
        "schema": compiled.schema,
        "kinds": compiled.kinds,
        "node_id": body.node_id or flow.terminal,
        "masked_columns": masked,
        "max_rows": max_rows,
    }


@router.get("/flows/{name}", dependencies=[EDITOR])
def read_flow(
    name: str, files: FlowFilesDep, store: StoreDep, perms: PermDep,
    user: UserDep,
) -> dict:
    try:
        result = _flow_or_400(files.read, name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from None
    if not _may_read_flow(store, perms, user, result):
        # 403, not 404: the caller already learnt the flow exists by asking for
        # it by name, and pretending otherwise would only make the 409 they get
        # from `PUT` inexplicable.
        raise HTTPException(
            status_code=403,
            detail=(
                f"This flow reads a dataset {user.username!r} cannot read, so "
                "its definition — the columns it picks and the values it "
                "filters on — is not available here."
            ),
        )
    result["output_will_be_restricted"] = False
    # Only for a flow that actually validates. `read()` deliberately echoes back
    # a flow that is parseable JSON but structurally invalid, so the builder can
    # open it and repair it rather than being locked out of its own file — and
    # re-validating it here would turn that repair path into a 500.
    if result.get("flow") and not result.get("error"):
        result["output_will_be_restricted"] = output_will_be_restricted(
            store, FlowDef.from_json(result["flow"], name=name)
        )
    return result


@router.put("/flows/{name}", dependencies=[EDITOR, Depends(require_flows_unlocked)])
def write_flow(
    name: str,
    body: FlowWriteRequest,
    files: FlowFilesDep,
    catalog: CatalogDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
    registry: RegistryDep,
) -> dict:
    """Validate a flow completely, then save it.

    Tier A (structure), Tier B (every identifier against the live schema) and
    governance all run *before* the write, so a flow that cannot build is
    refused rather than saved. That mirrors `PipelineFiles.write`, which
    compiles Python before saving, and for the same reason: the alternative is
    a workspace whose registry collection fails, which 409s every route that
    touches it.
    """
    flow = _flow_or_400(FlowDef.from_json, dict(body.flow), name=name)
    # A save may only replace the flow file it names. `by_output` alone is not
    # enough: a *Python* transform whose name equals the flow's would pass a
    # bare name comparison, the file would save, and the next
    # `collect_transforms` would raise `Duplicate transform name` — 409ing
    # every graph-touching route in the workspace until someone deletes the
    # flow file. Measured on this tree before the `kind` check existed.
    # Flow name == output (enforced with a 400 in `FlowDef.from_json`), and
    # the registry this request collected is duplicate-free, so checking the
    # output producer and falling back to the name index covers both the
    # same-output and the same-name-different-output collision.
    existing = registry.by_output(flow.output)
    if existing is None:
        try:
            existing = registry.get(flow.name)
        except KeyError:
            existing = None
    if existing is not None and not (
        existing.kind == "flow" and existing.name == flow.name
    ):
        if existing.kind != "flow":
            what = f"code transform {existing.name!r}"
            if existing.source_file:
                what += f" (pipeline file {existing.source_file!r})"
            detail = (
                f"{what} already uses this name or produces "
                f"{flow.output!r}. A flow cannot replace or share a name "
                "with a code transform; pick a different flow name or remove "
                "the pipeline file first."
            )
        else:
            detail = (
                f"Dataset {flow.output!r} is already produced by transform "
                f"{existing.name!r}. Each dataset may have only one producing "
                "transform."
            )
        raise HTTPException(status_code=409, detail=detail)
    _reject_workspace_cycle(registry, flow)
    compiled = _flow_or_400(
        _compiled_flow, catalog, store, perms, user, flow
    )
    _flow_or_400(
        check_flow_governance, store, perms, user, flow,
        output_columns=compiled.schema, authoring=True,
    )
    saved = _flow_or_400(files.write, name, body.flow, user.username)
    store.log_audit(
        "flow_written",
        {"name": saved.name, "output": saved.output,
         "sources": saved.source_datasets()},
        actor=actor,
    )
    return {
        "name": saved.name,
        "flow": saved.as_json(),
        "schema": compiled.schema,
        "output_will_be_restricted": output_will_be_restricted(store, saved),
    }


@router.delete("/flows/{name}", dependencies=[EDITOR, Depends(require_flows_unlocked)])
def delete_flow(
    name: str, files: FlowFilesDep, store: StoreDep, actor: ActorDep
) -> dict:
    try:
        files.delete(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from None
    except FlowRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    store.log_audit("flow_deleted", {"name": name}, actor=actor)
    return {
        "ok": True,
        # Stated in the response because it is surprising and deliberate.
        # Nothing in this tree deletes lineage except
        # `replace_lineage_for_transform`, and deleting it here would
        # *declassify* every downstream dataset that inherited a marking
        # through this flow. Retaining it is fail-closed.
        "lineage_retained": True,
        "detail": (
            f"Flow {name!r} is deleted. Its lineage and any classification "
            "markings it propagated are kept, so nothing downstream is "
            "declassified. The dataset it produced is not deleted."
        ),
    }


@router.get("/flows/{name}/sql", dependencies=[EDITOR])
def flow_sql(
    name: str,
    files: FlowFilesDep,
    catalog: CatalogDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
) -> dict:
    """The compiled SQL, read-only. Not a round trip.

    The UI renders this in a `<pre>`, labelled "generated from this flow; not
    editable". There is no path from edited SQL back into a flow — that
    direction is a Python-source analyser, unbounded in scope, and it is where
    the silent-overwrite data loss lives.

    `params` is a **count**, never the values. The values are the author's
    data — a filter constant can be a customer name — and this is a different
    entitlement from reading the dataset.
    """
    try:
        flow = _flow_or_400(files.read, name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from None
    if not flow.get("flow"):
        raise HTTPException(status_code=400, detail=flow.get("error") or "Invalid flow")
    parsed = FlowDef.from_json(flow["flow"], name=name)
    compiled = _flow_or_400(
        _compiled_flow, catalog, store, perms, user, parsed
    )
    return {
        "name": name,
        "sql": compiled.sql,
        "params": len(compiled.params),
        "inputs": compiled.inputs,
        "schema": compiled.schema,
    }


# Both guards: eject writes a `.py` (the escape hatch back into code, so
# `--lock-pipelines` must cover it) and consumes a flow file (so a server
# that froze flow authoring should not let flows leak out the side either).
@router.post(
    "/flows/{name}/eject",
    dependencies=[EDITOR, Depends(require_pipelines_unlocked), Depends(require_flows_unlocked)],
)
def eject_flow(
    request: Request,
    name: str,
    files: FlowFilesDep,
    pipelines: PipelineFilesDep,
    catalog: CatalogDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
    workspace: WorkspaceDep,
) -> dict:
    """Convert a flow to a Python pipeline. **One way.**

    The UI gates this behind a type-to-confirm modal saying so. There is no
    un-eject and no import: re-parsing Python into an IR is a Python-source
    analyser, and it is wrong the first time someone writes a helper function.

    Ordering is load-bearing. Write the `.py`, verify it collects, and only
    then unlink the flow — and if the unlink fails, unlink the `.py` again.
    Two producers of one output dataset is a hard `TransformRegistry.register`
    failure, which 409s *every* route that collects the registry: the whole
    workspace, not just this flow.

    **Governance is checked here in full, not just view rights on the sources.**
    It used to call `check_flow_sources` alone, and that made eject a one-click
    downgrade out of every protection flows add. Three measured sequences, all
    API-only, all as a plain editor:

    * Author a flow; an admin later masks a column it reads. `PUT` now 400s and
      the build fails `FlowRefused` — and `eject` returned 200, wrote the .py,
      and the rebuild read the column in plaintext.
    * The same with a row policy: the ejected transform's output handed both
      regions to an editor with no grant at all, where the policied source
      returns one row to its own author.
    * A flow over a granted source promised `output_will_be_restricted: true`;
      ejecting it produced a dataset with `grants: []`, world-readable.

    So: the same `check_flow_governance` the save path and the build path run,
    and `restrict_output_to_author` applied *here*, because after this call
    there is no flow left for `Builder._execute_flow` to apply it for.
    """
    try:
        stored = _flow_or_400(files.read, name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from None
    if not stored.get("flow"):
        raise HTTPException(status_code=400, detail=stored.get("error") or "Invalid flow")
    flow = FlowDef.from_json(stored["flow"], name=name)
    check_flow_sources(store, perms, user, flow)
    sql, inputs, params = _flow_or_400(eject_sql, flow, catalog)
    _flow_or_400(
        check_flow_governance, store, perms, user, flow,
        output_columns=compile_flow_now(catalog, flow).schema, authoring=True,
    )

    if pipelines.exists_py(name):
        raise HTTPException(
            status_code=409,
            detail=f"A Python pipeline named {name!r} already exists.",
        )
    # Before the .py exists, so a crash between the two leaves the grant on
    # rather than off.
    restricted = restrict_output_to_author(store, flow, user.username)
    # Stamp the generated .py's author BEFORE it exists, so a crash between
    # the two leaves a dangling stamp (harmless: it re-attaches, and re-checks,
    # any future file of this name) rather than an UNSTAMPED ejected file —
    # which would fall into the operator-trusted bucket and let eject's
    # one-time authoring check go stale as grants change. Stamped, the ejected
    # file is re-checked against this user at every build, which is strictly
    # stronger than today's eject.
    _stamp_pipeline_author(request, store, name, user.username)
    try:
        # `inputs` from the IR, exactly — bypassing generate_sql_transform's
        # word-boundary regex, which cannot tell a table name from the same
        # word inside a string literal. `params` likewise: the values stay
        # bound, and the generated file binds them too.
        result = pipelines.generate_sql_transform(
            sql, name, dataset_names=[], inputs=inputs, params=params
        )
    except ValueError as exc:
        store.delete_pipeline_author(name)
        raise HTTPException(status_code=400, detail=str(exc)) from None
    try:
        files.delete(name)
    except Exception:
        # Roll the .py back rather than leave two producers of one dataset.
        pipelines.delete(name)
        store.delete_pipeline_author(name)
        raise
    store.log_audit(
        "flow_ejected",
        {"name": name, "inputs": inputs, "output_restricted": restricted},
        actor=actor,
    )
    # `generate_sql_transform` re-collects the workspace as part of writing, and
    # at that instant BOTH the .py and the .flow.json exist and claim the same
    # output — so its `collect_error` is a duplicate-producer complaint about a
    # collision this request is halfway through resolving. Recomputed after the
    # unlink so the response describes the workspace as it now is, rather than
    # reporting a transient state as a defect the author has to act on.
    result = result | {"collect_error": None}
    try:
        collect_transforms(workspace.pipelines_dir)
    except PipelineError as exc:
        result["collect_error"] = Failure.from_exception(
            exc, code=FailureCode.TRANSFORM_FAILED, phase=Phase.compile,
            subject=f"pipeline:{name}", driver="python",
        ).as_dict()
    return result | {
        "ejected": True, "inputs": inputs, "output_restricted": restricted,
    }


# ---------------------------------------------------------------------------
# Explore: point-and-click data-to-chart
#
# Explore has NO representation of its own — not on the wire, not in storage.
# The UI synthesizes a FlowDef (source → filter → derive → aggregate → sort)
# and this route runs it through the exact stack `/flows/preview` runs:
# `FlowDef.from_json` (Tier A), `_compiled_flow` (view rights, then Tier B
# against the live schema, every value bound), `_execute_sql` (the only SQL
# execution path, as the caller). A second, "lighter" query language here
# would be a second compiler, and a second compiler is a second injection and
# governance surface — the mistake this seam exists to prevent.
#
# EDITOR, matching /flows: the shaping vocabulary and `/flows/schema` (which
# feeds Explore's pickers) are authoring surfaces. Viewers consume saved
# panels, which run server-side as themselves.
# ---------------------------------------------------------------------------


class ExplorePreviewRequest(BaseModel):
    # A FlowDef, exactly as /flows/preview takes one. The only query-shaped
    # type this route parses is the one the Flow compiler already validates.
    flow: dict
    max_rows: int = 50


@router.post("/explore/preview", dependencies=[EDITOR])
def preview_explore(
    body: ExplorePreviewRequest,
    catalog: CatalogDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
) -> dict:
    """Run an Explore query — a synthesized flow — **as the caller**.

    `preview_flow` minus exactly one call, and the difference is deliberate:
    **`check_flow_governance` is not run here.** Its row-policy and
    referenced-mask refusals guard *materialization* — "a flow's result is a
    new dataset without that policy" (flow_governance.py) — and Explore
    materializes nothing. Every result is computed by `_execute_sql` under
    the caller's own ACL, row-level security and column masks (the same way
    the workbench and SQL panels already serve row-policied datasets), and a
    saved flow panel re-compiles and re-executes per viewer. Refusing here
    would make Explore unable to chart exactly the governed datasets it
    exists for, while protecting nothing. A referenced masked column is not
    refused either: the mask renders `***` in the scan — the caller watching
    the mask work — and `masked_columns` lets the UI grey those columns out
    of the measure pickers instead of letting DuckDB surface a binder error.

    Everything else is inherited, not re-solved: sources are view-checked
    *before* the compiler can name a column (`_compiled_flow`'s disclosure
    ordering); the limit is bound at the terminal as `max_rows + 1` so
    `truncated` can actually fire; errors are first-party sentences
    (`FlowRefused` verbatim — this is the author's own draft) or
    `_FLOW_EXEC_ADVICE`, never driver echo; and the 200-row / 2GB / 60s /
    8-slot admission posture is `FLOW_PREVIEW_MAX_ROWS` and `limits.py`,
    shared with every other interactive surface.

    `schema` + `kinds` in the response are what drive the UI's binding
    dropdowns (x/y/series), so a chart can only ever be bound to columns the
    compiled result really has.
    """
    flow = _flow_or_400(FlowDef.from_json, body.flow, name=body.flow.get("name"))
    max_rows = max(1, min(int(body.max_rows), FLOW_PREVIEW_MAX_ROWS))
    compiled = _flow_or_400(
        _compiled_flow, catalog, store, perms, user, flow,
        limit=max_rows + 1,
    )
    result = _flow_execution(
        _execute_sql, catalog, store, perms, user, compiled.sql, max_rows,
        params=compiled.params,
        server_authored=True,
    )
    masked: dict[str, list[str]] = {}
    for ds in flow.source_datasets():
        columns = sorted(masked_columns_for(perms, user, ds))
        if columns:
            masked[ds] = columns
    return result | {
        "schema": compiled.schema,
        "kinds": compiled.kinds,
        "masked_columns": masked,
        "max_rows": max_rows,
    }
