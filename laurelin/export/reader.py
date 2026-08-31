"""Reconstruct a workspace from an archive, without binding a single principal.

**The invariant, in one sentence.** Import writes every governance rule
verbatim and binds no principal, so for any principal that exists in the target
before the import, the target's post-import answer — ``(can_view, can_edit)``,
the visible row set and the unmasked cell set — is a *subset* of the source's
answer for the same-named principal, for every dataset in the archive. Import
may narrow. It may never widen. Widening requires a subsequent, explicit,
audited admin act.

**Why not refuse on an unknown principal.** The target case is a fresh empty
workspace where no principal exists. Refusing makes the feature unusable
exactly where it is needed, and operators reach for ``--force``.

**Why not auto-remap by username.** Username is the only join key anywhere in
governance, and ``provision_oidc_user`` is find-or-create by username — so a
destination IdP that mints ``finance-lead`` would inherit the imported
``finance-lead``'s clearances and memberships with nobody deciding it.
Measured: making the destination's ``analysts`` contain ``mal`` flipped ``mal``
from ``(False, False)`` to ``(True, False)`` on a dataset whose only gate was
its grants.

**Why grants import even when their subject does not resolve.** Measured:
emptying a dataset's grant list flips it from an allowlist to
readable-by-every-authenticated-viewer (``permissions.py:333``). Dropping an
unresolvable grant would therefore *widen*. Rows are written through the
backend rather than the routes precisely because ``validate_grants`` raises
"Unknown user in grant" and would tempt a skip.

**Atomicity, and the order it depends on.** Every metadata row lands in one
transaction on one connection, which is also where effective markings are
recomputed — a second connection could not see the uncommitted rows. The commit
is the **last** thing that happens: parts are written, the pipeline gate is
armed, workspace files are moved, and only then does the transaction close. A
failure at any point before that rolls the rows back, deletes the parts it
wrote, unlinks the files it landed and restores the gate.

That ordering is load-bearing and was measured wrong. When the commit came
first, a single archive member that made file-landing raise left attacker
pipelines on disk with no state file — and ``pipelines_acknowledged()`` reads
"acknowledged" when the file is absent, so one viewer-gated ``GET /transforms``
exec'd them. The same failure then ran a cleanup that deleted the Parquet parts
of versions whose rows had already committed, which is the
registered-but-missing version this docstring used to promise could not happen.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import tarfile
import uuid
from pathlib import Path
from typing import IO, Any, Optional

from pydantic import BaseModel, Field

from laurelin.core import fileperms
from laurelin.core.config import MARKER as MARKER_FILE
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore, propagate_markings
from laurelin.core.models import DATASET_KINDS
from laurelin.core.storage import Storage, storage_for
from laurelin.export.manifest import (
    DATA_STATE_KEY,
    FORMAT_VERSION,
    MANIFEST_MEMBER,
    MEMBERSHIP_TABLE,
    NEEDS_CREDENTIALS_KEY,
    TABLE_POLICY,
    TRAILER_MEMBER,
    DatasetPlan,
    DataState,
    ExportManifest,
    ExportTrailer,
    ImportRefused,
    PrincipalRef,
    Withheld,
    exported_tables,
    origin_slug,
)
from laurelin.transforms.flow_files import PIPELINE_FILE_SUFFIXES

COPY_CHUNK = 1 << 20

# Declared-size budgets, enforced from the tar header before a byte is read.
#
# Measured, before these existed: a 514 KiB gzipped archive carrying one extra
# `tables/audit_log.jsonl` member that declared 512 MiB of newline filler took
# the importer from 172 MiB to 4794 MiB of RSS — a 9,400x amplification — and
# then reported a *successful* import, because the blank lines were skipped by
# the parser. The same trick at 50 GiB is an OOM kill of the API worker,
# reachable by any admin who accepts an archive from a stranger, which is
# precisely the Foundry-refugee case this feature is for.
#
# The numbers: a governance configuration is tens of thousands of rows, and
# audit_log is the only table that grows without bound. 256 MiB of JSONL is
# roughly two million audit rows, which is far past any workspace this has been
# measured on and still an order of magnitude below the smallest box.
MAX_TABLE_MEMBER_BYTES = 256 << 20
MAX_TABLE_TOTAL_BYTES = 512 << 20
# ontology/*.yml, pipelines/*.py and laurelin.yml. These are hand-written
# files; a megabyte of YAML is already implausible.
MAX_FILE_MEMBER_BYTES = 16 << 20

# A storage key an archive is allowed to name. Anchored, no traversal, no
# scheme, no backslash.
#
# Measured, before this existed: `files_json` was imported verbatim and
# unvalidated, so an archive naming `../tenant_b_secret.parquet` made
# `Storage.resolve` (`storage.py:83`, an f-string with `lstrip('/')`) read a
# Parquet file from the *sibling* workspace directory and serve it through
# `GET /datasets/{name}/rows`. Absolute paths were already neutralised by that
# `lstrip`; the relative form was not.
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# Tables whose rows travel in the archive and are deliberately NOT written.
#
# The spec's rule 1 ("every governance row imports verbatim") and rule 3 ("the
# group starts with no members") read as a contradiction; the stated invariant
# decides it. A membership row and a clearance row are not *rules* — they are
# principal *bindings*, and a binding is the only thing that can widen. So the
# rules land and the bindings do not, which is exactly "writes every rule,
# binds no principal".
QUARANTINED = ("users", "group_members", "clearances", "api_tokens", MEMBERSHIP_TABLE)

# Anchored, and checked independently of tarfile's own filter. `filter="data"`
# is a default that differs across the supported range (it needs >= 3.11.4 and
# the floor is 3.11), so it is belt and this is braces.
_SAFE_MEMBER_RE = re.compile(
    r"^(manifest\.json|TRAILER\.json|laurelin\.yml"
    r"|(tables|ontology|pipelines|data)/[A-Za-z0-9._/-]+)$"
)

# The tables an empty workspace must have no rows in. Not a table count: a
# populated *derived* table is not evidence of authored state.
_PRISTINE_TABLES = (
    "datasets", "groups", "markings", "users", "dataset_grants",
    "ontology_grants", "dataset_policies", "dashboards", "object_apps",
    "sources", "engines", "schedules",
)

_IMPORT_STATE_FILE = ".laurelin-import.json"
PIPELINES_UNACKNOWLEDGED = "pipelines_unacknowledged"

# The governance-*rule* tables that actually land on import (the bindings —
# clearances, group_members, users — are QUARANTINED above and never widen).
# A grant of ``everyone can_view can_edit``, a removed row policy or a stripped
# marking rides in exactly one of these, and the raw-SQL importer writes them
# verbatim, below the ticketed store methods (by design). They map one-to-one
# to the change kinds the approval gate enumerates, so they are the tables the
# import path must refuse to apply while second-approver mode is armed.
_IMPORT_GOVERNANCE_TABLES = (
    "dataset_grants", "ontology_grants", "dataset_policies", "dataset_markings",
)


def _import_governance_tables(tables: dict) -> list[str]:
    """Which governance-rule tables the archive would write, if any."""
    return [t for t in _IMPORT_GOVERNANCE_TABLES if tables.get(t)]


class ImportOptions(BaseModel):
    dry_run: bool = False
    merge: bool = False
    confirm: Optional[str] = None
    rename_prefix: Optional[str] = None
    metadata_only: bool = False
    actor: str = "import"
    #: The sha256 the operator was handed with the archive, out of band. Not a
    #: signature: it proves the bytes are the ones whoever gave you the digest
    #: meant, and nothing about who that was. Checked before the first member
    #: is parsed, because everything downstream reads these bytes.
    expect_sha256: Optional[str] = None


class Collision(BaseModel):
    kind: str                 # "dataset" | "group" | "marking" | "user"
    name: str
    severity: str             # "refusal" | "widening-risk" | "note"
    detail: str
    resolution: str = ""


class ImportReport(BaseModel):
    """What an import did, or what it would do. Also the confirmation token.

    ``report_sha256`` is the digest of everything else in this document, so a
    ``--merge --confirm <digest>`` refuses when the report the operator read is
    not the report that would now be applied.
    """

    applied: bool = False
    dry_run: bool = False
    merge: bool = False
    manifest: Optional[ExportManifest] = None
    target_not_pristine: dict[str, int] = Field(default_factory=dict)
    collisions: list[Collision] = Field(default_factory=list)
    rows_imported: dict[str, int] = Field(default_factory=dict)
    rows_quarantined: dict[str, int] = Field(default_factory=dict)
    principals: list[PrincipalRef] = Field(default_factory=list)
    withheld: list[Withheld] = Field(default_factory=list)
    datasets: list[DatasetPlan] = Field(default_factory=list)
    marking_renames: dict[str, str] = Field(default_factory=dict)
    dataset_renames: dict[str, str] = Field(default_factory=dict)
    # The clearance checklist, with each marking named as it exists *here*.
    # Separate from `rows_quarantined` because a count is not a checklist, and
    # separate from the archive's own clearances.jsonl because a namespaced
    # marking makes that list wrong at exactly the moment it matters.
    quarantined_clearances: list[dict[str, str]] = Field(default_factory=list)
    parts_written: int = 0
    bytes_written: int = 0
    files_written: list[str] = Field(default_factory=list)
    import_state: str = PIPELINES_UNACKNOWLEDGED
    warnings: list[str] = Field(default_factory=list)
    report_sha256: str = ""

    def sealed(self) -> "ImportReport":
        # The digest covers what *would be applied*, not how the run was
        # invoked — otherwise the dry run and the apply hash differently and
        # the confirmation token could never match anything.
        body = self.model_dump(
            mode="json", exclude={"report_sha256", "applied", "dry_run"}
        )
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return self.model_copy(update={"report_sha256": digest})


# --------------------------------------------------------------------------- safety

def require_data_filter() -> None:
    """The floor is 3.11 and ``tarfile.data_filter`` landed in 3.11.4.

    Failing here with a sentence beats failing later with an AttributeError
    inside an extraction loop.
    """
    if not hasattr(tarfile, "data_filter"):
        raise ImportRefused(
            "This interpreter's tarfile has no data_filter (added in 3.11.4). "
            "Upgrade to 3.11.4+ before importing an archive: without it the "
            "stdlib will happily extract absolute paths and symlinks."
        )


def safe_member_name(member: tarfile.TarInfo) -> str:
    """Validate a member name, or refuse.

    Symlinks, hardlinks and device nodes are rejected outright rather than
    sanitized: none of them has any legitimate place in a workspace archive, so
    their presence is evidence rather than a formatting problem.
    """
    name = member.name
    if not member.isreg():
        raise ImportRefused(
            f"Archive member {name!r} is a {member.type!r}, not a regular file. "
            "A workspace archive contains only regular files; refusing."
        )
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ImportRefused(f"Archive member {name!r} is an absolute path; refusing.")
    if ".." in Path(name).parts:
        raise ImportRefused(f"Archive member {name!r} escapes the workspace; refusing.")
    # Must already be in normal form. Measured: `ontology/.` passed every check
    # above — `Path("ontology/.").parts` is `('ontology',)`, so the `..` test
    # never saw it and the regex matched — and then `_land_files` chmod'd the
    # workspace's ontology *directory* to 0600, stripping its traverse bit and
    # aborting the import halfway through. Anything not already normalised is a
    # name meaning something other than what it says.
    if name != posixpath.normpath(name):
        raise ImportRefused(
            f"Archive member {name!r} is not in normal form (it contains '.', "
            "a doubled or trailing slash, or something equivalent). A name that "
            "does not mean what it says is refused rather than normalised."
        )
    if not _SAFE_MEMBER_RE.match(name):
        raise ImportRefused(
            f"Archive member {name!r} is not a name this format defines. "
            "Refusing rather than writing something nobody planned for."
        )
    return name


# --------------------------------------------------------------------------- pristine

def target_is_pristine(store: MetadataStore, workspace: Workspace) -> tuple[bool, dict[str, int]]:
    """Whether the target holds any authored state, and what it holds."""
    counts: dict[str, int] = {}
    with store._conn() as c:
        for table in _PRISTINE_TABLES:
            row = c.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
            n = int(row["n"]) if row else 0
            if n:
                counts[table] = n
    for directory, suffixes in (
        (workspace.pipelines_dir, PIPELINE_FILE_SUFFIXES),
        (workspace.ontology_dir, (".yml", ".yaml")),
    ):
        if directory.is_dir():
            n = sum(
                1 for p in directory.iterdir()
                if p.is_file() and p.suffix.lower() in suffixes
            )
            if n:
                counts[directory.name + "/"] = n
    return (not counts), counts


class Resolution:
    """What ``_collisions`` decided, and what the writer must do about it."""

    def __init__(self) -> None:
        self.collisions: list[Collision] = []
        self.marking_renames: dict[str, str] = {}
        self.dataset_renames: dict[str, str] = {}
        # (table, conflict-key tuple) pairs the destination already holds and
        # that must therefore not be inserted again.
        self.skip: dict[str, set[tuple]] = {}
        # Archive members deliberately not landed.
        self.skip_files: set[str] = set()

    def reuse(self, table: str, key: tuple) -> None:
        self.skip.setdefault(table, set()).add(key)

    @property
    def refusals(self) -> list[Collision]:
        return [c for c in self.collisions if c.severity == "refusal"]


def _key_of(row: dict, columns: tuple[str, ...]) -> tuple:
    return tuple(str(row.get(c)) for c in columns)


def _collisions(
    store: MetadataStore,
    workspace: Workspace,
    manifest: ExportManifest,
    tables: dict[str, list[dict]],
    files: dict[str, Path],
    options: ImportOptions,
) -> Resolution:
    """The merge report: what the two workspaces disagree about.

    Name is the only join key there is, so every collision here is a case of
    two different things claiming one identity. Each gets the treatment its
    blast radius deserves — refuse, namespace, reuse, or say so out loud.

    Everything a merge could collide over is checked here, because a collision
    this function does not name is a collision the writer meets as a driver
    error. Measured, when it only looked at datasets, markings, groups and
    users: a destination sharing a single dashboard, object app, schedule,
    source, engine, group or marking name aborted the apply with a raw
    ``IntegrityError`` (HTTP 500), which made ``--merge`` unusable on any real
    destination — including on the two resolutions the report itself printed.
    """
    resolution = Resolution()
    out = resolution.collisions
    marking_renames = resolution.marking_renames
    dataset_renames = resolution.dataset_renames
    slug = manifest.origin.origin_slug or origin_slug(manifest.origin.workspace_dir)

    existing_datasets = {d.name for d in store.list_datasets()}
    for row in tables.get("datasets", []):
        name = row["name"]
        if name not in existing_datasets:
            continue
        if options.rename_prefix:
            dataset_renames[name] = f"{options.rename_prefix}{name}"
            out.append(Collision(
                kind="dataset", name=name, severity="note",
                detail="A dataset of this name already exists at the target.",
                resolution=f"imported as {dataset_renames[name]!r}",
            ))
        else:
            out.append(Collision(
                kind="dataset", name=name, severity="refusal",
                detail=(
                    "A dataset of this name already exists at the target. Two "
                    "different tables claiming one identity has no safe merge."
                ),
                resolution="pass --rename-prefix to import it under another name",
            ))

    existing_markings = {m["name"]: m.get("description", "") for m in store.list_markings()}
    for row in tables.get("markings", []):
        name = row["name"]
        if name not in existing_markings:
            continue
        # The origin digest lives in the envelope only. Adding a column would
        # be a migration on two backends for an import-time check; the accepted
        # cost is that a re-export of the destination cannot reproduce origin
        # attribution, and this comment is the disclosure.
        if existing_markings[name] == (row.get("description") or ""):
            resolution.reuse("markings", (name,))
            out.append(Collision(
                kind="marking", name=name, severity="note",
                detail="A marking of this name and description already exists.",
                resolution="reused (reported, never silent)",
            ))
        else:
            renamed = f"{name}.{slug}"[:48]
            marking_renames[name] = renamed
            out.append(Collision(
                kind="marking", name=name, severity="note",
                detail=(
                    "A marking of this name exists with a different description, "
                    "so it is a different classification wearing the same word."
                ),
                resolution=f"namespaced as {renamed!r}",
            ))

    existing_groups = {g["name"].lower() for g in store.list_groups()}
    grant_targets: dict[str, set[str]] = {}
    for row in tables.get("dataset_grants", []):
        if row.get("subject_kind") == "group":
            grant_targets.setdefault(
                (row.get("subject") or "").lower(), set()
            ).add(row.get("dataset") or "?")
    for row in tables.get("groups", []):
        name = row["name"].lower()
        if name not in existing_groups:
            continue
        # Reused rather than re-inserted: a groups row carries only a name and
        # a timestamp, so the destination's row already says everything the
        # imported one would. The widening risk below is about its *members*,
        # which is a different table and is never bound by an import.
        resolution.reuse("groups", (row["name"],))
        for dataset in sorted(grant_targets.get(name, {"(no dataset grants)"})):
            out.append(Collision(
                kind="group", name=row["name"], severity="widening-risk",
                detail=(
                    f"Grant group:{row['name']} on {dataset} will bind to the "
                    "DESTINATION's membership, not the source's."
                ),
                resolution="review the destination group's members before applying",
            ))

    for row in tables.get("users", []):
        if store.get_user(row["username"]) is not None:
            out.append(Collision(
                kind="user", name=row["username"], severity="note",
                detail=(
                    "A user of this name already exists at the target, so the "
                    "imported rules naming it will bind when you rebind."
                ),
                resolution="confirm it is the same person",
            ))

    _ontology_grant_collisions(store, workspace, tables, out)
    _file_collisions(workspace, files, resolution)
    # Compared *after* the renames above, because --rename-prefix is precisely
    # the resolution for a dataset collision and its versions must not then be
    # reported as colliding under the old name.
    _generic_collisions(store, _renamed(tables, resolution), resolution)
    return resolution


def _renamed(tables: dict[str, list[dict]], resolution: Resolution) -> dict[str, list[dict]]:
    """The rows as they will be written: dataset and marking renames applied."""
    if not (resolution.dataset_renames or resolution.marking_renames):
        return tables
    def dataset(value: str) -> str:
        return resolution.dataset_renames.get(value, value)

    out: dict[str, list[dict]] = {}
    for table, rows in tables.items():
        fresh = []
        for row in rows:
            row = _rename_dataset_columns(row, dataset)
            if table == "datasets":
                row = dict(row, name=resolution.dataset_renames.get(
                    row.get("name"), row.get("name")))
            elif table == "markings":
                row = dict(row, name=resolution.marking_renames.get(
                    row.get("name"), row.get("name")))
            elif table in ("dataset_markings", "clearances"):
                row = dict(row, marking=resolution.marking_renames.get(
                    row.get("marking"), row.get("marking")))
            fresh.append(row)
        out[table] = fresh
    return out


def _ontology_grant_collisions(
    store: MetadataStore, workspace: Workspace,
    tables: dict[str, list[dict]], out: list[Collision],
) -> None:
    """An imported ontology grant may never land on a type the target owns.

    ``_evaluate`` ORs over every matching grant, so an added grant can only
    widen — and ``ontology_grants`` is keyed on ``object_type``, which
    ``--rename-prefix`` does not touch.

    Measured: a source with a *stale* ``report`` grant (the type had since been
    renamed out of its YAML, and nothing cascades the grant row) merged into a
    destination whose own ``report`` was backed by ``secret_ds``. A viewer went
    from 403 to reading a row out of a dataset the archive never contained, and
    the only collision reported was about the ``root`` user.
    """
    imported = {
        (row.get("object_type") or "") for row in tables.get("ontology_grants", [])
    }
    if not imported:
        return
    existing_types = set(_declared_object_types(workspace.ontology_dir))
    existing_grants = {row.get("object_type") for row in store.list_grants()}
    for object_type in sorted(imported):
        if object_type not in existing_types and object_type not in existing_grants:
            continue
        out.append(Collision(
            kind="object_type", name=object_type, severity="refusal",
            detail=(
                f"The archive carries ontology grants for object type "
                f"{object_type!r} and the target already has that type. Grants "
                "are OR-ed, so importing these could only widen access to the "
                "target's own backing dataset — which this archive does not "
                "contain."
            ),
            resolution=(
                "rename the type at one end, or drop its grants at the source, "
                "then re-export"
            ),
        ))


def _declared_object_types(ontology_dir: Path) -> list[str]:
    """api_names declared by the YAML in a directory.

    Read with yaml directly rather than through ``load_ontology`` because the
    destination's ontology may already be unloadable, and a collision check
    that raises is worse than one that under-reports.
    """
    import yaml

    names: list[str] = []
    if not Path(ontology_dir).is_dir():
        return names
    for path in sorted(Path(ontology_dir).iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".yml", ".yaml"):
            continue
        try:
            parsed = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if not isinstance(parsed, dict):
            continue
        for entry in parsed.get("object_types") or []:
            if isinstance(entry, dict) and entry.get("api_name"):
                names.append(str(entry["api_name"]))
    return names


def _file_collisions(
    workspace: Workspace, files: dict[str, Path], resolution: Resolution
) -> None:
    """Workspace files are never overwritten, and never brick the ontology.

    Two failures measured, both silent and both after the metadata had already
    committed. Same basename: ``shutil.move`` overwrote the destination's own
    ``ontology/own.yml`` and its object types were simply gone. Different
    basename, same ``api_name``: ``load_ontology`` began raising, and *every*
    ontology route returned 400 — including for types the archive never
    mentioned.
    """
    out = resolution.collisions
    for name, spilled in sorted(files.items()):
        destination = workspace.root / name
        if not destination.exists():
            continue
        if _same_bytes(destination, spilled):
            # Re-importing the same archive is the ordinary case (a dry run
            # followed by an apply, or a retry). Identical bytes are not two
            # things claiming one name, so there is nothing to refuse.
            resolution.skip_files.add(name)
            continue
        if name == MARKER_FILE:
            # The one member that always collides: `laurelin init` writes
            # laurelin.yml, so the target has one before the archive arrives.
            # It carries only {name, description} (config.py reads nothing
            # else), and the target's own name is not the archive's to
            # overwrite — so the target keeps it, and the report says so.
            resolution.skip_files.add(name)
            out.append(Collision(
                kind="file", name=name, severity="note",
                detail=(
                    "Every initialised workspace has a laurelin.yml. It carries "
                    "only the workspace name and description."
                ),
                resolution="kept the target's; the archive's name is in the manifest",
            ))
            continue
        out.append(Collision(
            kind="file", name=name, severity="refusal",
            detail=(
                f"The target already has {name}. Landing the archive's copy "
                "would overwrite a file this import did not write and cannot "
                "put back."
            ),
            resolution=f"move or delete {name} at the target, then re-run",
        ))

    incoming: dict[str, str] = {}
    for name, spilled in sorted(files.items()):
        # Only files that will actually land: one being skipped because it is
        # byte-identical to the target's own copy declares the same types the
        # target already declares, which is agreement, not a collision.
        if not name.startswith("ontology/") or name in resolution.skip_files:
            continue
        for api_name in _declared_object_types_of(spilled):
            incoming[api_name] = name
    if not incoming:
        return
    for api_name in sorted(set(incoming) & set(_declared_object_types(workspace.ontology_dir))):
        out.append(Collision(
            kind="object_type", name=api_name, severity="refusal",
            detail=(
                f"Both workspaces declare object type {api_name!r} "
                f"({incoming[api_name]} at the source). load_ontology refuses a "
                "duplicate api_name, so landing this would 400 every ontology "
                "route in the target, including for types the archive never "
                "mentioned."
            ),
            resolution="rename the type at one end before exporting",
        ))


def _same_bytes(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        return left.read_bytes() == right.read_bytes()
    except OSError:
        return False


def _declared_object_types_of(path: Path) -> list[str]:
    import yaml

    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, yaml.YAMLError):
        return []
    if not isinstance(parsed, dict):
        return []
    return [
        str(e["api_name"]) for e in (parsed.get("object_types") or [])
        if isinstance(e, dict) and e.get("api_name")
    ]


# Handled above with reasoning the generic pass cannot express.
_BESPOKE_CONFLICTS = ("datasets", "markings", "groups")


def _generic_collisions(
    store: MetadataStore, tables: dict[str, list[dict]], resolution: Resolution
) -> None:
    """Every remaining unique key, compared against the destination.

    This is the belt for the tables nobody thought about. It exists because the
    apply path was measured aborting with a raw driver ``IntegrityError`` on
    seven tables at once, none of which the report had mentioned — an HTTP 500
    where the whole design says there should be a refusal naming the conflict.
    """
    for table, rows in tables.items():
        spec = TABLE_POLICY.get(table)
        if spec is None or not spec.conflict_key or table in _BESPOKE_CONFLICTS:
            continue
        if table in QUARANTINED:
            continue
        columns = ", ".join(spec.conflict_key)
        with store._conn() as c:
            existing = {
                _key_of(dict(r), spec.conflict_key)
                for r in c.execute(f"SELECT {columns} FROM {table}").fetchall()
            }
        if not existing:
            continue
        for row in rows:
            key = _key_of(row, spec.conflict_key)
            if key not in existing:
                continue
            if spec.on_conflict == "reuse":
                resolution.reuse(table, key)
                continue
            resolution.collisions.append(Collision(
                kind=table, name=" / ".join(key), severity="refusal",
                detail=(
                    f"The target already has a {table} row with "
                    f"{columns} = {' / '.join(key)}. Two authored objects "
                    "claiming one identity have no safe merge: keeping either "
                    "one silently discards the other."
                ),
                resolution=(
                    f"rename or remove the target's {table} row before merging"
                ),
            ))


# --------------------------------------------------------------------------- reading

def _open_stream(archive: Path | str | IO[bytes]):
    if hasattr(archive, "read"):
        return tarfile.open(fileobj=archive, mode="r|*"), None
    handle = open(archive, "rb")  # noqa: SIM115 — closed by the caller below
    return tarfile.open(fileobj=handle, mode="r|*"), handle


def read_manifest(archive: Path | str | IO[bytes]) -> ExportManifest:
    """Parse ``manifest.json`` from the head of the stream and stop.

    The whole reason the manifest is the first member: a dry run prints the
    withheld-secrets report and the rebind checklist without reading a
    terabyte of Parquet.
    """
    require_data_filter()
    tar, handle = _open_stream(archive)
    try:
        member = tar.next()
        if member is None or safe_member_name(member) != MANIFEST_MEMBER:
            raise ImportRefused(
                "The first archive member is not manifest.json. A Laurelin "
                "export always leads with it; this is not one, or it was "
                "repacked by something that reordered it."
            )
        payload = tar.extractfile(member)
        assert payload is not None
        return _validated(ExportManifest.model_validate_json(payload.read()))
    finally:
        tar.close()
        if handle is not None:
            handle.close()


def _validated(manifest: ExportManifest) -> ExportManifest:
    # Both bounds. Measured: only the upper one existed, so an archive
    # declaring format_version 0 — or -1 — was parsed as a version 1 archive
    # and imported. A version this build has never emitted is not a version
    # this build knows how to read.
    if manifest.format_version < 1:
        raise ImportRefused(
            f"Archive declares format_version {manifest.format_version}, which "
            "no Laurelin has ever written. Version numbers start at 1; refusing "
            "rather than guessing that it meant 1."
        )
    if manifest.format_version > FORMAT_VERSION:
        raise ImportRefused(
            f"Archive declares format_version {manifest.format_version}; this "
            f"build understands {FORMAT_VERSION}. There is no upgrade path yet, "
            "and guessing at a future layout is how governance rows land in the "
            "wrong columns."
        )
    return manifest


# --------------------------------------------------------------------------- writing

def _insert(conn, table: str, columns: tuple[str, ...], rows: list[dict]) -> int:
    """Insert a table's rows, turning any driver complaint into a refusal.

    ``_validate_rows`` catches what can be checked ahead of time; this catches
    the rest. Measured, before it existed: a duplicate row, a NOT NULL column
    missing from a hand-edited archive and a JSON object where a string belonged
    each escaped as a raw ``IntegrityError`` or ``ProgrammingError``, which the
    API turns into a bare 500 — the one status code that tells an operator
    nothing about an archive they are trying to diagnose.
    """
    if not rows:
        return 0
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    try:
        conn.executemany(
            f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
            [[_bindable(table, c, row.get(c)) for c in columns] for row in rows],
        )
    except Exception as exc:  # noqa: BLE001 — every backend has its own hierarchy
        raise ImportRefused(
            f"The metadata store refused the archive's {table} rows: "
            f"{type(exc).__name__}: {exc}. Nothing was written. The archive is "
            "malformed or was hand-edited; there is no partial import."
        ) from None
    return len(rows)


def _bindable(table: str, column: str, value: Any) -> Any:
    """Anything a driver cannot bind is the archive's problem, not a crash."""
    if value is None or isinstance(value, (str, int, float, bool, bytes)):
        return value
    raise ImportRefused(
        f"{table}.{column} holds a {type(value).__name__} where the column takes "
        "a scalar. A JSON column travels as a *string* in this format; refusing."
    )


def _sentinel(source_json: Any, state: str) -> str:
    """Stamp a dataset whose bytes did not travel.

    The catalog refuses to scan a dataset carrying this. A 409 rather than an
    empty result set, because zero rows in a governance product is
    indistinguishable from a working row policy — which is exactly the
    subtly-broken outcome this feature exists to prevent.
    """
    try:
        parsed = json.loads(source_json or "{}")
    except ValueError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {"value": parsed}
    parsed[NEEDS_CREDENTIALS_KEY] = True
    parsed[DATA_STATE_KEY] = state
    return json.dumps(parsed)


def needs_credentials(source: dict | None) -> bool:
    return bool(source) and bool(source.get(NEEDS_CREDENTIALS_KEY))


# The authoring rules the rest of the tree enforces. Import writes through the
# backend rather than the routes — deliberately, so an unresolvable grant is
# not dropped — which means these have to be re-applied here or not at all.
_DATASET_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")          # catalog.py:42
_MARKING_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")  # routes.py:1161
_GROUP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,31}$")    # auth_routes.py:568


def _validate_rows(tables: dict[str, list[dict]]) -> None:
    """Re-apply the name and enum rules the routes enforce.

    Measured, before this existed: an archive could land a dataset called
    ``../escaped`` (permanently un-writable and un-compactable, because
    ``catalog.write`` rejects the name it would have to use), a ``kind`` of
    ``totally-made-up``, a 5000-character marking name, and a
    ``dataset_versions.version`` of ``"9999999"`` as a string. None of it was an
    escape — ``catalog.write`` is still the gate on the write path — but all of
    it is a catalog row no other code path in the tree can operate on.
    """
    def refuse(what: str, value: Any, rule: str) -> None:
        raise ImportRefused(
            f"The archive's {what} is {value!r}, which is not a name this "
            f"workspace can operate on ({rule}). Every other way into the "
            "catalog enforces it, so a row that skips it is a row nothing else "
            "can write to, compact or rename."
        )

    for row in tables.get("datasets", []):
        name = str(row.get("name", ""))
        if not _DATASET_NAME_RE.match(name):
            refuse("dataset name", name, "^[a-z][a-z0-9_]*$")
        if str(row.get("kind") or "managed") not in DATASET_KINDS:
            raise ImportRefused(
                f"Dataset {name!r} declares kind {row.get('kind')!r}, which this "
                f"build does not have a reader for. Known kinds: "
                f"{', '.join(DATASET_KINDS)}."
            )
    for row in tables.get("markings", []):
        if not _MARKING_NAME_RE.match(str(row.get("name", ""))):
            refuse("marking name", row.get("name"), "^[a-z0-9][a-z0-9_.-]{0,47}$")
    for row in tables.get("groups", []):
        if not _GROUP_NAME_RE.match(str(row.get("name", ""))):
            refuse("group name", row.get("name"), "^[a-z0-9][a-z0-9_.-]{1,31}$")
    for row in tables.get("dataset_versions", []):
        _require_int("dataset_versions.version", row.get("version"))
        for key in _version_keys(row):
            _require_safe_key(row.get("dataset"), key)
        if row.get("path"):
            _require_safe_key(row.get("dataset"), str(row["path"]))
    for row in tables.get("dataset_markings", []):
        _require_int("dataset_markings.inherited", row.get("inherited") or 0)


def _require_int(what: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ImportRefused(
            f"{what} is {value!r}, which is not an integer. A version number "
            "that is a string sorts and increments as a string; refusing."
        )
    return value


def _version_keys(row: dict) -> list[str]:
    raw = row.get("files_json")
    if not raw:
        return []
    try:
        files = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        raise ImportRefused(
            f"dataset_versions.files_json for {row.get('dataset')!r} is not JSON."
        ) from None
    if not isinstance(files, list):
        raise ImportRefused(
            f"dataset_versions.files_json for {row.get('dataset')!r} is not a list."
        )
    return [str(f) for f in files]


def _require_safe_key(dataset: Any, key: str) -> None:
    """A part key must stay inside the workspace's storage prefix.

    ``Storage.resolve`` is ``f"{self.base}/{key.lstrip('/')}"`` with no
    containment check, and ``catalog.version_files`` hands the result straight
    to pyarrow. Measured: an archive whose ``files_json`` said
    ``../tenant_b_secret.parquet`` — together with governance the same archive
    authored — served another workspace directory's Parquet through
    ``GET /datasets/{name}/rows``.
    """
    if ".." in key.split("/") or not _SAFE_KEY_RE.match(key):
        raise ImportRefused(
            f"Dataset {dataset!r} references the storage key {key!r}, which "
            "leaves the workspace's data prefix or is not a plain relative key. "
            "An archive does not get to name what the destination reads."
        )


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _expect_digest(
    archive: Path | str | IO[bytes], expected: str, workspace: Workspace
) -> tuple[Path | str | IO[bytes], Optional[Path]]:
    """Refuse unless the archive's bytes hash to ``expected``. Before anything.

    This is a **digest handshake, not a signature** — the same distinction
    PORTABILITY.md keeps: there is no key here, so it proves the bytes are the
    ones whoever gave you the digest was holding, and says nothing about who
    that was. The trailer inside the archive cannot do this job, because
    whoever rewrites a member can rewrite the trailer; a digest that travelled
    by a different route than the archive cannot be rewritten with it.

    Ordering is the point. The check happens before the manifest is parsed, so
    a mismatch never reaches the code that reads an untrusted member. For a
    stream (``laurelin import -``) that means spooling first: a digest cannot
    be computed from bytes that are already being consumed, and "we verified it
    as we imported it" would verify nothing.
    """
    expected = expected.strip().lower()
    if not _DIGEST_RE.match(expected):
        raise ImportRefused(
            f"--expect-sha256 {expected!r} is not a sha256: it must be 64 hex "
            "characters, as printed by `laurelin export` and by `sha256sum`."
        )
    spooled: Optional[Path] = None
    if hasattr(archive, "read"):
        # 0600 in the workspace, not /tmp: this is the whole workspace's
        # governance and data, and /tmp is world-traversable on a normal box.
        # Same reasoning as the export spool.
        spooled = workspace.root / f".laurelin-verify-{uuid.uuid4().hex[:8]}.tar"
        fd = os.open(spooled, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(archive, out, COPY_CHUNK)
        source: Path | str | IO[bytes] = spooled
    else:
        source = archive
    digest = hashlib.sha256()
    with open(source, "rb") as fh:  # type: ignore[arg-type]
        for chunk in iter(lambda: fh.read(COPY_CHUNK), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        if spooled is not None:
            spooled.unlink(missing_ok=True)
        raise ImportRefused(
            f"Archive sha256 is {actual}, not the {expected} you expected. "
            "Nothing was read from it. Either this is not the archive whose "
            "digest you were given, or it changed on the way here."
        )
    return source, spooled


def import_workspace(
    archive: Path | str | IO[bytes],
    workspace: Workspace,
    store: MetadataStore,
    options: Optional[ImportOptions] = None,
    storage: Optional[Storage] = None,
) -> ImportReport:
    """Reconstruct ``archive`` into ``workspace``. Returns the report.

    Two phases when merging: the first call writes a report and applies
    nothing, the second must quote that report's digest back. Everything else
    is one pass over the stream.
    """
    options = options or ImportOptions()
    storage = storage or storage_for(workspace)
    require_data_filter()

    spooled: Optional[Path] = None
    if options.expect_sha256:
        archive, spooled = _expect_digest(archive, options.expect_sha256, workspace)

    tar, handle = _open_stream(archive)
    staging = workspace.root / f".laurelin-import-{uuid.uuid4().hex[:8]}"
    conn = None
    committed = False
    written_parts: list[str] = []
    landed: list[Path] = []
    state_file: Optional[Path] = None
    state_backup: Optional[bytes] = None
    observed = _Observed()
    try:
        manifest = _read_manifest_member(tar, observed)
        pristine, counts = target_is_pristine(store, workspace)
        if not pristine and not options.merge:
            summary = ", ".join(f"{n} {t}" for t, n in sorted(counts.items()))
            raise ImportRefused(
                f"Target workspace is not empty ({summary}). Import into a "
                "populated workspace is a merge: with username and marking name "
                "as the only join keys, an existing group starts matching the "
                "imported grants the instant the rows land. Re-run with --merge "
                "to review the collision report, or `laurelin init` a fresh "
                "workspace."
            )

        report = ImportReport(
            dry_run=options.dry_run, merge=options.merge, manifest=manifest,
            target_not_pristine=counts, principals=list(manifest.principals),
            withheld=list(manifest.withheld), datasets=list(manifest.datasets),
        )

        # Everything but data/** is small enough to hold, and the collision
        # report needs the whole picture before the first row is written.
        tables, files, data_members, trailer_member = _drain(
            tar, options, staging, observed
        )
        # Before anything is inspected, let alone written: an archive whose
        # bytes do not match its own trailer is not the archive that was
        # exported, and every check downstream reads those bytes.
        _verify_trailer(tar, trailer_member, observed, report)
        _validate_rows(tables)

        resolution = _collisions(store, workspace, manifest, tables, files, options)
        report.collisions = resolution.collisions
        report.marking_renames = resolution.marking_renames
        report.dataset_renames = resolution.dataset_renames
        # The archive's clearance list is a checklist an operator works from,
        # so it has to name the marking as it exists *here*. Measured: after a
        # namespaced marking, granting exactly what clearances.jsonl said left
        # the principal denied, and the rename lived only in a field the
        # checklist does not mention.
        report.quarantined_clearances = [
            {
                "username": row.get("username", ""),
                "marking_at_source": row.get("marking", ""),
                "marking": resolution.marking_renames.get(
                    row.get("marking", ""), row.get("marking", "")
                ),
            }
            for row in tables.get("clearances", [])
        ]
        # The archive decides what arrived, not the manifest. Measured: dropping
        # every data/ member from an archive whose manifest still said
        # "included" imported clean and then raised a bare FileNotFoundError
        # carrying an absolute server path — the empty-not-loud failure this
        # feature exists to prevent, in its rawest form.
        data_states = _reconcile_data_states(
            manifest, tables, {name for name, _ in data_members}, options, report
        )
        # An archive's part key is not allowed to name a part the destination
        # already has. See `_relocate_colliding_parts`.
        key_map = _relocate_colliding_parts(storage, tables, data_members, report)

        if resolution.refusals:
            first = resolution.refusals[0]
            raise ImportRefused(
                f"{len(resolution.refusals)} collision(s) have no safe merge, "
                f"starting with {first.kind} {first.name!r}: {first.detail} "
                f"({first.resolution})"
            )

        sealed = report.sealed()
        if options.dry_run or (options.merge and options.confirm != sealed.report_sha256):
            if options.merge and not options.dry_run:
                raise ImportRefused(
                    "A merge needs the digest of the report you read: re-run "
                    f"with --confirm {sealed.report_sha256}. Written to the "
                    "report so a report that changed between read and apply "
                    "refuses instead of applying."
                )
            return sealed

        # Second-approver mode is armed: the digest ceremony is a ONE-party
        # confirmation (the same admin reads the report and quotes its hash), so
        # letting a governance-bearing import through here would walk straight
        # past the queue that every REST/MCP/SCIM governance write obeys — the
        # "guard on one path, absent on the next" wound. The raw-SQL importer
        # cannot decompose the archive into per-change tickets, so it fails
        # closed: a governance-rule-bearing import is refused until review is
        # off (disabling it is itself a queued loosening — approvals.py), or the
        # archive is stripped to data + inert config. Verified against finding:
        # importing an ``everyone can_view can_edit`` grant used to auto-apply
        # with an auto-approved record and zero pending proposals.
        settings = store.get_setting("approvals") or {}
        if settings.get("require_second_approver"):
            gov = _import_governance_tables(tables)
            if gov:
                raise ImportRefused(
                    "Second-approver mode is enabled, so a workspace import that "
                    "carries governance rules cannot apply on one admin's digest "
                    f"confirmation alone (archive writes: {', '.join(gov)}). The "
                    "import path writes governance below the approval gate by "
                    "design and cannot split the archive into reviewable "
                    "proposals, so it refuses rather than bypass the queue. "
                    "Import data and inert config, or disable second-approver "
                    "mode first (that change itself queues for a second admin)."
                )

        conn = store.backend.connect()
        report.rows_imported = _write_tables(conn, tables, resolution, data_states)
        report.rows_quarantined = {
            name: len(tables.get(name, [])) for name in QUARANTINED if tables.get(name)
        }
        _recompute_effective_markings(conn)

        if not options.metadata_only:
            for name, member in data_members:
                key = name[len("data/"):] if name.startswith("data/") else name
                key = key_map.get(f"data/{key}", f"data/{key}")
                report.bytes_written += _write_part(storage, key, member)
                written_parts.append(key)
            report.parts_written = len(written_parts)

        # The gate goes on BEFORE the first pipeline file exists on disk, not
        # after the last one. Measured, when it went on afterwards: an archive
        # with one member that made `_land_files` raise left attacker pipelines
        # in pipelines/ with no state file at all, and
        # `pipelines_acknowledged()` returns True when the file is absent — so
        # a single viewer-gated `GET /transforms` exec'd them. The state file
        # is fail-closed only if it is written first.
        state_file, state_backup = _write_import_state(workspace, manifest, report)
        report.files_written = _land_files(
            workspace, staging,
            {k: v for k, v in files.items() if k not in resolution.skip_files},
            landed,
        )

        # Last, because everything above can still fail and roll back. Nothing
        # after this line may raise on a healthy system: the parts, the files
        # and the rows are all in place, and the failure handler must not undo
        # a durable import.
        conn.commit()
        committed = True
        conn.close()
        conn = None

        report.applied = True
        store.log_audit(
            "workspace_imported",
            {
                "origin": manifest.origin.origin_id,
                "rows": sum(report.rows_imported.values()),
                "parts": report.parts_written,
                "quarantined": report.rows_quarantined,
            },
            actor=options.actor,
        )
        # The import's superadmin gate + dry-run report + report_sha256 confirm
        # digest IS its approval ceremony; the auto-approved record below files
        # it in the proposals inbox alongside route-driven changes, so the
        # "guard on one path, absent on the next" objection is answered in the
        # record itself. The raw-SQL writes above bypass the ticketed store
        # methods BY DESIGN (see the module docstring); this record is how
        # that path stays visible to review.
        from laurelin.core.approvals import file_record
        from laurelin.core.models import utcnow_iso

        sealed_final = report.sealed()
        file_record(
            store, kind="import", target=manifest.origin.origin_id,
            payload={"report_sha256": sealed_final.report_sha256,
                     "rows": sum(report.rows_imported.values())},
            proposer=options.actor, ticket_kind="import_confirmed",
            decided_by=options.actor, classification="loosening",
            rationale="workspace import (digest-confirmed ceremony)",
            applied_at=utcnow_iso(),
        )
        return sealed_final
    except BaseException:
        if conn is not None:
            conn.rollback()
            conn.close()
        if not committed:
            # Only unreferenced once the rows that named them rolled back.
            # Measured, when this ran unconditionally: a failure *after* the
            # commit deleted the Parquet parts of versions that were already
            # durable, producing exactly the registered-but-missing version
            # this module's docstring promises cannot happen.
            for key in written_parts:
                storage.delete(key)
            for path in landed:
                path.unlink(missing_ok=True)
            # The gate comes off last, so a failure while undoing the files
            # leaves an unacknowledged workspace rather than a silently armed
            # one.
            if state_file is not None:
                if state_backup is None:
                    state_file.unlink(missing_ok=True)
                else:
                    state_file.write_bytes(state_backup)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        tar.close()
        if handle is not None:
            handle.close()
        if spooled is not None:
            spooled.unlink(missing_ok=True)


class _Observed:
    """What actually arrived, hashed on the way past.

    The trailer's per-member digests were being parsed and then ignored: only
    ``member_count != len(members)`` was checked. Measured, with a repacked
    archive whose TRAILER.json was left byte-identical — a stripped
    ``dataset_markings`` and a grant widened to ``everyone can_view can_edit``
    imported clean, with empty warnings and a successful ``workspace_imported``
    audit line.
    """

    def __init__(self) -> None:
        self.digests: dict[str, tuple[str, int]] = {}
        self.skipped: set[str] = set()

    def record(self, name: str, digest: str, size: int) -> None:
        self.digests[name] = (digest, size)


def _read_manifest_member(tar, observed: _Observed) -> ExportManifest:
    member = tar.next()
    if member is None or safe_member_name(member) != MANIFEST_MEMBER:
        raise ImportRefused(
            "The first archive member is not manifest.json. A Laurelin export "
            "always leads with it."
        )
    _require_size(member, MAX_FILE_MEMBER_BYTES, "manifest")
    payload = tar.extractfile(member)
    assert payload is not None
    raw = payload.read()
    observed.record(MANIFEST_MEMBER, hashlib.sha256(raw).hexdigest(), len(raw))
    return _validated(ExportManifest.model_validate_json(raw))


def _require_size(member, limit: int, what: str) -> None:
    """Refuse on the declared size, before a byte is read."""
    if member.size > limit:
        raise ImportRefused(
            f"Archive member {member.name!r} declares {member.size} bytes, over "
            f"the {limit}-byte ceiling for a {what} member. A compressed archive "
            "can declare far more than it costs to build, so the ceiling is "
            "checked from the header rather than discovered by running out of "
            "memory."
        )


def _drain(tar, options: ImportOptions, staging: Path, observed: _Observed):
    """Consume the stream up to (but not including) TRAILER.json.

    Table rows and workspace files are held; Parquet parts are *not* — they are
    spilled to the staging directory so a terabyte archive costs disk, not RAM.
    A stream cannot be rewound, so the alternative would be buffering it.

    Every member is hashed here, because this is the only pass over the bytes
    and the trailer cannot be checked against anything else.
    """
    tables: dict[str, list[dict]] = {}
    files: dict[str, Path] = {}
    data_members: list[tuple[str, Path]] = []
    seen: set[str] = set()
    table_bytes = 0
    staging.mkdir(parents=True, exist_ok=True)

    while True:
        member = tar.next()
        if member is None:
            raise ImportRefused(
                "The archive ended without a TRAILER.json. It is truncated, and "
                "nothing here can tell which members are missing."
            )
        name = safe_member_name(member)
        if name == TRAILER_MEMBER:
            _refuse_file_as_directory(seen)
            # Returned rather than extracted here: in stream mode extractfile()
            # is only valid for the member the reader is currently positioned
            # on, and nothing advances the reader between here and the check.
            return tables, files, data_members, member
        if name in seen:
            # Measured: a second copy of any tables/*.jsonl member silently
            # replaced the first, with no warning anywhere and member_count
            # still agreeing with itself. Which member wins is not something an
            # archive gets to leave ambiguous.
            raise ImportRefused(
                f"Archive member {name!r} appears twice. Which copy is the real "
                "one is not a question this format answers; refusing."
            )
        seen.add(name)
        payload = tar.extractfile(member)
        if payload is None:
            continue
        if name.startswith("tables/"):
            _require_size(member, MAX_TABLE_MEMBER_BYTES, "table")
            table_bytes += member.size
            if table_bytes > MAX_TABLE_TOTAL_BYTES:
                raise ImportRefused(
                    f"The archive's tables/ members total more than "
                    f"{MAX_TABLE_TOTAL_BYTES} bytes. That is far past any "
                    "governance configuration measured on this tree; refusing "
                    "rather than reading it into memory to find out."
                )
            table = name[len("tables/"):-len(".jsonl")]
            tables[table] = _read_rows(name, payload, observed)
        elif name in ("laurelin.yml",) or name.startswith(("ontology/", "pipelines/")):
            _require_size(member, MAX_FILE_MEMBER_BYTES, "workspace file")
            target = staging / name.replace("/", "__")
            _spill(name, payload, target, observed)
            files[name] = target
        elif name.startswith("data/"):
            if options.metadata_only:
                # Streamed past, deliberately not landed — and deliberately not
                # hashed, because reading a terabyte to check a digest is the
                # exact cost --metadata-only exists to avoid.
                observed.skipped.add(name)
                continue
            target = staging / f"part-{len(data_members):08d}.bin"
            _spill(name, payload, target, observed)
            data_members.append((name, target))


def _refuse_file_as_directory(names: set[str]) -> None:
    """No member may be a directory of another member.

    Measured: an archive holding both ``pipelines/z.py`` and
    ``pipelines/z.py/a.py`` landed the first and then raised a bare
    ``FileExistsError`` from ``mkdir`` on the second — a 500 over HTTP, and
    (before the ordering was fixed) a half-written workspace. One name cannot
    be both a file and a directory, so the archive is malformed and says so
    here rather than discovering it mid-write.
    """
    for name in sorted(names):
        parent = posixpath.dirname(name)
        while parent:
            if parent in names:
                raise ImportRefused(
                    f"Archive member {name!r} is inside {parent!r}, which is "
                    "itself a member. One name cannot be both a file and a "
                    "directory; refusing."
                )
            parent = posixpath.dirname(parent)


def _read_rows(name: str, payload, observed: _Observed) -> list[dict]:
    """Parse one JSONL member, hashing and counting as it goes."""
    digest = hashlib.sha256()
    total = 0
    rows: list[dict] = []
    pending = b""
    while True:
        chunk = payload.read(COPY_CHUNK)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
        pending += chunk
        lines = pending.split(b"\n")
        pending = lines.pop()
        rows.extend(_parse_row(name, line) for line in lines if line.strip())
    if pending.strip():
        rows.append(_parse_row(name, pending))
    observed.record(name, digest.hexdigest(), total)
    return [r for r in rows if r is not None]


def _parse_row(name: str, line: bytes):
    try:
        row = json.loads(line)
    except ValueError as exc:
        raise ImportRefused(
            f"Archive member {name!r} contains a line that is not JSON: {exc}. "
            "A table member is one JSON object per line; refusing."
        ) from None
    if not isinstance(row, dict):
        raise ImportRefused(
            f"Archive member {name!r} contains a line that is not a JSON "
            f"object but a {type(row).__name__}; refusing."
        )
    return row


def _spill(name: str, payload, target: Path, observed: _Observed) -> None:
    digest = hashlib.sha256()
    total = 0
    with open(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as out:
        while True:
            chunk = payload.read(COPY_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            out.write(chunk)
    observed.record(name, digest.hexdigest(), total)


def _relocate_colliding_parts(
    storage: Storage, tables: dict, data_members: list, report: "ImportReport"
) -> dict[str, str]:
    """Give every arriving part a key the destination is not already using.

    **The defect this closes, measured end to end.** ``_write_part`` calls
    ``storage.open_output_stream``, which truncates — there is no ``O_EXCL``
    anywhere on this path — and the part-write loop used the archive member
    name *verbatim* as the destination key. ``--rename-prefix`` renames only
    the metadata (``datasets.name`` and the dataset columns of the rows that
    reference it); it never touched ``files_json`` or the member keys. So on
    the documented safe resolution for a name collision — "imported as
    'imported_salaries'", a ``note``, not a refusal — an archive's
    ``data/salaries/parts/<uuid>.parquet`` landed **on top of** the
    destination's live part of that exact key, which the destination's own
    ``salaries`` still references. The operator was told the import had been
    safely renamed; ``GET /datasets/salaries/rows`` then returned the
    archive's rows.

    It is not a theoretical collision. Every archive exported from a workspace
    keeps that workspace's part keys, so a round trip — export, edit, re-import
    with ``--merge`` — collides on every part by construction, and the archive
    is unsigned (``docs/PORTABILITY.md`` says so), so its Parquet is whatever
    the person holding the tar wanted.

    The sibling this also closes: the pre-commit rollback deletes
    ``written_parts``. When those keys were the destination's own, a merge that
    failed after the part write **deleted the destination's live data**.

    Relocating rather than refusing, because refusing would break the one
    workflow ``--rename-prefix`` exists for: the imported dataset is a new
    dataset here and its parts are new parts. The version rows that reference
    the key are rewritten with it, in the same pass, so nothing is left
    pointing at a key that was not written.
    """
    arriving = [
        (name if name.startswith("data/") else f"data/{name}")
        for name, _ in data_members
    ]
    key_map: dict[str, str] = {}
    for key in arriving:
        if not storage.exists(key):
            continue
        parts = key.split("/")
        dataset = parts[1] if len(parts) > 2 else "imported"
        suffix = key.rsplit(".", 1)[-1] if "." in parts[-1] else "parquet"
        key_map[key] = Storage.new_part_key(dataset, suffix)
    if not key_map:
        return key_map
    for row in tables.get("dataset_versions", []):
        keys = _version_keys(row)
        if keys:
            row["files_json"] = json.dumps(
                [key_map.get(k, k) for k in keys], separators=(",", ":")
            )
        path = row.get("path")
        if path and str(path) in key_map:
            row["path"] = key_map[str(path)]
    report.warnings.append(
        f"{len(key_map)} arriving data part(s) named a storage key this "
        f"workspace already uses and were written to fresh keys instead. The "
        f"destination's existing parts were not overwritten."
    )
    return key_map


def _write_part(storage: Storage, key: str, spilled: Path) -> int:
    total = 0
    with spilled.open("rb") as source, storage.open_output_stream(key) as sink:
        while True:
            chunk = source.read(COPY_CHUNK)
            if not chunk:
                break
            sink.write(chunk)
            total += len(chunk)
    return total


def _write_tables(
    conn,
    tables: dict[str, list[dict]],
    resolution: Resolution,
    data_states: dict[str, str],
) -> dict[str, int]:
    """Land every rule row, in dependency order, inside one transaction."""
    counts: dict[str, int] = {}

    def rename_marking(value: str) -> str:
        return resolution.marking_renames.get(value, value)

    def rename_dataset(value: str) -> str:
        return resolution.dataset_renames.get(value, value)

    for table in exported_tables(include_audit=True):
        if table in QUARANTINED:
            continue
        rows = tables.get(table)
        if not rows:
            continue
        spec = TABLE_POLICY[table]
        columns = spec.columns

        if table == "markings":
            rows = [dict(r, name=rename_marking(r["name"])) for r in rows]
        elif table == "dataset_markings":
            rows = [
                dict(r, dataset=rename_dataset(r["dataset"]),
                     marking=rename_marking(r["marking"]), inherited=0)
                for r in rows
                # inherited=1 is recomputed below from explicit markings and
                # lineage. Trusting the exporter's filter would be one place
                # too many for the derived rows to sneak in.
                if int(r.get("inherited") or 0) == 0
            ]
        elif table == "datasets":
            rows = [
                dict(
                    r,
                    name=rename_dataset(r["name"]),
                    source_json=(
                        r.get("source_json")
                        if data_states.get(r["name"]) == DataState.included.value
                        else _sentinel(r.get("source_json"),
                                       data_states.get(r["name"],
                                                       DataState.elsewhere.value))
                    ),
                )
                for r in rows
            ]
        elif table == "schedules":
            # Forced off on every import path: an imported workspace must never
            # fire a build or a connector sync against a production system at
            # boot. The watermark still travels, so enabling one later does not
            # replay history.
            rows = [dict(r, enabled=0) for r in rows]
        elif table in ("sources", "engines"):
            # Stamped for the same reason a dataset without its bytes is: the
            # endpoint was withheld, so the row is a shape awaiting a
            # credential, not a working registration. Measured, before this:
            # an imported source read back as `{'url': None}` with a null
            # status, indistinguishable from a healthy connector until the
            # first sync failed with a driver error.
            column = "config_json" if table == "sources" else "options_json"
            rows = [
                dict(r, **{column: _sentinel(r.get(column), "needs_credentials")},
                     **({"last_sync_status": "needs_credentials"}
                        if table == "sources" else {}))
                for r in rows
            ]
        elif table in ("dataset_versions", "dataset_policies", "dataset_grants",
                       "lineage_edges"):
            rows = [_rename_dataset_columns(r, rename_dataset) for r in rows]

        skip = resolution.skip.get(table)
        if skip:
            rows = [r for r in rows if _key_of(r, spec.conflict_key) not in skip]

        counts[table] = _insert(conn, table, columns, rows)
    return counts


_DATASET_COLUMNS = ("dataset", "upstream_dataset", "downstream_dataset")


def _rename_dataset_columns(row: dict, rename) -> dict:
    out = dict(row)
    for column in _DATASET_COLUMNS:
        if column in out and out[column]:
            out[column] = rename(out[column])
    return out


def _recompute_effective_markings(conn) -> None:
    """Derive ``dataset_markings(inherited=1)`` from what just landed.

    Run on the import's own connection, using the same pure closure the store
    uses, because a second connection cannot see uncommitted rows — and because
    two implementations of *which markings apply* is the one duplication a
    governance layer cannot afford.
    """
    datasets = [r["name"] for r in conn.execute("SELECT name FROM datasets").fetchall()]
    edges = [
        (r["upstream_dataset"], r["downstream_dataset"])
        for r in conn.execute(
            "SELECT upstream_dataset, downstream_dataset FROM lineage_edges"
        ).fetchall()
    ]
    explicit: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT dataset, marking FROM dataset_markings WHERE inherited = 0"
    ).fetchall():
        explicit.setdefault(row["dataset"], set()).add(row["marking"])

    effective = propagate_markings(datasets, edges, explicit)
    conn.execute("DELETE FROM dataset_markings WHERE inherited = 1")
    conn.executemany(
        "INSERT INTO dataset_markings (dataset, marking, inherited) VALUES (?, ?, 1)",
        [(d, m) for d, marks in sorted(effective.items()) for m in sorted(marks)],
    )


def _verify_trailer(tar, member, observed: _Observed, report: ImportReport) -> None:
    """Compare every arrived member against the digest the trailer claims.

    **What this catches and what it does not.** There is no signature here, so
    an attacker who rewrites a member can also rewrite the trailer; that limit
    is stated in docs/PORTABILITY.md and is not fixed by this function. What it
    does catch is every *partial* rewrite — the case measured before it existed,
    where two governance members were replaced, the trailer was left
    byte-identical, and a stripped marking plus a grant widened to ``everyone
    can_view can_edit`` imported clean with empty warnings. It also catches the
    ordinary corruption the trailer was always advertised as catching, which it
    was not: the digests were parsed and then never compared to anything.
    """
    if member is None:
        raise ImportRefused("The archive has no TRAILER.json; it is truncated.")
    _require_size(member, MAX_FILE_MEMBER_BYTES, "trailer")
    payload = tar.extractfile(member)
    assert payload is not None
    trailer = ExportTrailer.model_validate_json(payload.read())
    if trailer.member_count != len(trailer.members):
        report.warnings.append(
            "TRAILER.json member_count disagrees with its own member list."
        )

    mismatched: list[str] = []
    for name, (digest, size) in sorted(observed.digests.items()):
        claim = trailer.members.get(name)
        if claim is None:
            mismatched.append(f"{name}: arrived but the trailer does not list it")
        elif claim.sha256 != digest or claim.bytes != size:
            mismatched.append(
                f"{name}: trailer claims {claim.sha256[:12]}/{claim.bytes}B, "
                f"{digest[:12]}/{size}B arrived"
            )
    for name in sorted(set(trailer.members) - set(observed.digests) - observed.skipped):
        mismatched.append(f"{name}: listed in the trailer but never arrived")
    if mismatched:
        raise ImportRefused(
            f"{len(mismatched)} archive member(s) do not match TRAILER.json, "
            f"starting with {mismatched[0]}. The archive was corrupted or "
            "repacked after it was written. Refusing before anything is "
            "written, because a partially rewritten archive is how a marking "
            "goes missing and a grant widens without a warning."
        )
    if observed.skipped:
        report.warnings.append(
            f"{len(observed.skipped)} data member(s) were streamed past "
            "unread (--metadata-only), so their digests were not verified."
        )


def _strip_data(key: str) -> str:
    """Part keys and part member names are the same string; be sure of it."""
    return key[len("data/"):] if key.startswith("data/") else key


def _reconcile_data_states(
    manifest: ExportManifest,
    tables: dict[str, list[dict]],
    arrived: set[str],
    options: ImportOptions,
    report: ImportReport,
) -> dict[str, str]:
    """Decide each dataset's data state from the archive, not the manifest.

    The manifest declares intent at the head of a stream; what a dataset gets
    is decided by whether its parts actually turned up. Measured, when the
    manifest was trusted: an archive whose ``data/`` members had been removed
    imported clean with ``data_state: "included"``, then raised a bare
    ``FileNotFoundError`` naming an absolute server path on the first read.
    The inverse also held — a manifest with an empty ``datasets`` list stamped
    needs-credentials on a dataset whose parts were on disk.
    """
    declared = {d.name: d for d in manifest.datasets}
    states: dict[str, str] = {}
    missing: list[str] = []

    for row in tables.get("datasets", []):
        name = str(row.get("name", ""))
        plan = declared.get(name)
        kind = str(row.get("kind") or "managed")
        if kind != "managed":
            states[name] = (plan.data_state if plan else DataState.elsewhere.value)
            continue
        if options.metadata_only:
            states[name] = DataState.metadata_only.value
            continue
        keys = {
            key
            for version in tables.get("dataset_versions", [])
            if version.get("dataset") == name
            for key in _version_keys(version)
        }
        if keys and {_strip_data(k) for k in keys} <= {_strip_data(k) for k in arrived}:
            states[name] = DataState.included.value
        elif not keys:
            # A managed dataset with no parts at all is a dataset with no
            # versions yet. Nothing is missing, so nothing is stamped.
            states[name] = DataState.included.value
        else:
            states[name] = DataState.metadata_only.value
            missing.append(name)

    if missing:
        report.warnings.append(
            f"{len(missing)} managed dataset(s) reference parts the archive did "
            f"not carry ({', '.join(sorted(missing)[:5])}). They were imported "
            "needing credentials, so a read refuses with a message instead of "
            "returning nothing."
        )
    for plan in report.datasets:
        if plan.name in states:
            plan.data_state = states[plan.name]
    return states


def _land_files(
    workspace: Workspace, staging: Path, files: dict[str, Path], landed: list[Path]
) -> list[str]:
    """Move staged workspace files into place, 0600, before the rows commit.

    Every destination is checked first, because a half-landed set is the state
    with no owner: the rows have not committed, but the files are on disk.
    ``_file_collisions`` has already refused any name the target holds, so this
    is the second look — the target could have grown a file since, and an
    overwrite here is unrecoverable (the caller can unlink what it created, but
    it cannot put back what it clobbered).
    """
    plan: list[tuple[Path, Path]] = []
    for name, spilled in sorted(files.items()):
        destination = workspace.root / name
        if destination.exists():
            raise ImportRefused(
                f"{name} already exists in the target workspace. An import "
                "never overwrites a file it did not write."
            )
        plan.append((destination, spilled))

    written: list[str] = []
    for destination, spilled in plan:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(spilled), str(destination))
            landed.append(destination)
            os.chmod(destination, 0o600)
        except OSError as exc:
            raise ImportRefused(
                f"Could not land {destination.name}: {exc.strerror or exc}. "
                "Nothing was committed."
            ) from None
        written.append(str(destination.relative_to(workspace.root)))
    return written


def _write_import_state(workspace: Workspace, manifest: ExportManifest,
                        report: ImportReport) -> tuple[Path, Optional[bytes]]:
    """Park imported pipelines behind an explicit admin acknowledgement.

    ``transforms/api.py`` ``exec``s every ``.py`` in pipelines/ unsandboxed, so
    an import that ran them would be a remote code execution primitive dressed
    as data movement.
    """
    state = {
        "import_state": PIPELINES_UNACKNOWLEDGED,
        "origin_id": manifest.origin.origin_id,
        "imported_at": manifest.created_at,
        "content_warnings": [w.model_dump() for w in manifest.content_warnings],
    }
    path = workspace.root / _IMPORT_STATE_FILE
    previous = path.read_bytes() if path.exists() else None
    fileperms.write_private(path, json.dumps(state, indent=2))
    report.import_state = PIPELINES_UNACKNOWLEDGED
    return path, previous


def import_state(workspace: Workspace) -> Optional[dict]:
    path = workspace.root / _IMPORT_STATE_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None


def pipelines_acknowledged(workspace: Workspace) -> bool:
    state = import_state(workspace)
    return state is None or state.get("import_state") != PIPELINES_UNACKNOWLEDGED


def acknowledge_pipelines(workspace: Workspace, store: Optional[MetadataStore] = None,
                          actor: str = "admin") -> None:
    """Clear the block. An admin act, audited, never inferred."""
    state = import_state(workspace) or {}
    state["import_state"] = "acknowledged"
    state["acknowledged_by"] = actor
    path = workspace.root / _IMPORT_STATE_FILE
    fileperms.write_private(path, json.dumps(state, indent=2))
    if store is not None:
        store.log_audit("import_pipelines_acknowledged", {"actor": actor}, actor=actor)


def require_pipelines_acknowledged(workspace: Workspace) -> None:
    if not pipelines_acknowledged(workspace):
        raise ImportRefused(
            "This workspace's pipelines arrived in an import and have not been "
            "acknowledged. They are exec'd unsandboxed on every build, so an "
            "admin must review them first: POST "
            "/api/v1/workspace/import/acknowledge-pipelines."
        )
