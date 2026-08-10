"""Workspace portability over HTTP: export, import, and the governance proof.

Authorization, and the reasoning behind each level — this is the part of the
feature that turns into an exfiltration endpoint if it is wrong.

**Export is admin.** An export is a bulk read of everything: every row of every
managed dataset *pre-policy*, plus every grant, policy, marking and audit line.
The role that already holds exactly that authority is ``admin`` — ``_evaluate``
returns ``(True, True)`` before it looks at a single grant
(``permissions.py:331``), ``_has_clearance`` short-circuits for an admin
(``permissions.py:376``), and every column mask is skipped
(``permissions.py:455-473``). So an admin can already ``GET`` every unmasked row
one dataset at a time; the archive packages authority they hold rather than
granting new authority. An editor or a viewer cannot — a viewer's masks are the
whole point — which is why nothing here is gated any lower, not even the
preview: the preview enumerates every table, every dataset and every withheld
credential's location, which is a map of the deployment.

**Two things need more than workspace admin.**

* ``include_membership`` in multi-workspace mode reads
  ``control.db::workspace_members``, which lives outside the workspace and is
  otherwise superadmin-only (``GET /workspaces/{slug}/members``). A workspace
  admin who could request it would be reading the control plane through a side
  door.
* ``POST /workspace/import/from-path`` names a path on the *server's*
  filesystem. In single mode ``require_superadmin`` is the workspace admin, so
  the operator loses nothing; in multi mode it stops one tenant's admin from
  pointing the server at another tenant's directory.

**Import is admin, and blocked by --lock-pipelines.** An import is a bulk write
that includes ``pipelines/*.py``, and ``transforms/api.py`` ``exec``s every
``.py`` in that directory, unsandboxed, on every build. That makes import a
code-delivery channel wearing a data-movement costume. A server started with
``--lock-pipelines`` has declared that pipeline files may only be edited on
disk; if import ignored that, the flag would be bypassable by uploading a
tarball, so import is refused outright there. Everything that does land still
parks behind ``POST /workspace/import/acknowledge-pipelines``.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import IO, Annotated, Any, Iterator, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from laurelin.api.auth_routes import require_admin, require_superadmin
from laurelin.api.context import active_slug, is_multi
from laurelin.api.routes import (
    ActorDep,
    CatalogDep,
    StoreDep,
    WorkspaceDep,
    require_pipelines_unlocked,
)
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Role, User
from laurelin.export import (
    ExportOptions,
    ImportOptions,
    ImportRefused,
    ImportReport,
    acknowledge_pipelines,
    build_manifest,
    governance_fingerprint,
    import_state,
    import_workspace,
    pipelines_acknowledged,
    stream_export,
    target_is_pristine,
)
from laurelin.ontology import load_ontology

export_router = APIRouter(tags=["portability"])

AdminDep = Annotated[User, Depends(require_admin)]
SuperadminDep = Annotated[User, Depends(require_superadmin)]

# The report is a file rather than process state so it survives a restart and
# so `laurelin import --report` and the UI read the same document. 0600 because
# it names every principal the archive references.
REPORT_FILE = "import_report.json"

_STREAM_CHUNK = 1 << 20


# --------------------------------------------------------------------------- options


def _membership_rows(request: Request, slug: str) -> tuple[dict, ...]:
    control = request.app.state.control
    return tuple({"slug": slug, **row} for row in control.list_members(slug))


def _require_membership_authority(request: Request, user: User) -> None:
    """Membership is control-plane state, so asking for it is a control-plane act."""
    if not is_multi(request) or request.app.state.no_auth:
        return
    if not user.superadmin:
        raise HTTPException(
            status_code=403,
            detail="A user's role for this workspace lives in the control plane "
            "(control.db::workspace_members), which only a server administrator "
            "may read. Re-run with include_membership=false to take a "
            "governance-incomplete export, or ask a superadmin to run this one.",
        )


def _export_options(
    request: Request,
    user: User,
    *,
    actor: str,
    metadata_only: bool,
    include_audit: bool,
    include_membership: Optional[bool],
    allow_content_warnings: bool,
    gzip: Optional[bool],
    datasets: Optional[list[str]],
    allow_remote_data_plane: bool,
) -> ExportOptions:
    mode = "multi" if is_multi(request) else "single"
    slug: Optional[str] = active_slug(request) if mode == "multi" else None
    rows: tuple[dict, ...] = ()
    if mode == "multi" and include_membership:
        _require_membership_authority(request, user)
        rows = _membership_rows(request, slug or "")
    return ExportOptions(
        metadata_only=metadata_only,
        include_audit=include_audit,
        gzip=gzip,
        allow_content_warnings=allow_content_warnings,
        datasets=tuple(datasets) if datasets else None,
        created_by=actor,
        mode=mode,
        multi_slug=slug,
        include_membership=include_membership,
        membership_rows=rows,
        allow_remote_data_plane=allow_remote_data_plane,
    )


# --------------------------------------------------------------------------- principals


def _object_types(workspace: Workspace) -> dict[str, str]:
    try:
        ontology = load_ontology(workspace.ontology_dir)
    except ValueError:
        return {}
    return {o.api_name: o.backing_dataset for o in ontology.object_types}


def _resolve_principal(
    request: Request, store: MetadataStore, name: str
) -> Optional[User]:
    """A named principal as the *active workspace* would see them.

    In multi mode the effective role is the membership role, not the account
    role (``auth_routes.py:126``) — fingerprinting the account role would
    measure a decision the server never makes.
    """
    from laurelin.api.context import identity_auth

    if name.lower() in ("anonymous", "none", ""):
        return None
    user = identity_auth(request).get_user(name)
    if user is None:
        return None
    if not is_multi(request):
        return user
    if user.superadmin:
        return user.model_copy(update={"role": Role.admin})
    role = request.app.state.control.member_role(active_slug(request), user.username)
    if role is None:
        return None
    return user.model_copy(update={"role": role})


def _workspace_principals(
    request: Request, store: MetadataStore
) -> list[Optional[User]]:
    """Everyone whose answer this workspace can produce, plus the anonymous one.

    Anonymous is always included: "a principal who saw nothing still sees
    nothing" is half the round-trip claim, and it has no account to enumerate.
    """
    from laurelin.api.context import identity_auth

    if is_multi(request):
        slug = active_slug(request)
        names = [row["username"] for row in request.app.state.control.list_members(slug)]
    else:
        names = [u.username for u in identity_auth(request).list_users()]
    people = [_resolve_principal(request, store, n) for n in sorted(names)]
    return [p for p in people if p is not None] + [None]


# --------------------------------------------------------------------------- export


@export_router.get("/workspace/export/preview")
def preview_export(
    request: Request,
    workspace: WorkspaceDep,
    store: StoreDep,
    catalog: CatalogDep,
    user: AdminDep,
    actor: ActorDep,
    metadata_only: bool = Query(False),
    include_audit: bool = Query(True),
    include_membership: Optional[bool] = Query(None),
    allow_content_warnings: bool = Query(False),
    allow_remote_data_plane: bool = Query(False),
    fingerprint: bool = Query(False),
    dataset: Annotated[Optional[list[str]], Query()] = None,
) -> dict:
    """The manifest only — no members, no data.

    This is what the UI renders before anyone downloads a terabyte, and it is
    also how the secrets posture gets audited: the entire ``withheld`` list is
    here, at the cost of a few metadata queries instead of a full archive.
    """
    options = _export_options(
        request, user, actor=actor, metadata_only=metadata_only,
        include_audit=include_audit, include_membership=include_membership,
        allow_content_warnings=allow_content_warnings, gzip=None,
        datasets=dataset, allow_remote_data_plane=allow_remote_data_plane,
    )
    if fingerprint:
        options.governance_fingerprint = governance_fingerprint(
            store, catalog, _workspace_principals(request, store),
            object_types=_object_types(workspace),
        )
    manifest, parts = build_manifest(workspace, store, options)
    store.log_audit(
        "workspace_export_previewed",
        {"metadata_only": metadata_only, "datasets": len(manifest.datasets)},
        actor=actor,
    )
    return manifest.model_dump(mode="json") | {
        "estimated_parts": len(parts),
        "estimated_part_bytes": sum(parts.values()),
    }


def _stream_archive(
    workspace: Workspace,
    store: MetadataStore,
    options: ExportOptions,
) -> Iterator[bytes]:
    """Drive ``stream_export`` through an OS pipe so nothing is buffered.

    ``stream_export`` writes to a file object; a ``StreamingResponse`` wants an
    iterator. A pipe plus one thread is the only adapter that keeps the memory
    bound the writer worked for — collecting the writes into a list to yield
    them later would buffer the whole workspace, which is the exact failure the
    streaming design exists to avoid.

    A failure after the first byte cannot change the status code, so it closes
    the pipe and the client gets a truncated archive. That is detectable rather
    than silent: ``TRAILER.json`` is the last member, and an import refuses an
    archive that has none.
    """
    read_fd, write_fd = os.pipe()
    failure: list[BaseException] = []

    def produce() -> None:
        try:
            out = os.fdopen(write_fd, "wb")
        except BaseException as exc:  # noqa: BLE001 - the reader must not hang
            failure.append(exc)
            os.close(write_fd)
            return
        try:
            stream_export(workspace, store, out, options)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the reader side
            failure.append(exc)
        finally:
            try:
                out.close()
            except BaseException:  # noqa: BLE001 - a broken pipe here is the client leaving
                pass

    thread = threading.Thread(target=produce, name="laurelin-export", daemon=True)
    thread.start()
    try:
        with os.fdopen(read_fd, "rb") as source:
            while chunk := source.read(_STREAM_CHUNK):
                yield chunk
    finally:
        thread.join(timeout=60)
    if failure:
        raise failure[0]


@export_router.get("/workspace/export")
def download_export(
    request: Request,
    workspace: WorkspaceDep,
    store: StoreDep,
    catalog: CatalogDep,
    user: AdminDep,
    actor: ActorDep,
    metadata_only: bool = Query(False),
    include_audit: bool = Query(True),
    include_membership: Optional[bool] = Query(None),
    allow_content_warnings: bool = Query(False),
    allow_remote_data_plane: bool = Query(False),
    gzip: Optional[bool] = Query(None),
    fingerprint: bool = Query(False),
    dataset: Annotated[Optional[list[str]], Query()] = None,
) -> StreamingResponse:
    """The archive, streamed. Never buffered, never spooled to a whole copy."""
    options = _export_options(
        request, user, actor=actor, metadata_only=metadata_only,
        include_audit=include_audit, include_membership=include_membership,
        allow_content_warnings=allow_content_warnings, gzip=gzip,
        datasets=dataset, allow_remote_data_plane=allow_remote_data_plane,
    )
    if fingerprint:
        options.governance_fingerprint = governance_fingerprint(
            store, catalog, _workspace_principals(request, store),
            object_types=_object_types(workspace),
        )
    # Built once up front so every refusal — a remote data plane, a pipeline
    # that looks like it holds a credential, multi mode with no membership
    # decision — is a 409 with a message, instead of a 200 that truncates. The
    # writer builds it again; that costs a handful of metadata queries and buys
    # a status code the UI can act on.
    manifest, parts = build_manifest(workspace, store, options)

    suffix = ".tar.gz" if options.compress else ".tar"
    stamp = manifest.created_at.replace(":", "").replace("-", "")[:15]
    filename = f"laurelin-export-{manifest.origin.origin_slug}-{stamp}{suffix}"
    store.log_audit(
        "workspace_exported",
        {
            "metadata_only": metadata_only,
            "audit": include_audit,
            "membership": manifest.scope.membership,
            "datasets": len(manifest.datasets),
            "parts": len(parts),
            "bytes": sum(parts.values()),
            "withheld": len(manifest.withheld),
        },
        actor=actor,
    )
    return StreamingResponse(
        _stream_archive(workspace, store, options),
        media_type="application/gzip" if options.compress else "application/x-tar",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Enough of the manifest to show a progress estimate without
            # parsing the stream twice.
            "X-Laurelin-Export-Parts": str(len(parts)),
            "X-Laurelin-Export-Bytes": str(sum(parts.values())),
        },
    )


# --------------------------------------------------------------------------- import


def _refuse_if_pipelines_locked(request: Request) -> None:
    if request.app.state.lock_pipelines:
        raise HTTPException(
            status_code=403,
            detail="This server runs with --lock-pipelines, so pipeline files "
            "may only be edited on disk. An archive carries pipelines/*.py and "
            "those are exec'd unsandboxed on every build, so importing one over "
            "HTTP would make the flag bypassable. Import with the `laurelin "
            "import` CLI on the server instead.",
        )


def _report_path(workspace: Workspace) -> Path:
    return workspace.root / REPORT_FILE


def _save_report(workspace: Workspace, report: ImportReport) -> None:
    path = _report_path(workspace)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(report.model_dump(mode="json"), fh, indent=2)
    os.replace(tmp, path)


def _explain_refusal(workspace: Workspace, store: MetadataStore, exc: Exception) -> str:
    """Add the bit of context only the HTTP seam has.

    The pristine check counts ``users``, and over HTTP the target can never
    have none — the caller had to authenticate to reach this route, so there is
    always at least the admin doing the import. That makes a plain POST into a
    freshly-``init``ed workspace refuse, which reads as a bug until you know
    why. It is not one: an imported ``user:root view`` grant really would bind
    to the destination's ``root``, and that is a widening nobody asked for. So
    the refusal stands and the message explains the two-phase flow instead. The
    CLI, which needs no login, is unaffected.
    """
    pristine, counts = target_is_pristine(store, workspace)
    if pristine or set(counts) != {"users"}:
        return str(exc)
    return (
        f"{exc} Over HTTP the target is never empty of users: you authenticated "
        f"to reach this route, so the {counts['users']} account(s) here are the "
        "only thing blocking it. That still matters — an imported grant naming "
        "one of those usernames would bind to it — so review the collision "
        "report first: POST this archive again with dry_run=true&merge=true, "
        "then apply it with merge=true&confirm=<report_sha256>."
    )


def _run_import(
    workspace: Workspace,
    store: MetadataStore,
    archive: Path | str | IO[bytes],
    options: ImportOptions,
) -> dict:
    try:
        report = import_workspace(archive, workspace, store, options)
    except ImportRefused as exc:
        raise HTTPException(
            status_code=409, detail=_explain_refusal(workspace, store, exc)
        ) from None
    _save_report(workspace, report)
    return report.model_dump(mode="json")


@export_router.post("/workspace/import")
def import_upload(
    request: Request,
    workspace: WorkspaceDep,
    store: StoreDep,
    user: AdminDep,
    actor: ActorDep,
    file: UploadFile = File(...),
    dry_run: bool = Query(False),
    merge: bool = Query(False),
    confirm: Optional[str] = Query(None),
    rename_prefix: Optional[str] = Query(None),
    metadata_only: bool = Query(False),
) -> dict:
    """Reconstruct a workspace from an uploaded archive.

    The upload is handed to the reader as a *stream*. Starlette has already
    spooled it to a temp file (mode 0600 — ``tempfile`` opens with ``O_EXCL``
    and that mode), so copying it again into the workspace would double the
    disk cost of a terabyte archive without removing the copy that already
    exists. Set ``TMPDIR`` to a directory on the workspace's volume if the spool
    location matters to you; the CLI, which reads the file in place, avoids the
    question entirely.
    """
    _refuse_if_pipelines_locked(request)
    options = ImportOptions(
        dry_run=dry_run, merge=merge, confirm=confirm,
        rename_prefix=rename_prefix, metadata_only=metadata_only, actor=actor,
    )
    file.file.seek(0)
    return _run_import(workspace, store, file.file, options)


class ImportPathRequest(BaseModel):
    path: str


@export_router.post("/workspace/import/from-path")
def import_from_path(
    body: ImportPathRequest,
    request: Request,
    workspace: WorkspaceDep,
    store: StoreDep,
    user: SuperadminDep,
    actor: ActorDep,
    dry_run: bool = Query(False),
    merge: bool = Query(False),
    confirm: Optional[str] = Query(None),
    rename_prefix: Optional[str] = Query(None),
    metadata_only: bool = Query(False),
) -> dict:
    """Import an archive already on the server's filesystem.

    Superadmin, not workspace admin: the caller names a path on the *host*, and
    in multi mode a workspace admin is a tenant. In single mode superadmin is
    the workspace admin, so the operator loses nothing.
    """
    _refuse_if_pipelines_locked(request)
    path = Path(body.path).expanduser()
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"No such archive: {body.path}")
    options = ImportOptions(
        dry_run=dry_run, merge=merge, confirm=confirm,
        rename_prefix=rename_prefix, metadata_only=metadata_only, actor=actor,
    )
    return _run_import(workspace, store, path, options)


@export_router.get("/workspace/import/report")
def get_import_report(workspace: WorkspaceDep, user: AdminDep) -> dict:
    path = _report_path(workspace)
    if not path.exists():
        raise HTTPException(
            status_code=404, detail="No import has been run in this workspace."
        )
    return json.loads(path.read_text())


@export_router.get("/workspace/import/state")
def get_import_state(workspace: WorkspaceDep, user: AdminDep) -> dict:
    """Whether imported pipelines are still parked, and what the scan flagged.

    Separate from the report because the UI needs a banner on every page, and
    the report is a large document that only the Portability page reads.
    """
    state = import_state(workspace) or {}
    return {
        "imported": bool(state),
        "import_state": state.get("import_state"),
        "pipelines_acknowledged": pipelines_acknowledged(workspace),
        "origin_id": state.get("origin_id"),
        "imported_at": state.get("imported_at"),
        "content_warnings": state.get("content_warnings", []),
    }


@export_router.post(
    "/workspace/import/acknowledge-pipelines",
    dependencies=[Depends(require_pipelines_unlocked)],
)
def acknowledge_imported_pipelines(
    workspace: WorkspaceDep, store: StoreDep, user: AdminDep, actor: ActorDep
) -> dict:
    """Admit that an admin has read the imported pipelines.

    Never inferred from a successful import: the files are ``exec``'d on every
    build, so somebody has to say they looked.
    """
    if pipelines_acknowledged(workspace):
        return {"pipelines_acknowledged": True, "already": True}
    acknowledge_pipelines(workspace, store, actor=actor)
    return {"pipelines_acknowledged": True, "already": False}


# --------------------------------------------------------------------------- proof


class FingerprintRequest(BaseModel):
    principals: list[str] = Field(default_factory=list)
    datasets: Optional[list[str]] = None


@export_router.post("/workspace/governance/fingerprint")
def compute_fingerprint(
    body: FingerprintRequest,
    request: Request,
    workspace: WorkspaceDep,
    store: StoreDep,
    catalog: CatalogDep,
    user: AdminDep,
    actor: ActorDep,
) -> dict:
    """Recompute the decision matrix, so the round trip is provable by an
    operator and not only by the test suite.

    A principal that does not resolve is reported rather than skipped: after an
    inert import *no* principal resolves, and that absence is the single most
    important thing the operator has to see.
    """
    unresolved: list[str] = []
    principals: list[Optional[User]] = []
    if body.principals:
        for name in body.principals:
            resolved = _resolve_principal(request, store, name)
            if resolved is None and name.lower() not in ("anonymous", "none", ""):
                unresolved.append(name)
                continue
            principals.append(resolved)
    else:
        principals = _workspace_principals(request, store)

    matrix: dict[str, Any] = governance_fingerprint(
        store, catalog, principals, datasets=body.datasets,
        object_types=_object_types(workspace),
    )
    store.log_audit(
        "governance_fingerprint_computed",
        {"principals": len(principals), "unresolved": len(unresolved)},
        actor=actor,
    )
    return {"fingerprint": matrix, "unresolved_principals": unresolved}
