"""Write a workspace to a tar stream.

**Why tar, not zip.** A zip's directory lives at the end, so a reader must
seek — which rules out ``laurelin export - | ssh host laurelin import -``. Tar
streams in both directions, is stdlib, and ``tar tvf`` / ``tar xOf`` inspect it
on a machine that has never heard of Laurelin. That inspectability is not a
nicety: an opaque archive would make the anti-lock-in claim self-refuting.

**Why a manifest at the head and a trailer at the tail.** The manifest must be
readable before a terabyte of Parquet, so ``--dry-run`` can print the
withheld-secrets report without buffering. Per-member digests cannot be known
until after the members are written. Splitting is the only honest resolution:
``manifest.json`` declares intent, ``TRAILER.json`` declares what landed.

**Why not copy metadata.db.** Measured: 34 SQLite tables at runtime — the 27
declared plus ``object_search``, five FTS5 shadows and ``sqlite_sequence`` —
none of which exist on Postgres, which indexes with pg_trgm instead. And WAL
mode means ``cp`` on a running server silently loses committed rows sitting in
``metadata.db-wal``. Row-level JSONL through the backend is the only
dialect-neutral option.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Iterator, Optional

from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.storage import Storage, storage_for
from laurelin.export.manifest import (
    FORMAT_VERSION,
    MANIFEST_MEMBER,
    MEMBERSHIP_COLUMNS,
    MEMBERSHIP_TABLE,
    TABLE_POLICY,
    TRAILER_MEMBER,
    DatasetPlan,
    DataState,
    ExportManifest,
    ExportOptions,
    ExportRefused,
    ExportTrailer,
    MemberDigest,
    Origin,
    PipelineWarning,
    PrincipalRef,
    Scope,
    TableStat,
    Withheld,
    exported_tables,
    not_exported_entries,
    origin_id,
    origin_slug,
)
from laurelin.export.pipeline_scan import (
    scan_columns,
    scan_ontology,
    scan_pipelines,
)
from laurelin.export.secrets import strip_secrets, withheld_field
from laurelin.transforms.flow_files import PIPELINE_FILE_SUFFIXES

# 1 MiB. The only buffer a multi-terabyte export is allowed to hold.
COPY_CHUNK = 1 << 20

# JSONL is generated into a spool that stays in memory up to this size and
# spills to disk beyond it, because a tar member's size must be in its header
# before its bytes. 8 MiB covers every table but audit_log on a real workspace.
SPOOL_MAX = 8 << 20

# Rows fetched per page. The point is a bounded working set, not throughput.
PAGE = 10_000

# A deterministic read order per table, so two exports of an unchanged
# workspace produce the same bytes. audit_log orders by id — the column that is
# deliberately *not* carried — because source order is what makes the
# destination's reassigned identities land in the same sequence.
_ORDER_BY: dict[str, str] = {
    "markings": "name",
    "groups": "name",
    "users": "username",
    "datasets": "name",
    "dataset_versions": "dataset, version",
    "lineage_edges": "upstream_dataset, downstream_dataset, transform_name",
    "dataset_policies": "dataset",
    "dataset_grants": "dataset, id",
    "ontology_grants": "object_type, id",
    "dataset_markings": "dataset, marking",
    "group_members": "group_name, username",
    "clearances": "username, marking",
    "object_edits": "edit_seq, id",
    "dashboards": "name",
    "object_apps": "name",
    "schedules": "name",
    "sources": "name",
    "engines": "name",
    "audit_log": "id",
    "api_tokens": "created_at, id",
}

# Which column names a row in the withheld report. "*" where a table has no
# single-column key worth naming.
_ROW_KEY: dict[str, str] = {
    "datasets": "name", "sources": "name", "engines": "name",
    "users": "username", "api_tokens": "id",
}


def _member(name: str, size: int, mtime: int) -> tarfile.TarInfo:
    """A tar header that leaks nothing about who built the archive.

    uid/gid/uname/gname are zeroed because otherwise the archive records the
    exporting operator, and extracted as root it would restore their ownership.
    Mode 0600 for every member, matching the archive itself.
    """
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = mtime
    info.mode = 0o600
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.type = tarfile.REGTYPE
    return info


class _Digesting:
    """Counts and hashes bytes on their way into the tar, for the trailer."""

    def __init__(self) -> None:
        self.members: dict[str, MemberDigest] = {}
        self.total = 0

    def record(self, name: str, digest: str, size: int) -> None:
        self.members[name] = MemberDigest(sha256=digest, bytes=size)
        self.total += size

    def trailer(self) -> ExportTrailer:
        return ExportTrailer(
            members=dict(self.members),
            member_count=len(self.members),
            total_bytes=self.total,
        )


def _sha256_of(fh: IO[bytes]) -> str:
    import hashlib

    digest = hashlib.sha256()
    fh.seek(0)
    while True:
        chunk = fh.read(COPY_CHUNK)
        if not chunk:
            break
        digest.update(chunk)
    fh.seek(0)
    return digest.hexdigest()


# --------------------------------------------------------------------------- reading

def _count(store: MetadataStore, table: str, spec) -> int:
    where, params = _where(spec)
    with store._conn() as c:
        row = c.execute(f"SELECT count(*) AS n FROM {table}{where}", params).fetchone()
    return int(row["n"]) if row else 0


def _where(spec) -> tuple[str, list]:
    if spec.row_filter is None:
        return "", []
    column, value = spec.row_filter
    return f" WHERE {column} = ?", [value]


def _iter_rows(store: MetadataStore, table: str, spec) -> Iterator[dict]:
    """Page a table out in a bounded working set.

    LIMIT/OFFSET rather than a server-side cursor because it is the one form
    both dialects spell identically, and the read order is fixed by _ORDER_BY
    so the pages cannot interleave differently between them.
    """
    columns = ", ".join(spec.columns)
    order = _ORDER_BY.get(table) or ", ".join(spec.columns)
    where, base_params = _where(spec)
    offset = 0
    while True:
        sql = (
            f"SELECT {columns} FROM {table}{where} ORDER BY {order} "
            f"LIMIT {PAGE} OFFSET {offset}"
        )
        with store._conn() as c:
            page = c.execute(sql, base_params).fetchall()
        if not page:
            return
        yield from page
        if len(page) < PAGE:
            return
        offset += PAGE


def _clean_row(table: str, spec, row: dict) -> tuple[dict, list[Withheld]]:
    """Apply the column allowlist and the secret posture to one row."""
    key_col = _ROW_KEY.get(table)
    row_key = str(row.get(key_col, "*")) if key_col else "*"
    out: dict[str, Any] = {}
    withheld: list[Withheld] = []
    for column in spec.columns:
        value = row.get(column)
        if column in spec.error_columns:
            value = None
        value, hits = strip_secrets(table, row_key, column, value)
        withheld.extend(hits)
        out[column] = value
    return out, withheld


# --------------------------------------------------------------------------- planning

def _dataset_plans(
    workspace: Workspace,
    store: MetadataStore,
    storage: Storage,
    options: ExportOptions,
) -> tuple[list[DatasetPlan], dict[str, int]]:
    """Per-dataset data state, and the deduplicated set of parts to carry.

    The part set is the **transitive union** over every version of every
    managed dataset. An append's manifest references parts written for earlier
    versions, so a per-version walk would miss them — and a directory listing
    would sweep every version's parts into one version, which is why
    ``Storage.list_keys`` is deliberately non-recursive.
    """
    wanted = set(options.datasets) if options.datasets else None
    plans: list[DatasetPlan] = []
    parts: dict[str, int] = {}

    for info in sorted(store.list_datasets(), key=lambda d: d.name):
        versions = store.list_versions(info.name)
        if info.kind == "managed":
            if options.metadata_only or (wanted is not None and info.name not in wanted):
                plans.append(DatasetPlan(
                    name=info.name, kind=info.kind,
                    data_state=DataState.metadata_only.value,
                    versions=len(versions),
                    reason=("--metadata-only omits data/**" if options.metadata_only
                            else "not named by --dataset; its governance still travels"),
                ))
                continue
            keys: set[str] = set()
            for version in versions:
                files = list(version.files)
                if not files:
                    # Pre-manifest layout: everything under the version dir.
                    files = [
                        k for k in storage.list_keys(version.path) if k.endswith(".parquet")
                    ]
                keys.update(files)
            total = 0
            for key in sorted(keys):
                size = storage.size(key)
                if size < 0:
                    raise ExportRefused(
                        f"Dataset {info.name!r} references a part that is not in "
                        f"storage: {key}. Exporting the manifest without it would "
                        "reconstruct a version nobody can read. Repair or drop the "
                        "version before exporting."
                    )
                parts[key] = size
                total += size
            plans.append(DatasetPlan(
                name=info.name, kind=info.kind, data_state=DataState.included.value,
                versions=len(versions), parts=len(keys), bytes=total,
                note="unmasked pre-policy rows",
            ))
        elif info.kind == "iceberg":
            plans.append(DatasetPlan(
                name=info.name, kind=info.kind,
                data_state=DataState.elsewhere_absolute_path.value,
                versions=len(versions),
                reason=(
                    "Iceberg-backed. warehouse_uri() and source.path are absolute "
                    "file:// paths and the catalog is a separate database at "
                    "<root>/iceberg-catalog.db, neither of which is in this "
                    "archive. Governance and version history travel; the table "
                    "will not resolve until it is re-registered against a "
                    "reachable warehouse."
                ),
            ))
        else:
            plans.append(DatasetPlan(
                name=info.name, kind=info.kind, data_state=DataState.elsewhere.value,
                versions=len(versions),
                reason=(
                    f"{info.kind} datasets are pointers; the rows live in the "
                    "remote system and no export can carry them"
                ),
            ))
    return plans, parts


def _principals(store: MetadataStore) -> list[PrincipalRef]:
    """Every principal the imported rules name.

    Username is the only join key anywhere in governance — group_members,
    clearances, Grant.subject and RowRule.subject are all lowercased name
    strings — so this list *is* the rebind checklist. Nothing here is bound
    automatically; see reader.py for why auto-remap is the silent-widening bug.
    """
    refs: dict[tuple[str, str], set[str]] = {}

    def note(kind: str, name: str, source: str) -> None:
        if not name:
            return
        refs.setdefault((kind, name.strip().lower()), set()).add(source)

    for row in store.list_dataset_grants():
        kind = row.get("subject_kind")
        if kind in ("user", "group"):
            note(kind, row.get("subject", ""), f"dataset_grants:{row.get('dataset')}")
    for row in store.list_grants():
        kind = row.get("subject_kind")
        if kind in ("user", "group"):
            note(kind, row.get("subject", ""), f"ontology_grants:{row.get('object_type')}")
    with store._conn() as c:
        for row in c.execute("SELECT group_name, username FROM group_members").fetchall():
            note("user", row["username"], f"group_members:{row['group_name']}")
            note("group", row["group_name"], "group_members")
        for row in c.execute("SELECT username FROM clearances").fetchall():
            note("user", row["username"], "clearances")
        for row in c.execute("SELECT dataset, policy_json FROM dataset_policies").fetchall():
            _policy_principals(row["dataset"], row["policy_json"], note)
    return [
        PrincipalRef(kind=kind, name=name, referenced_by=sorted(sources))
        for (kind, name), sources in sorted(refs.items())
    ]


def _policy_principals(dataset: str, policy_json: str, note) -> None:
    try:
        policy = json.loads(policy_json or "{}")
    except ValueError:
        return
    row_policy = policy.get("row_policy") or {}
    for rule in row_policy.get("rules") or []:
        if rule.get("subject_kind") in ("user", "group"):
            note(rule["subject_kind"], rule.get("subject", ""),
                 f"dataset_policies:{dataset}:row_policy")
    for mask in policy.get("column_masks") or []:
        for exempt in mask.get("exempt") or []:
            if exempt.get("subject_kind") in ("user", "group"):
                note(exempt["subject_kind"], exempt.get("subject", ""),
                     f"dataset_policies:{dataset}:mask:{mask.get('column')}")


def _content_warnings(workspace: Workspace, store: MetadataStore,
                      tables: list[str]) -> list[PipelineWarning]:
    """Every credential-shaped line in everything that travels near-verbatim.

    Three sources, one mechanism, because the reason none of them is stripped
    is the same in all three cases: stripping would destroy the meaning.
    Measured, when this only walked ``pipelines/``, the export shipped a DSN in
    ``ontology/core.yml``, a password inside a dashboard panel's SQL, a webhook
    token in an object app's filters and an ``s3://user:pass@`` schedule target
    — with ``pipeline_warnings: []`` in the manifest beside them.

    Columns are scanned **as they will travel**, i.e. after the secret posture
    has run, so a key the allowlist already nulled does not raise a flag the
    operator cannot act on.
    """
    warnings = scan_pipelines(workspace.pipelines_dir)
    warnings += scan_ontology(workspace.ontology_dir)
    for table in tables:
        spec = TABLE_POLICY[table]
        if not spec.scan_columns:
            continue
        key_col = _ROW_KEY.get(table, "name")
        for row in _iter_rows(store, table, spec):
            cleaned, _ = _clean_row(table, spec, row)
            warnings += scan_columns(
                table, str(row.get(key_col, "*")),
                [(c, cleaned.get(c)) for c in spec.scan_columns],
            )
    return warnings


def _withheld_prepass(
    store: MetadataStore, tables: list[str]
) -> tuple[list[Withheld], dict[str, int]]:
    """Enumerate withheld secrets before a single byte is written.

    Positive enumeration is the point: the manifest is at the head of the
    stream so an operator can read the whole checklist without unpacking a
    terabyte, which means it cannot be accumulated while streaming. The
    secret-bearing tables are all small (tens of rows), so a pre-pass is cheap
    and re-applying the same deterministic function during the stream yields
    the identical result.
    """
    withheld: list[Withheld] = []
    nulled: dict[str, int] = {}
    for table in tables:
        spec = TABLE_POLICY[table]
        if not (spec.secret_columns or spec.error_columns):
            continue
        for row in _iter_rows(store, table, spec):
            withheld.extend(_clean_row(table, spec, row)[1])
        # Columns dropped for every row: named once, with "*" as the row.
        for column in spec.secret_columns:
            if column in spec.drop_columns:
                withheld.append(withheld_field(table, "*", column))
        # Error text is counted with its own query rather than while iterating,
        # because most error columns are not in the allowlist at all — the row
        # loop would never see one, and the manifest would report zero for a
        # field that was genuinely dropped.
        for column in spec.error_columns:
            with store._conn() as c:
                row = c.execute(
                    f"SELECT count(*) AS n FROM {table} "
                    f"WHERE {column} IS NOT NULL AND {column} <> ''"
                ).fetchone()
            count = int(row["n"]) if row else 0
            if count:
                nulled[f"{table}.{column}"] = count
    return withheld, nulled


def _resolve_membership(options: ExportOptions) -> tuple[bool, tuple[dict, ...]]:
    """Multi-workspace mode makes the export refuse rather than guess.

    In multi mode the *effective* role comes from ``control.db::
    workspace_members`` (auth_routes.py:126) — the single largest decision
    input, since admin bypasses every gate — and it is not one of the workspace
    tables. Refusing entirely was rejected: it makes the feature unusable in
    the deployment mode most likely to need it.
    """
    if options.mode != "multi":
        return False, ()
    if options.include_membership is None:
        raise ExportRefused(
            "This workspace runs in multi-workspace mode. A user's role for this "
            "workspace lives in control.db::workspace_members, outside the "
            "workspace. Without it the export cannot reproduce a single access "
            "decision. Re-run with --include-membership to emit "
            f"tables/{MEMBERSHIP_TABLE}.jsonl scoped to slug "
            f"{options.multi_slug!r}, or accept a governance-incomplete export "
            "with --no-membership."
        )
    if not options.include_membership:
        return False, ()
    return True, tuple(options.membership_rows)


def build_manifest(
    workspace: Workspace,
    store: MetadataStore,
    options: Optional[ExportOptions] = None,
    storage: Optional[Storage] = None,
) -> tuple[ExportManifest, dict[str, int]]:
    """The manifest, and the part set it implies. No members are written."""
    options = options or ExportOptions()
    storage = storage or storage_for(workspace)

    if storage.is_remote and not options.metadata_only and not options.allow_remote_data_plane:
        # UNVERIFIED: no object store was available to measure this path
        # end-to-end, so it is opt-in rather than silently attempted. Refusing
        # is the loud failure; the alternative is an archive that looks fine
        # and is missing its data.
        raise ExportRefused(
            f"The data plane is an object store ({storage.uri}). Streaming parts "
            "out of it has not been verified end-to-end, so a full export from "
            "it is opt-in: pass allow_remote_data_plane=True to attempt it, or "
            "use --metadata-only, which reads no parts at all."
        )

    include_membership, membership_rows = _resolve_membership(options)

    tables = exported_tables(include_audit=options.include_audit)
    warnings = _content_warnings(workspace, store, tables)
    if warnings and not options.allow_content_warnings:
        first = warnings[0]
        where = first.file if not first.line else f"{first.file}:{first.line}"
        raise ExportRefused(
            f"{len(warnings)} place(s) in this workspace's authored content look "
            f"like they hold a credential, starting at {where} ({first.pattern}). "
            "These members travel near-verbatim — pipelines/*.py is exec'd "
            "unsandboxed on every build, and a dashboard panel's SQL is run as "
            "written — so the export will not strip them behind your back. Fix "
            "them, or re-run with --allow-content-warnings to carry them as "
            "they are."
        )

    plans, parts = _dataset_plans(workspace, store, storage, options)
    withheld, nulled = _withheld_prepass(store, tables)

    stats = {
        table: TableStat(
            rows=_count(store, table, TABLE_POLICY[table]),
            columns=list(TABLE_POLICY[table].columns),
        )
        for table in tables
    }
    if include_membership:
        stats[MEMBERSHIP_TABLE] = TableStat(
            rows=len(membership_rows), columns=list(MEMBERSHIP_COLUMNS)
        )

    directory = workspace.root.name
    manifest = ExportManifest(
        format_version=FORMAT_VERSION,
        laurelin_version=_laurelin_version(),
        created_by=options.created_by,
        origin=Origin(
            workspace_name=workspace.name,
            workspace_dir=directory,
            origin_id=origin_id(workspace.name, directory),
            origin_slug=origin_slug(directory),
            metadata_dialect=store.dialect,
            mode=options.mode,
            multi_slug=options.multi_slug,
            data_plane="remote" if storage.is_remote else "local",
        ),
        scope=Scope(
            data=not options.metadata_only,
            audit=options.include_audit,
            membership=include_membership,
            datasets=list(options.datasets) if options.datasets else None,
        ),
        tables=stats,
        datasets=plans,
        withheld=withheld,
        not_exported=not_exported_entries(),
        principals=_principals(store),
        content_warnings=warnings,
        nulled_error_fields=nulled,
        governance_fingerprint=dict(options.governance_fingerprint),
    )
    return manifest, parts


def preview_manifest(
    workspace: Workspace,
    store: MetadataStore,
    options: Optional[ExportOptions] = None,
    storage: Optional[Storage] = None,
) -> ExportManifest:
    """What a full export *would* carry, without writing one.

    This is how the withheld-secrets posture gets audited: the entire list is
    in the manifest, and producing it costs a few hundred milliseconds instead
    of a terabyte.
    """
    return build_manifest(workspace, store, options, storage)[0]


def _laurelin_version() -> str:
    try:
        from importlib.metadata import version

        return version("laurelin")
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------- writing

def stream_export(
    workspace: Workspace,
    store: MetadataStore,
    out: IO[bytes],
    options: Optional[ExportOptions] = None,
    storage: Optional[Storage] = None,
) -> ExportManifest:
    """Write the archive to ``out``, which is never seeked.

    ``out`` may be a socket, a pipe or an HTTP response body. Stream mode
    (``w|``) is what makes ``laurelin export - | ssh host laurelin import -``
    work, and it is also what forces every member's size to be known before its
    bytes — hence the spool below.
    """
    options = options or ExportOptions()
    storage = storage or storage_for(workspace)
    manifest, parts = build_manifest(workspace, store, options, storage)
    include_membership, membership_rows = _resolve_membership(options)

    mtime = int(datetime.fromisoformat(
        manifest.created_at.replace("Z", "+00:00")
    ).replace(tzinfo=timezone.utc).timestamp()) if manifest.created_at else 0
    digests = _Digesting()
    spool_dir = options.spool_dir or str(workspace.root)

    mode = "w|gz" if options.compress else "w|"
    tar = tarfile.open(fileobj=out, mode=mode, format=tarfile.PAX_FORMAT)
    try:
        _add_bytes(tar, digests, MANIFEST_MEMBER,
                   manifest.model_dump_json(indent=2).encode() + b"\n", mtime)
        _add_bytes(tar, digests, "laurelin.yml", _reemitted_marker(workspace), mtime)

        for path in _sorted_files(workspace.ontology_dir, (".yml", ".yaml")):
            _add_path(tar, digests, f"ontology/{path.name}", path, mtime)
        for path in _sorted_files(workspace.pipelines_dir, PIPELINE_FILE_SUFFIXES):
            _add_path(tar, digests, f"pipelines/{path.name}", path, mtime)

        for table in exported_tables(include_audit=options.include_audit):
            _add_table(tar, digests, store, table, spool_dir, mtime)
        if include_membership:
            _add_membership(tar, digests, membership_rows, spool_dir, mtime)

        for key in sorted(parts):
            _add_part(tar, digests, storage, key, parts[key], mtime)

        _add_bytes(tar, digests, TRAILER_MEMBER,
                   digests.trailer().model_dump_json(indent=2).encode() + b"\n", mtime)
    finally:
        tar.close()
    return manifest


def _sorted_files(directory: Path, suffixes: tuple[str, ...]) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in suffixes
    )


def _reemitted_marker(workspace: Workspace) -> bytes:
    """laurelin.yml is re-emitted, not copied.

    config.py yaml.safe_loads the whole file and reads only {name, description}.
    A verbatim copy would carry whatever else an operator left in it —
    unexamined, unclassified, and possibly a secret.
    """
    import yaml

    return yaml.safe_dump(
        {"name": workspace.name, "description": workspace.description},
        sort_keys=False,
    ).encode()


def _add_bytes(tar, digests, name: str, payload: bytes, mtime: int) -> None:
    import hashlib

    tar.addfile(_member(name, len(payload), mtime), io.BytesIO(payload))
    digests.record(name, hashlib.sha256(payload).hexdigest(), len(payload))


def _add_path(tar, digests, name: str, path: Path, mtime: int) -> None:
    size = path.stat().st_size
    with path.open("rb") as fh:
        digest = _sha256_of(fh)
        tar.addfile(_member(name, size, mtime), fh)
    digests.record(name, digest, size)


def _add_table(tar, digests, store, table: str, spool_dir: str, mtime: int) -> None:
    """One JSONL member per table, generated into a spool.

    JSON columns travel as **strings, byte-faithful**, never as re-serialized
    objects: re-serializing reorders keys, and dataset_policies.policy_json
    byte-identity is what makes the round-trip proof a proof rather than a
    semantic argument.
    """
    spec = TABLE_POLICY[table]
    with tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX, dir=spool_dir) as spool:
        for row in _iter_rows(store, table, spec):
            cleaned, _ = _clean_row(table, spec, row)
            spool.write(json.dumps(cleaned, sort_keys=False).encode() + b"\n")
        size = spool.tell()
        digest = _sha256_of(spool)
        name = f"tables/{table}.jsonl"
        tar.addfile(_member(name, size, mtime), spool)
    digests.record(name, digest, size)


def _add_membership(tar, digests, rows, spool_dir: str, mtime: int) -> None:
    payload = b"".join(
        json.dumps({k: row.get(k) for k in MEMBERSHIP_COLUMNS}).encode() + b"\n"
        for row in rows
    )
    _add_bytes(tar, digests, f"tables/{MEMBERSHIP_TABLE}.jsonl", payload, mtime)


def _add_part(tar, digests, storage: Storage, key: str, size: int, mtime: int) -> None:
    """Copy one Parquet part through a 1 MiB buffer.

    Never ``read_table``: a terabyte dataset must cost one buffer, and the
    bytes in the archive should be the bytes on disk rather than a re-encoding
    of them.
    """
    import hashlib

    digest = hashlib.sha256()
    with storage.open_input_stream(key) as source:
        tar.addfile(_member(key, size, mtime), _HashingReader(source, digest))
    digests.record(key, digest.hexdigest(), size)


class _HashingReader(io.RawIOBase):
    """Hashes bytes as tarfile copies them, so nothing is read twice."""

    def __init__(self, source, digest):
        self._source = source
        self._digest = digest

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        chunk = self._source.read(COPY_CHUNK if size is None or size < 0 else size)
        if chunk:
            self._digest.update(chunk)
        return chunk


def export_workspace(
    workspace: Workspace,
    store: MetadataStore,
    destination: Path | str,
    options: Optional[ExportOptions] = None,
    storage: Optional[Storage] = None,
) -> ExportManifest:
    """Write the archive to a file, 0600, atomically.

    ``os.open(..., O_CREAT|O_EXCL|O_WRONLY, 0o600)`` rather than ``open()`` then
    ``chmod``: the latter leaves a 0644 window during which a terabyte of
    unmasked rows is world-readable. The temp file lives in the *destination*
    directory so the final step is a same-device rename, and O_EXCL also
    refuses to silently overwrite.
    """
    options = options or ExportOptions()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ExportRefused(
            f"{destination} already exists. An export is a snapshot; overwriting "
            "one silently is how the wrong archive gets shipped."
        )
    temp = destination.with_name(f".{destination.name}.partial")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(temp, flags, 0o600)
    except FileExistsError:
        os.unlink(temp)
        fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            manifest = stream_export(workspace, store, fh, options, storage)
        os.rename(temp, destination)
    except BaseException:
        # A partial archive that looks like a complete one is the failure this
        # whole feature exists to prevent, so it never gets the real name.
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return manifest


def read_manifest_bytes(raw: bytes) -> ExportManifest:
    return ExportManifest.model_validate_json(raw)


__all__ = [
    "COPY_CHUNK",
    "build_manifest",
    "export_workspace",
    "preview_manifest",
    "read_manifest_bytes",
    "stream_export",
]
