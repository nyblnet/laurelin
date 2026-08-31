"""Iceberg dataset routes.

An Iceberg-backed dataset is written and versioned by Laurelin and readable by
Spark, Trino, Snowflake and DuckDB without it. This exposes the pieces the
catalog already implements — create, snapshots, branches, schema evolution —
which until now had no REST surface at all, so the feature the CHANGELOG
advertises could only be driven from Python.

Editor-gated like any dataset write. Every route needs pyiceberg; when it isn't
installed they return a clear 501 rather than a confusing 500.
"""

from __future__ import annotations

from fastapi import File as FileParam
from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, Field

from laurelin.api.routes import (
    EDITOR,
    ActorDep,
    CatalogDep,
    PermDep,
    StoreDep,
    UserDep,
    _dump,
    _require_dataset_edit,
    _require_existing_dataset_view,
    _spooled_upload,
    router,
)
from laurelin.core import iceberg
from laurelin.core.failure import safe_detail


def _require_iceberg() -> None:
    if not iceberg.available():
        raise HTTPException(
            status_code=501,
            detail="Iceberg support is not installed on this server "
            "(pip install 'laurelin[iceberg]').",
        )


class BranchRequest(BaseModel):
    branch: str
    from_version: int | None = None


class SchemaEvolveRequest(BaseModel):
    add: dict[str, str] = Field(default_factory=dict)
    drop: list[str] = Field(default_factory=list)
    rename: dict[str, str] = Field(default_factory=dict)
    # Dropping or renaming breaks everything that names the column, so it needs
    # explicit intent — the UI sends this only after showing the impact.
    allow_breaking: bool = False


@router.post("/datasets/{name}/iceberg", dependencies=[EDITOR])
def create_iceberg_dataset(
    name: str,
    catalog: CatalogDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
    file: UploadFile = FileParam(...),
    mode: str = "replace",
) -> dict:
    """Create or add to an Iceberg dataset from an uploaded CSV/Parquet file."""
    _require_iceberg()
    _require_dataset_edit(perms, user, name)
    if mode not in ("replace", "append"):
        raise HTTPException(status_code=400, detail="mode must be replace or append")
    with _spooled_upload(file) as tmp_path:
        try:
            table = catalog.parse_upload(tmp_path)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=safe_detail(exc, subject=f"dataset:{name}")
            ) from None
        # NOT inside the try: `write_iceberg` goes into pyiceberg and pyarrow, and
        # what comes back is caught by `app._catch_all_detail`. That is where
        # this belongs — a catch here would have to guess the phase.
        info = catalog.write_iceberg(name, table, mode=mode)
    store.log_audit(
        "iceberg_written",
        {"dataset": name, "mode": mode, "version": info.version,
         "rows": info.row_count},
        actor=actor,
    )
    return _dump(info)


@router.get("/datasets/{name}/iceberg/snapshots")
def iceberg_snapshots(
    name: str, catalog: CatalogDep, store: StoreDep, perms: PermDep, user: UserDep
) -> list[dict]:
    _require_iceberg()
    _require_existing_dataset_view(store, perms, user, name)
    return catalog.iceberg_snapshots(name)


@router.get("/datasets/{name}/iceberg/storage")
def iceberg_storage(
    name: str, catalog: CatalogDep, store: StoreDep, perms: PermDep, user: UserDep
) -> dict:
    """What this table's snapshots cost, and which of them cannot be expired.

    The snapshot table above says a table has six snapshots; it never said what
    they cost or why they are all still there. Compaction reclaims scan cost,
    not disk, and nothing expires snapshots — both are documented limitations,
    and this is the number that makes them checkable rather than merely stated.
    """
    _require_iceberg()
    _require_existing_dataset_view(store, perms, user, name)
    return catalog.iceberg_storage_report(name)


@router.get("/datasets/{name}/iceberg/branches")
def list_iceberg_branches(
    name: str, catalog: CatalogDep, store: StoreDep, perms: PermDep, user: UserDep
) -> list[dict]:
    _require_iceberg()
    _require_existing_dataset_view(store, perms, user, name)
    return catalog.iceberg_branches(name)


@router.post("/datasets/{name}/iceberg/branches", dependencies=[EDITOR])
def create_iceberg_branch(
    name: str,
    body: BranchRequest,
    catalog: CatalogDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
) -> dict:
    _require_iceberg()
    _require_dataset_edit(perms, user, name)
    try:
        result = catalog.iceberg_branch(name, body.branch, from_version=body.from_version)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=safe_detail(exc, subject="dataset")) from None
    store.log_audit("iceberg_branch_created",
                    {"dataset": name, "branch": body.branch}, actor=actor)
    return result


@router.delete("/datasets/{name}/iceberg/branches/{branch}", dependencies=[EDITOR])
def delete_iceberg_branch(
    name: str,
    branch: str,
    catalog: CatalogDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
) -> dict:
    _require_iceberg()
    _require_dataset_edit(perms, user, name)
    try:
        catalog.delete_iceberg_branch(name, branch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=safe_detail(exc, subject="dataset")) from None
    store.log_audit("iceberg_branch_deleted",
                    {"dataset": name, "branch": branch}, actor=actor)
    return {"deleted": branch}


@router.post("/datasets/{name}/iceberg/branches/{branch}/merge", dependencies=[EDITOR])
def merge_iceberg_branch(
    name: str,
    branch: str,
    catalog: CatalogDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
) -> dict:
    _require_iceberg()
    _require_dataset_edit(perms, user, name)
    try:
        info = catalog.merge_iceberg_branch(name, branch)
    except (ValueError, KeyError) as exc:
        # A diverged branch is a 409: it's a conflict, not a malformed request.
        raise HTTPException(status_code=409, detail=safe_detail(exc, subject="dataset")) from None
    store.log_audit("iceberg_branch_merged",
                    {"dataset": name, "branch": branch, "version": info.version},
                    actor=actor)
    return _dump(info)


@router.get("/datasets/{name}/iceberg/schema/impact")
def iceberg_schema_impact(
    name: str, catalog: CatalogDep, store: StoreDep, perms: PermDep, user: UserDep
) -> dict:
    """What a breaking schema change on this dataset would affect.

    Surfaced before the change, not after: the question isn't "is dropping this
    column safe" — it's "what breaks when I do it", which this answers.

    Row-filtered by dataset visibility, like `GET /lineage`. `downstream_of` is
    the transitive closure over `store.list_lineage()` — the same edges #75
    projected — and this route handed the whole closure back after checking
    only the SUBJECT dataset. Measured: a viewer who saw four datasets read
    `["downstream_of_secret","joined_public","pay_summary","topsecret_payroll"]`,
    byte-identical to the admin's answer. That is the name AND the cardinality
    of the hidden topology, which is precisely what the placeholder option was
    rejected for. One unquantified boolean survives, as everywhere else.
    """
    _require_iceberg()
    _require_existing_dataset_view(store, perms, user, name)
    downstream = catalog.downstream_of(name)
    visible = perms.viewable_datasets(user, downstream)
    kept = [d for d in downstream if d in visible]
    return {
        "downstream": kept,
        "hidden_downstream": len(kept) != len(downstream),
    }


@router.post("/datasets/{name}/iceberg/schema", dependencies=[EDITOR])
def evolve_iceberg_schema(
    name: str,
    body: SchemaEvolveRequest,
    catalog: CatalogDep,
    store: StoreDep,
    perms: PermDep,
    user: UserDep,
    actor: ActorDep,
) -> dict:
    _require_iceberg()
    _require_dataset_edit(perms, user, name)
    try:
        columns = catalog.evolve_iceberg_schema(
            name, add=body.add or None, drop=body.drop or None,
            rename=body.rename or None, allow_breaking=body.allow_breaking,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=safe_detail(exc, subject="dataset")) from None
    store.log_audit(
        "iceberg_schema_evolved",
        {"dataset": name, "add": list(body.add), "drop": body.drop,
         "rename": body.rename},
        actor=actor,
    )
    return {"columns": columns}
