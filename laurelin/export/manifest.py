"""What a workspace export carries, and what it deliberately does not.

``TABLE_POLICY`` is the single source of truth. Every one of the 28 metadata
tables appears in it exactly once, with the columns that travel spelled out —
an **allowlist**, never a denylist. That direction is the whole security
argument: a column added tomorrow that nobody classified defaults to *absent*,
which surfaces at import as a missing field somebody fixes. A denylist would
default to *present*, and a secret-bearing column would ship in the clear from
the day it was added, silently, in a file that gets emailed and committed to
git.

A CI guard cross-checks this table against ``laurelin/core/db.py::_SCHEMA`` by
parsing the DDL, so the guard cannot be satisfied by editing a list in a test.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from laurelin.core.models import utcnow_iso

# Bumped only when a reader written for the old layout would misread the new
# one. An archive declaring a higher version is refused rather than guessed at.
FORMAT_VERSION = 1

# The two members whose position in the stream is load-bearing.
MANIFEST_MEMBER = "manifest.json"
TRAILER_MEMBER = "TRAILER.json"

# Import stamps this into `datasets.source_json` for anything whose bytes did
# not travel. The catalog refuses to scan a dataset carrying it — a 409 rather
# than an empty result set, because zero rows in a governance product is
# indistinguishable from a working row policy.
NEEDS_CREDENTIALS_KEY = "__laurelin_needs_credentials"
DATA_STATE_KEY = "__laurelin_data_state"


class TableClass(str, Enum):
    """Why a table travels, or why it does not."""

    portable = "portable"      # authored state; reconstructing needs it
    derived = "derived"        # recomputable, and wrong if copied
    ephemeral = "ephemeral"    # live credentials or in-flight work


class DataState(str, Enum):
    included = "included"
    elsewhere = "elsewhere"
    elsewhere_absolute_path = "elsewhere_absolute_path"
    metadata_only = "metadata_only"


@dataclass(frozen=True)
class TableSpec:
    """One metadata table's export policy.

    ``columns`` is the allowlist. ``drop_columns`` is not a second mechanism —
    it exists so the reason a column is *missing* is written down next to the
    table rather than inferred from its absence.
    """

    cls: TableClass
    reason: str
    columns: tuple[str, ...] = ()
    drop_columns: tuple[str, ...] = ()
    secret_columns: tuple[str, ...] = ()
    # Authored free-form columns whose *carried* value is credential-scanned.
    # Same mechanism and same refusal as pipelines/*.py, for the same reason:
    # these travel near-verbatim because stripping them would destroy what they
    # mean. Measured: a dashboard panel's SQL shipped a postgres DSN with its
    # password, and nothing looked at it.
    scan_columns: tuple[str, ...] = ()
    # The columns that make a row unique. Import compares these against the
    # destination before inserting anything, because a plain INSERT into a
    # populated workspace was measured to abort a merge with a raw driver
    # IntegrityError — a 500, not a refusal, on seven different tables.
    conflict_key: tuple[str, ...] = ()
    # What to do when conflict_key already exists at the destination.
    # "refuse": two authored objects claiming one name have no safe merge.
    # "reuse": the destination's row already says the same thing (groups carry
    #   only a name, and a marking whose description matches is the same
    #   marking), so skipping the insert changes nothing.
    on_conflict: str = "refuse"
    # Nulled with a count in the manifest: driver and connector error text is a
    # documented credential channel (see laurelin/core/starrocks.py:207) and no
    # redactor in this tree covers free-form messages.
    error_columns: tuple[str, ...] = ()
    # api_tokens is ephemeral, but which tokens existed is audit history the
    # operator needs in order to reissue them. The hash is never carried, so
    # nothing here can be replayed.
    only_with_audit: bool = False
    # dataset_markings(inherited=1) is the *output* of recompute_all_markings.
    # Carrying it would let a destination hold effective markings that
    # contradict its own lineage graph.
    row_filter: Optional[tuple[str, Any]] = None
    # Import order. Groups must land before the grants that name them, and
    # datasets before the versions and policies that reference them.
    order: int = 100

    @property
    def travels(self) -> bool:
        return self.cls is TableClass.portable or self.only_with_audit


_PORTABLE = TableClass.portable
_DERIVED = TableClass.derived
_EPHEMERAL = TableClass.ephemeral


TABLE_POLICY: dict[str, TableSpec] = {
    # -- portable: authored governance and catalog state ---------------------
    "markings": TableSpec(
        _PORTABLE,
        "Not read by enforcement — measured, deleting it changes no decision — "
        "but marking_exists() gates authoring, so a destination without it "
        "cannot re-author the policy it just imported.",
        columns=("name", "description", "created_at"),
        conflict_key=("name",),
        on_conflict="reuse",
        order=10,
    ),
    "groups": TableSpec(
        _PORTABLE,
        "Created empty at the destination: group_exists() gates authoring "
        "through the routes, while membership stays unbound (see reader.py).",
        columns=("name", "created_at"),
        conflict_key=("name",),
        on_conflict="reuse",
        order=11,
    ),
    "users": TableSpec(
        _PORTABLE,
        "The roster travels so the operator can see which principals the rules "
        "name. Rows are NOT created at import — binding a principal is an "
        "explicit, audited admin act.",
        columns=("id", "username", "role", "created_at", "disabled", "superadmin"),
        drop_columns=("password_hash",),
        secret_columns=("password_hash",),
        conflict_key=("username",),
        order=12,
    ),
    "datasets": TableSpec(
        _PORTABLE,
        "The catalog. source_json is secret-stripped per key, not omitted "
        "wholesale — dropping it would destroy the registration itself. "
        "expected_fresh_seconds is the health-freshness declaration (task "
        "#74): authored governance state, an integer, carried verbatim — an "
        "archive from before the column existed imports as NULL (undeclared).",
        columns=("name", "description", "created_at", "kind", "source_json",
                 "expected_fresh_seconds"),
        secret_columns=("source_json",),
        scan_columns=("source_json",),
        conflict_key=("name",),
        order=20,
    ),
    "dataset_versions": TableSpec(
        _PORTABLE,
        "The manifest of immutable parts. files_json travels verbatim and is "
        "never re-derived: an append references parts written for earlier "
        "versions, so re-deriving from a directory listing would be wrong.",
        columns=(
            "dataset", "version", "created_at", "row_count", "schema_json",
            "path", "files_json", "build_id", "source", "snapshot_id",
        ),
        conflict_key=("dataset", "version"),
        order=21,
    ),
    "lineage_edges": TableSpec(
        _PORTABLE,
        "A governance input, not decoration. Measured: dropping it and "
        "recomputing declassified a downstream dataset from ['pii'] to [] and "
        "flipped an outsider from (False, False) to (True, False).",
        columns=("upstream_dataset", "downstream_dataset", "transform_name"),
        conflict_key=("upstream_dataset", "downstream_dataset", "transform_name"),
        on_conflict="reuse",
        order=22,
    ),
    "dataset_policies": TableSpec(
        _PORTABLE,
        "policy_json travels byte-faithful. Re-serializing would reorder keys, "
        "and byte identity is what makes the round-trip proof a proof.",
        columns=("dataset", "policy_json", "updated_at"),
        conflict_key=("dataset",),
        order=23,
    ),
    "dataset_grants": TableSpec(
        _PORTABLE,
        "Never filtered by whether the subject resolves. Measured: emptying a "
        "dataset's grant list flips it from allowlist to readable by every "
        "authenticated viewer (permissions.py:333).",
        columns=(
            "id", "dataset", "subject_kind", "subject", "can_view", "can_edit",
            "created_at",
        ),
        conflict_key=("id",),
        order=24,
    ),
    "ontology_grants": TableSpec(
        _PORTABLE,
        "Same fail-open rule as dataset_grants. Needs ontology/*.yml for the "
        "backing_dataset the type names.",
        columns=(
            "id", "object_type", "subject_kind", "subject", "can_view",
            "can_edit", "created_at",
        ),
        conflict_key=("id",),
        order=25,
    ),
    "dataset_markings": TableSpec(
        _PORTABLE,
        "inherited=0 only. inherited=1 is the output of recompute_all_markings "
        "over explicit markings and lineage; importing it verbatim would let a "
        "destination hold effective markings its own graph contradicts.",
        columns=("dataset", "marking", "inherited"),
        row_filter=("inherited", 0),
        conflict_key=("dataset", "marking", "inherited"),
        on_conflict="reuse",
        order=26,
    ),
    "group_members": TableSpec(
        _PORTABLE,
        "Travels so the operator can see the intended membership. Destination "
        "groups stay empty until an explicit rebind: measured, a destination "
        "group reusing an imported name widened an outsider to (True, False).",
        columns=("group_name", "username"),
        conflict_key=("group_name", "username"),
        order=27,
    ),
    "clearances": TableSpec(
        _PORTABLE,
        "Quarantined: exported, reported, never written. A clearance is the "
        "one row type whose only possible effect is to widen.",
        columns=("username", "marking"),
        conflict_key=("username", "marking"),
        order=28,
    ),
    "object_edits": TableSpec(
        _PORTABLE,
        "The writeback log. edit_seq is carried verbatim — it is a log "
        "position, and renumbering it would silently reorder conflict "
        "resolution.",
        columns=(
            "id", "object_type", "pk_value", "kind", "payload_json", "actor",
            "created_at", "edit_seq", "folded_at", "folded_into_version",
        ),
        drop_columns=("seq",),
        conflict_key=("id",),
        order=30,
    ),
    "dashboards": TableSpec(
        _PORTABLE,
        "Authored declarative config; nothing regenerates it.",
        columns=(
            "name", "title", "description", "panels_json", "created_at",
            "created_by", "updated_at",
        ),
        scan_columns=("panels_json",),
        conflict_key=("name",),
        order=31,
    ),
    "analyses": TableSpec(
        _PORTABLE,
        "Authored declarative config; nothing regenerates it. Cells are "
        "instructions only — no result is ever persisted, so nothing here "
        "can replay one caller's rows to another.",
        columns=(
            "name", "title", "description", "cells_json", "next_cell",
            "created_at", "created_by", "updated_at",
        ),
        scan_columns=("cells_json",),
        conflict_key=("name",),
        order=32,
    ),
    "object_apps": TableSpec(
        _PORTABLE,
        "Authored declarative config; nothing regenerates it.",
        columns=(
            "name", "title", "description", "object_type", "config_json",
            "created_at", "created_by", "updated_at",
        ),
        scan_columns=("config_json",),
        conflict_key=("name",),
        order=32,
    ),
    "pipeline_authors": TableSpec(
        _PORTABLE,
        "Who last saved each pipelines/*.py through the API; the Builder "
        "checks that author's read access before running the file's "
        "transforms. Travels so an imported workspace keeps its API-authored "
        "files entitlement-checked; an author who does not exist at the "
        "destination refuses the build with the reassignment remedy, exactly "
        "as an imported flow's author already does.",
        columns=("name", "author", "written_at"),
        conflict_key=("name",),
        order=36,
    ),
    "schedules": TableSpec(
        _PORTABLE,
        "Definition plus watermark. The watermark is carried because "
        "scheduler.py:144 treats None as 'fire now', so dropping it makes "
        "every upstream-triggered schedule fire the moment it is enabled. "
        "enabled is forced to 0 on import regardless of what travelled.",
        columns=(
            "name", "enabled", "trigger_type", "cron", "upstream_dataset",
            "action", "targets_json", "source", "watermark", "created_at",
            "created_by",
        ),
        drop_columns=(
            "next_run_at", "last_run_at", "last_status", "last_error",
            "last_failure_json", "last_build_id", "claimed_by", "lease_expires_at",
        ),
        # `last_failure_json` holds a `Failure`, which is safe by construction —
        # but the export is a file that leaves the building, and there is no
        # reason for it to carry failure history at all.
        error_columns=("last_error", "last_failure_json"),
        scan_columns=("targets_json",),
        conflict_key=("name",),
        order=33,
    ),
    "sources": TableSpec(
        _PORTABLE,
        "Connector definitions, endpoint-stripped. cursor_value travels so an "
        "incremental connector does not re-pull history at the destination.",
        columns=(
            "name", "type", "dataset", "config_json", "created_at",
            "created_by", "last_sync_at", "last_sync_status",
            "last_sync_version", "last_sync_rows", "cursor_value",
        ),
        drop_columns=("last_sync_error", "last_sync_failure_json"),
        secret_columns=("config_json",),
        error_columns=("last_sync_error", "last_sync_failure_json"),
        scan_columns=("config_json",),
        conflict_key=("name",),
        order=34,
    ),
    "engines": TableSpec(
        _PORTABLE,
        "Delegated engine registrations. uri is nulled outright; every value "
        "in options_json is nulled with its key kept, because the key is the "
        "shape the operator must re-fill.",
        columns=("name", "type", "uri", "options_json", "created_at", "created_by"),
        secret_columns=("uri", "options_json"),
        conflict_key=("name",),
        order=35,
    ),
    "audit_log": TableSpec(
        _PORTABLE,
        "History, carried unless --no-audit. id is never carried: on Postgres "
        "it renders as GENERATED ALWAYS AS IDENTITY, which rejects an explicit "
        "insert. Rows go in source order and list_audit orders by id DESC, so "
        "ordering survives reassignment.",
        # min_read_role travels: it is the audience decision the writer made
        # about this row, and an import that dropped it would silently widen
        # every carried row to the column default.
        columns=("timestamp", "actor", "action", "details_json", "min_read_role"),
        drop_columns=("id",),
        # details_json is walked, not nulled: an audit trail with its subjects
        # removed is not an audit trail. Only credential-shaped and error keys
        # go — see secrets.AUDIT_KEY_RE for why the endpoint keys do not.
        secret_columns=("details_json",),
        order=40,
    ),

    # -- ephemeral: live credentials, or in-flight work ----------------------
    "api_tokens": TableSpec(
        _EPHEMERAL,
        "The row IS the authenticator. With audit history the (id, name, "
        "user_id, created_at) shell travels so the operator can see which "
        "tokens existed and reissue them; token_hash never does, and nothing "
        "can be revived from a hash anyway.",
        columns=("id", "name", "user_id", "created_at"),
        conflict_key=("id",),
        drop_columns=("token_hash", "last_used_at"),
        secret_columns=("token_hash",),
        only_with_audit=True,
        order=41,
    ),
    "sessions": TableSpec(
        _EPHEMERAL,
        "token_hash is a live credential. Importing it would keep every "
        "session cookie minted against the source authenticating at the "
        "target. Zero reconstruction value.",
        drop_columns=("token_hash", "user_id", "created_at", "expires_at"),
        secret_columns=("token_hash",),
    ),
    "oidc_flows": TableSpec(
        _EPHEMERAL,
        "Plaintext PKCE code_verifier and nonce: single-use, popped on "
        "callback, immediately exploitable with an intercepted authorization "
        "code, and meaningless at a destination.",
        drop_columns=("state", "nonce", "code_verifier", "redirect_uri", "created_at"),
        secret_columns=("nonce", "code_verifier"),
    ),
    "builds": TableSpec(
        _EPHEMERAL,
        "claim_build matches status IN ('pending','running') with an expired "
        "or null lease, so an imported in-flight build is claimed and "
        "re-executed at the destination. Finished rows are history only.",
        drop_columns=(
            "seq", "id", "targets_json", "status", "started_at", "finished_at",
            "error", "failure_json", "claimed_by", "lease_expires_at",
        ),
        error_columns=("error", "failure_json"),
    ),
    "build_tasks": TableSpec(
        _EPHEMERAL,
        "Belongs to a build that is not carried. dataset_versions.build_id may "
        "dangle, which is a cosmetic broken link, not a correctness problem.",
        drop_columns=(
            "build_id", "transform_name", "output_dataset", "status",
            "started_at", "finished_at", "error", "failure_json", "rows_written",
            "output_version", "expectations_json",
        ),
        error_columns=("error", "failure_json"),
    ),

    # -- derived: recomputable, and wrong if copied --------------------------
    "object_index": TableSpec(
        _DERIVED,
        "Two independent failures. _state_is_current checks fingerprint, "
        "dataset_version and applied_seq and never the digest on the read "
        "path, so an index arriving without its FTS mirror reports caught-up "
        "and serves rows the scan path would not produce. And props_json is an "
        "UNMASKED materialization of governed rows — masking is applied at "
        "read time — so shipping it puts cells in the archive the source's own "
        "column masks forbid. Rebuilt by reindex().",
        drop_columns=(
            "object_type", "pk", "ord", "applied_seq", "title", "search_text",
            "props_json",
        ),
    ),
    "object_index_state": TableSpec(
        _DERIVED,
        "Its only purpose is to authorize the index. Carrying it without the "
        "index is a lie about a table that is not there.",
        drop_columns=(
            "object_type", "dataset_version", "applied_seq", "digest",
            "type_fingerprint", "store_name", "object_count", "built_at",
        ),
    ),
    "transform_state": TableSpec(
        _DERIVED,
        "Degrades safely when absent (builder.py:443 falls back to a full "
        "recompute). Carrying it risks a destination answering 'unchanged' "
        "against a prefix that is not there — a build that writes nothing and "
        "reports success.",
        drop_columns=(
            "transform_name", "input_dataset", "last_version", "last_rows",
            "updated_at",
        ),
    ),
    "scope_locks": TableSpec(
        _DERIVED,
        "Anchor rows whose row locks serialize security-list replaces, plus "
        "the marking-input generation counter that fences stale recomputes "
        "(db.py::_lock_scope). Pure coordination state: rows are created on "
        "first contention and mean nothing outside the database that locked "
        "them. Carrying the generation would let an imported counter mask a "
        "destination's own in-flight recompute.",
        drop_columns=("kind", "scope", "generation"),
    ),
    # -- data health + alerting (task #74) -----------------------------------
    "health_state": TableSpec(
        _DERIVED,
        "Alert dedup state: the previous HealthStatus per dataset, diffed "
        "against a fresh derivation every scheduler tick. Recomputable by "
        "definition — health is a pure function of builds/versions/schedules/"
        "sources — and wrong if copied: an imported 'failing' would suppress "
        "the destination's own first transition alert for that dataset.",
        drop_columns=("dataset", "status", "since"),
    ),
    "health_events": TableSpec(
        _EPHEMERAL,
        "The in-app alert feed: transition history bound to the source "
        "workspace's runtime, like builds (whose history also stays behind). "
        "The durable trail an operator would miss is the audit log, which "
        "already travels.",
        drop_columns=("seq", "dataset", "event", "status", "at"),
    ),
    "alert_webhooks": TableSpec(
        _EPHEMERAL,
        "url IS the credential — a Slack-style webhook carries its secret in "
        "the path, the case redaction.py documents the DSN-shape rule cannot "
        "locate. The API already treats it as write-only (reads come back "
        "WITHHELD); a file that leaves the building gets the stricter posture "
        "and the whole row stays behind: a webhook with no URL is a husk, and "
        "an outbound alert destination is exactly the thing a destination "
        "operator must opt into again, deliberately.",
        drop_columns=(
            "name", "url", "datasets_json", "events_json", "enabled",
            "created_at", "created_by", "last_delivery_at",
            "last_delivery_status", "last_delivery_failure_json",
        ),
        secret_columns=("url",),
    ),
    # -- governance change approval (task #74) --------------------------------
    "proposals": TableSpec(
        _EPHEMERAL,
        "Decision history bound to the source workspace's principals and to "
        "the exact state the comparator classified against — an approve at "
        "the destination would re-run against state the diff never saw, which "
        "is the staleness trap the approve path exists to refuse. "
        "payload_json also embeds governed values (row-rule literals), which "
        "the export posture would have to withhold whole, leaving a record "
        "that certifies nothing. The audited outcomes travel in audit_log.",
        drop_columns=(
            "id", "kind", "target", "payload_json", "diff_json", "rationale",
            "proposer", "proposer_id", "created_at", "state", "classification",
            "ticket_kind", "decided_by", "decided_at", "applied_at",
            "decision_reason",
        ),
    ),
    "workspace_settings": TableSpec(
        _EPHEMERAL,
        "Workspace posture (require_second_approver). Not carried: enabling "
        "second-approver mode refuses unless the workspace has >= 2 active "
        "admins, and an import binds no principals — so a carried 'true' "
        "could land in a one-admin destination that the enable route would "
        "have refused, deadlocking every loosening. The destination's admins "
        "opt in through the route that checks.",
        drop_columns=("key", "value_json"),
    ),
}


# Tables not in db.py::_SCHEMA that an export may still emit. workspace_members
# lives in control.db, outside the workspace, and in multi-workspace mode it
# holds the single largest access-decision input there is.
MEMBERSHIP_TABLE = "workspace_members"
MEMBERSHIP_COLUMNS = ("slug", "username", "role")


# The tables that actually change an access decision. Deliberately smaller than
# "the portable tables": `groups` and `markings` are portable because authoring
# needs them, but measured, deleting either (keeping group_members /
# dataset_markings) changed no decision at all. A guard asserts that every name
# here really does move a fingerprint cell, so the list cannot quietly become
# aspirational.
GOVERNANCE_TABLES = (
    "dataset_grants",
    "ontology_grants",
    "dataset_policies",
    "dataset_markings",
    "lineage_edges",
    "group_members",
    "clearances",
)


def exported_tables(include_audit: bool = True) -> list[str]:
    """Table names in import order — the order the writer must emit them in."""
    names = [
        name for name, spec in TABLE_POLICY.items()
        if spec.travels and (include_audit or name not in ("audit_log", "api_tokens"))
    ]
    return sorted(names, key=lambda n: (TABLE_POLICY[n].order, n))


# Things that were never in the workspace, so no export could have carried
# them. Listed by name so the operator has a checklist rather than a surprise.
ENVIRONMENT_RESUPPLY = (
    "LAURELIN_DATABASE_URL",
    "LAURELIN_DATA_URI",
    "LAURELIN_OIDC_CLIENT_SECRET",
    "LAURELIN_OIDC_ROLE_MAP",
    "LAURELIN_OIDC_SUPERADMIN_GROUP",
    "LAURELIN_SAML_IDP_METADATA",
    "LAURELIN_SCIM_TOKEN",
    "LAURELIN_ICEBERG_WAREHOUSE",
    "LAURELIN_ICEBERG_CATALOG",
)


# --------------------------------------------------------------------------- models

class Withheld(BaseModel):
    """One secret the export refused to carry, and how to put it back.

    Positively enumerated rather than inferred from absence: an operator
    reading the manifest gets a checklist, and a test can assert that every
    entry names a route that exists.
    """

    table: str
    row: str                      # the row's primary key, or "*" for all rows
    field: str                    # column, or dotted path inside a JSON column
    reason: str = "credential"
    required_for: str = ""
    resupply: str = ""


class NotExported(BaseModel):
    table: str
    cls: str
    reason: str
    rebuild: str = ""


class DatasetPlan(BaseModel):
    name: str
    kind: str
    data_state: str
    versions: int = 0
    parts: int = 0
    bytes: int = 0
    reason: str = ""
    note: str = ""


class PrincipalRef(BaseModel):
    """A principal the imported rules name but the archive cannot create."""

    kind: str                      # "user" | "group"
    name: str
    referenced_by: list[str] = Field(default_factory=list)


class PipelineWarning(BaseModel):
    file: str
    line: int
    pattern: str
    preview: str


class TableStat(BaseModel):
    rows: int = 0
    columns: list[str] = Field(default_factory=list)


class Origin(BaseModel):
    workspace_name: str
    workspace_dir: str             # the Storage key prefix, which is the dir basename
    origin_id: str
    origin_slug: str
    metadata_dialect: str
    mode: str = "single"
    multi_slug: Optional[str] = None
    # Recorded because an object-store data plane is an untested path for a
    # full export, and an archive should say what it could not verify.
    data_plane: str = "local"


class Scope(BaseModel):
    data: bool = True
    audit: bool = True
    membership: bool = False
    datasets: Optional[list[str]] = None


class ExportManifest(BaseModel):
    """Declared intent, readable from the head of the stream.

    Split from TRAILER.json deliberately: the manifest must be readable before
    a terabyte of parts so `--dry-run` can print the withheld-secrets report
    without buffering, and per-member digests cannot be known until after they
    are written. One document cannot be both.
    """

    format_version: int = FORMAT_VERSION
    laurelin_version: str = ""
    created_at: str = Field(default_factory=utcnow_iso)
    created_by: str = ""
    origin: Origin
    scope: Scope = Field(default_factory=Scope)
    tables: dict[str, TableStat] = Field(default_factory=dict)
    datasets: list[DatasetPlan] = Field(default_factory=list)
    withheld: list[Withheld] = Field(default_factory=list)
    not_exported: list[NotExported] = Field(default_factory=list)
    environment_resupply: list[str] = Field(default_factory=lambda: list(ENVIRONMENT_RESUPPLY))
    principals: list[PrincipalRef] = Field(default_factory=list)
    content_warnings: list[PipelineWarning] = Field(default_factory=list)
    nulled_error_fields: dict[str, int] = Field(default_factory=dict)
    governance_fingerprint: dict[str, Any] = Field(default_factory=dict)


class MemberDigest(BaseModel):
    sha256: str
    bytes: int


class ExportTrailer(BaseModel):
    """What actually landed. Detects corruption; does not detect tampering —
    there is no signature here and the docs say so."""

    members: dict[str, MemberDigest] = Field(default_factory=dict)
    member_count: int = 0
    total_bytes: int = 0


@dataclass
class ExportOptions:
    """Everything the writer needs that is not the workspace itself."""

    metadata_only: bool = False
    include_audit: bool = True
    gzip: Optional[bool] = None            # None -> on for metadata-only, off otherwise
    allow_content_warnings: bool = False
    datasets: Optional[tuple[str, ...]] = None   # restricts DATA, never governance
    created_by: str = ""
    # Multi-workspace mode: the effective role lives in control.db, outside the
    # workspace, so the writer refuses to guess. The caller (which owns the
    # control store) supplies the rows; the module never opens control.db.
    mode: str = "single"
    multi_slug: Optional[str] = None
    include_membership: Optional[bool] = None
    membership_rows: tuple[dict, ...] = ()
    # A full export from an object-store data plane is an unverified path
    # (no object store was available to measure it against), so it is opt-in
    # rather than silently attempted.
    allow_remote_data_plane: bool = False
    # Where JSONL members are spooled while their size is measured. Defaults to
    # the workspace root rather than /tmp: the spool holds the whole governance
    # configuration, and /tmp is world-traversable on a normal box. Override it
    # when the workspace directory is read-only.
    spool_dir: Optional[str] = None
    governance_fingerprint: dict[str, Any] = field(default_factory=dict)
    #: Set BY the writer, not by the caller: the sha256 of the archive's own
    #: bytes, known only once the last of them is written. A stream cannot
    #: carry its own digest (the trailer is inside the archive, so it can only
    #: cover the members), which is why this comes back on the options object
    #: and is reported after the bytes rather than embedded in them.
    archive_sha256: str = ""

    @property
    def compress(self) -> bool:
        return self.metadata_only if self.gzip is None else self.gzip


class ExportRefused(RuntimeError):
    """The export stopped rather than write something misleading.

    Distinct from an error: every refusal names a flag that overrides it, and
    the CLI maps it to exit code 2.
    """


class ImportRefused(RuntimeError):
    """The import stopped rather than write something misleading."""


class NeedsCredentials(RuntimeError):
    """A dataset was imported without its data or its endpoint.

    Raised instead of returning zero rows. In a governance product an empty
    result set is indistinguishable from a working row policy, which is exactly
    the subtly-broken outcome portability exists to prevent.
    """


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def origin_slug(workspace_dir: str) -> str:
    """A marking-namespace suffix derived from the origin directory.

    Must satisfy the marking name rule ``^[a-z0-9][a-z0-9_.-]{0,47}$`` so that
    ``<name>.<origin_slug>`` is a legal marking at the destination.
    """
    slug = _SLUG_RE.sub("-", workspace_dir.lower()).strip("-")
    return (slug or "origin")[:24]


def origin_id(workspace_name: str, workspace_dir: str) -> str:
    """A stable attribution id for the exporting workspace.

    Attribution, not identity: it is derived from names an operator controls,
    so it says "these two archives came from the same place" and nothing
    stronger. Nothing authenticates on it.
    """
    digest = hashlib.sha256(
        f"laurelin-workspace\0{workspace_name}\0{workspace_dir}".encode()
    ).hexdigest()
    return f"sha256:{digest}"


def not_exported_entries() -> list[NotExported]:
    """The manifest's negative space: every table left out, with its reason."""
    out = []
    for name, spec in sorted(TABLE_POLICY.items()):
        if spec.travels:
            continue
        rebuild = "reindex()" if name.startswith("object_index") else ""
        out.append(NotExported(table=name, cls=spec.cls.value, reason=spec.reason,
                               rebuild=rebuild))
    return out
