"""Shared data models. These are the contracts between all Laurelin modules.

Every module (catalog, transforms, ontology, api) speaks in these types.
Keep this file dependency-light: pydantic only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------

class ColumnSchema(BaseModel):
    name: str
    type: str  # arrow type name, e.g. "string", "int64", "double", "timestamp[us]"


# Every dataset kind, mapped to the SQL dialect a policy must be rendered in
# for it. **Total by construction and with no default**: a kind that is not a
# key here has no dialect, and asking for one raises.
#
# The alternative -- `"clickhouse" if kind == "clickhouse" else "duckdb"` --
# was live until StarRocks, and it is the shape that makes a governance bug
# out of a forgotten line. A new source-scanned kind added without touching
# it would be read with DuckDB's *flat* statement and DuckDB's *quoter*, and
# the guard in `catalog.source_table` that compares the policy's dialect with
# the reader's could not catch it: both sides would say "duckdb".
_SQL_DIALECTS: dict[str, str] = {
    "managed": "duckdb",
    "federated": "duckdb",
    "iceberg": "duckdb",
    "clickhouse": "clickhouse",
    "starrocks": "starrocks",
}

# Kinds read through the source expression rather than local Parquet parts.
_SCANNED_AT_SOURCE = frozenset({"federated", "iceberg", "clickhouse", "starrocks"})

DATASET_KINDS = tuple(_SQL_DIALECTS)


class DatasetInfo(BaseModel):
    name: str
    description: str = ""
    created_at: str = Field(default_factory=utcnow_iso)
    latest_version: Optional[int] = None
    # "managed":   Laurelin owns the Parquet and versions it.
    # "federated": the bytes live elsewhere (Iceberg/Delta/Parquet/Postgres);
    #              Laurelin governs the table and scans it in place, so it has
    #              no versions.
    # "iceberg":   Laurelin owns an Iceberg table — writable and versioned like
    #              managed, but read at source like federated, and readable by
    #              Spark/Trino/DuckDB without Laurelin.
    # "clickhouse": read-only, scanned in place by embedded ClickHouse (chdb).
    #              Like federated in every way a reader cares about, except
    #              that the SQL is a different dialect — see `sql_dialect`.
    # "starrocks": read-only, scanned in place by a StarRocks server over the
    #              MySQL wire protocol. Its own dialect again, and unlike
    #              ClickHouse it is a *remote* engine with write privileges to
    #              lose — see laurelin/core/starrocks.py.
    kind: str = "managed"
    source: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_federated(self) -> bool:
        return self.kind == "federated"

    @property
    def is_iceberg(self) -> bool:
        return self.kind == "iceberg"

    @property
    def is_clickhouse(self) -> bool:
        return self.kind == "clickhouse"

    @property
    def is_starrocks(self) -> bool:
        return self.kind == "starrocks"

    @property
    def scans_at_source(self) -> bool:
        """Read via the source expression rather than local Parquet parts.

        The distinction that matters to a *reader* is not who owns the table
        but where the scan happens — so federated, Iceberg, ClickHouse and
        StarRocks share every read path, and with it one implementation of how
        policy is applied.
        """
        return self.kind in _SCANNED_AT_SOURCE

    @property
    def sql_dialect(self) -> str:
        """Which SQL dialect a policy must be rendered in for this dataset.

        Until ClickHouse, ``scans_at_source`` silently encoded two facts —
        "read via the source expression" *and* "DuckDB renders the SQL". They
        diverge here, and in a governance layer a derivation that drifts is a
        leak rather than a wrong number, so the second fact gets a name and one
        definition instead of N call sites re-deriving it.

        Raises on an unknown kind rather than falling back to DuckDB. A
        fallback is the wrong default in exactly one direction: the engine that
        gets read with the wrong dialect is the *new* one, and the wrong
        dialect is the one whose quoter and statement shape were never checked
        against it.
        """
        try:
            return _SQL_DIALECTS[self.kind]
        except KeyError:
            raise ValueError(
                f"Dataset {self.name!r} has kind {self.kind!r}, which declares "
                "no SQL dialect. Add it to _SQL_DIALECTS in "
                "laurelin/core/models.py — reading it with another engine's "
                "dialect would render its policy in a language it does not "
                "speak."
            ) from None


class DatasetVersionInfo(BaseModel):
    dataset: str
    version: int
    # Iceberg only: the snapshot this version pins, so a Laurelin version
    # number and an Iceberg snapshot mean the same point in history.
    snapshot_id: Optional[int] = None
    created_at: str = Field(default_factory=utcnow_iso)
    row_count: int = 0
    schema_: list[ColumnSchema] = Field(default_factory=list, alias="schema")
    path: str = ""  # workspace-relative directory of this version
    # Workspace-relative Parquet part files making up this version. A version
    # written by `append` lists its predecessor's parts plus the new one, so an
    # append costs O(delta) instead of rewriting the dataset. Empty means
    # "every *.parquet under `path`" — the layout used before manifests, still
    # read correctly.
    files: list[str] = Field(default_factory=list)
    build_id: Optional[str] = None
    source: str = "upload"  # "upload" | "transform" | "api" | "sync:*" | "append"

    model_config = ConfigDict(populate_by_name=True)


class ObjectAppInfo(BaseModel):
    """A curated view over one object type.

    The ontology explorer is generic: every type, every property, every action.
    An *app* is the opposite — one type, the columns that matter, the filters
    that scope it, and only the actions an operator should reach for. Same
    data and the same permissions; a narrower, nameable surface.

    Configuration, not code: everything here is declarative, so an app is
    something you define rather than a frontend you build.
    """

    name: str
    title: str = ""
    description: str = ""
    object_type: str
    # Empty means "every declared property", in ontology order.
    columns: list[str] = Field(default_factory=list)
    # Applied to every listing, so an app can scope itself to the rows that
    # matter (e.g. {"status": "maintenance"}).
    filters: dict[str, str] = Field(default_factory=dict)
    search_placeholder: str = ""
    # Empty means "every action available on the type"; naming them keeps an
    # operational app to the handful of operations it is actually about.
    actions: list[str] = Field(default_factory=list)
    # Link types to show as panels on the detail view.
    links: list[str] = Field(default_factory=list)

    created_at: str = Field(default_factory=utcnow_iso)
    created_by: str = ""
    updated_at: str = Field(default_factory=utcnow_iso)


class ScheduleInfo(BaseModel):
    """A trigger bound to an action — the piece that makes a pipeline run
    without anyone pressing a button."""

    name: str
    enabled: bool = True
    # "cron": fire on a schedule. "upstream": fire when a dataset gains a
    # version, so a pipeline follows its inputs instead of a clock.
    trigger: str = "cron"
    cron: str = ""
    upstream_dataset: str = ""
    # "build" (optionally specific targets) or "sync" (one connector source).
    action: str = "build"
    targets: list[str] = Field(default_factory=list)
    source: str = ""

    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None  # "succeeded" | "failed"
    last_error: Optional[str] = None
    last_build_id: Optional[str] = None
    # Highest upstream version already acted on, for the "upstream" trigger.
    watermark: Optional[int] = None

    created_at: str = Field(default_factory=utcnow_iso)
    created_by: str = ""


class SourceInfo(BaseModel):
    """A configured external data source that syncs into a dataset."""

    name: str
    type: str  # "postgres" | "http" | "file"
    dataset: str
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utcnow_iso)
    created_by: str = ""
    last_sync_at: Optional[str] = None
    last_sync_status: Optional[str] = None  # "succeeded" | "failed"
    last_sync_error: Optional[str] = None
    last_sync_version: Optional[int] = None
    last_sync_rows: Optional[int] = None
    # High-water mark for incremental (mode="append") syncs: the largest value
    # seen in the source's cursor column, carried into the next pull's WHERE.
    cursor_value: Optional[str] = None


# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------

class ChartKind(str, Enum):
    table = "table"
    bar = "bar"
    line = "line"
    area = "area"
    stat = "stat"  # single big number (first cell of the result)


class DashboardPanel(BaseModel):
    """One saved query + presentation.

    Panels execute client-side with the *viewer's* credentials, so each person
    sees their own ACL/RLS-filtered view and a dashboard adds no new read
    surface.

    A panel draws from exactly one of two sources:

    ``sql``
        Arbitrary SQL over datasets. Maximum power, and it sees raw rows.

    ``object_type`` + ``metrics``
        An aggregation over ontology objects. This is the one to reach for
        when charting something the ontology models, because SQL over the
        *backing dataset* misses the edit overlay — it answers from rows an
        action has already changed, and the chart gives no hint that it
        disagrees with the object list beside it.
    """

    id: str
    title: str = ""
    # -- source A: SQL
    sql: str = ""
    # -- source B: an object aggregation
    object_type: str = ""
    group_by: list[str] = Field(default_factory=list)
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    filters: dict[str, str] = Field(default_factory=dict)
    search: str = ""

    chart: ChartKind = ChartKind.table
    # Column bindings (empty = infer: first text column as x, numeric as y).
    x: str = ""
    y: list[str] = Field(default_factory=list)
    width: int = Field(default=6, ge=1, le=12)  # 12-column grid

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "DashboardPanel":
        has_sql, has_object = bool(self.sql.strip()), bool(self.object_type.strip())
        if has_sql and has_object:
            raise ValueError(
                "A panel draws from either sql or object_type, not both — "
                "two sources would make it ambiguous which one the chart shows."
            )
        if not has_sql and not has_object:
            raise ValueError("A panel needs either sql or object_type")
        if has_object and not self.metrics:
            raise ValueError(
                "An object panel needs at least one metric (e.g. "
                '{"op": "count", "alias": "count"})'
            )
        return self

    @property
    def is_object_panel(self) -> bool:
        return bool(self.object_type.strip())


class DashboardInfo(BaseModel):
    name: str
    title: str = ""
    description: str = ""
    panels: list[DashboardPanel] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow_iso)
    created_by: str = ""
    updated_at: str = Field(default_factory=utcnow_iso)


# ---------------------------------------------------------------------------
# Builds & lineage
# ---------------------------------------------------------------------------

class BuildStatus(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class BuildTaskInfo(BaseModel):
    transform_name: str
    output_dataset: str
    status: BuildStatus = BuildStatus.pending
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    rows_written: Optional[int] = None
    output_version: Optional[int] = None
    # One entry per declared expectation: {expectation, passed, severity,
    # measured, message}. Recorded whether the build passed or failed — a
    # check that passed is evidence, and a `warn` that fired needs somewhere
    # to be seen.
    expectations: list[dict] = Field(default_factory=list)


class BuildInfo(BaseModel):
    id: str
    targets: list[str] = Field(default_factory=list)
    status: BuildStatus = BuildStatus.pending
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    tasks: list[BuildTaskInfo] = Field(default_factory=list)


class LineageEdge(BaseModel):
    upstream_dataset: str
    downstream_dataset: str
    transform_name: str


# ---------------------------------------------------------------------------
# Ontology definitions (parsed from workspace ontology/*.yml)
# ---------------------------------------------------------------------------

class PropertyDef(BaseModel):
    type: str = "string"  # string | integer | float | boolean | timestamp | date
    display_name: Optional[str] = None
    description: str = ""


class ObjectTypeDef(BaseModel):
    api_name: str
    display_name: Optional[str] = None
    description: str = ""
    backing_dataset: str
    primary_key: str
    title_property: Optional[str] = None
    properties: dict[str, PropertyDef] = Field(default_factory=dict)

    def title_for(self, obj: dict[str, Any]) -> str:
        key = self.title_property or self.primary_key
        return str(obj.get(key, ""))


class Cardinality(str, Enum):
    one_to_one = "one_to_one"
    one_to_many = "one_to_many"
    many_to_many = "many_to_many"


class LinkTypeDef(BaseModel):
    api_name: str
    display_name: Optional[str] = None
    from_type: str = Field(alias="from")
    to_type: str = Field(alias="to")
    cardinality: Cardinality = Cardinality.one_to_many
    from_property: str  # join key on the from-side object type
    to_property: str  # join key on the to-side object type

    model_config = ConfigDict(populate_by_name=True)


class ActionKind(str, Enum):
    create = "create"
    update = "update"
    delete = "delete"


class ActionParameterDef(BaseModel):
    type: str = "string"
    required: bool = False
    description: str = ""


class ActionDef(BaseModel):
    api_name: str
    display_name: Optional[str] = None
    description: str = ""
    object_type: str
    kind: ActionKind
    parameters: dict[str, ActionParameterDef] = Field(default_factory=dict)


class OntologyDef(BaseModel):
    object_types: list[ObjectTypeDef] = Field(default_factory=list)
    link_types: list[LinkTypeDef] = Field(default_factory=list)
    actions: list[ActionDef] = Field(default_factory=list)

    def object_type(self, api_name: str) -> Optional[ObjectTypeDef]:
        return next((o for o in self.object_types if o.api_name == api_name), None)

    def link_type(self, api_name: str) -> Optional[LinkTypeDef]:
        return next((lt for lt in self.link_types if lt.api_name == api_name), None)

    def action(self, api_name: str) -> Optional[ActionDef]:
        return next((a for a in self.actions if a.api_name == api_name), None)


# ---------------------------------------------------------------------------
# Write-back (edit overlay) & audit
# ---------------------------------------------------------------------------

class EditKind(str, Enum):
    create = "create"
    update = "update"
    delete = "delete"


class ObjectEdit(BaseModel):
    id: str
    object_type: str
    pk_value: str
    kind: EditKind
    payload: dict[str, Any] = Field(default_factory=dict)
    actor: str = "anonymous"
    created_at: str = Field(default_factory=utcnow_iso)
    # Gapless per-type position in the edit log, allocated at append. 0 means
    # "not yet appended" — the value an in-memory edit carries before commit.
    edit_seq: int = 0


class AuditEvent(BaseModel):
    id: Optional[int] = None
    timestamp: str = Field(default_factory=utcnow_iso)
    actor: str = "anonymous"
    action: str
    details: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Authentication & authorization
# ---------------------------------------------------------------------------

class Role(str, Enum):
    """Ordered roles: viewer < editor < admin."""

    viewer = "viewer"
    editor = "editor"
    admin = "admin"

    @property
    def rank(self) -> int:
        return _ROLE_ORDER[self]

    def covers(self, required: "Role") -> bool:
        """True if this role grants at least ``required``'s privileges."""
        return self.rank >= required.rank


_ROLE_ORDER = {Role.viewer: 0, Role.editor: 1, Role.admin: 2}


class User(BaseModel):
    """A Laurelin account. The password hash is intentionally NOT part of this
    model so it can never leak through an API response.

    ``role`` is the account's role. In multi-workspace mode a user's *effective*
    role is per-workspace (from membership); ``role`` there is a baseline and
    ``superadmin`` marks a server administrator (manages workspaces + users and
    is admin in every workspace). In single-workspace mode ``superadmin`` is
    unused and ``role`` is the account's role directly.
    """

    id: str
    username: str
    role: Role = Role.viewer
    created_at: str = Field(default_factory=utcnow_iso)
    disabled: bool = False
    superadmin: bool = False


class WorkspaceInfo(BaseModel):
    """A workspace registered in the multi-workspace control plane."""

    slug: str
    name: str
    description: str = ""
    created_at: str = Field(default_factory=utcnow_iso)


class WorkspaceMembership(BaseModel):
    slug: str
    username: str
    role: Role = Role.viewer


# ---------------------------------------------------------------------------
# Groups & fine-grained ontology permissions
# ---------------------------------------------------------------------------

class GroupInfo(BaseModel):
    name: str
    members: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow_iso)


class SubjectKind(str, Enum):
    everyone = "everyone"  # any authenticated user
    role = "role"          # a global role name (viewer/editor/admin)
    group = "group"        # a named group
    user = "user"          # a specific username


class Grant(BaseModel):
    """A single access grant on an object type. ``subject`` is empty for
    ``everyone``, else the role name / group name / username."""

    subject_kind: SubjectKind
    subject: str = ""
    can_view: bool = False
    can_edit: bool = False  # edit implies view

    def normalized_subject(self) -> str:
        return self.subject.strip().lower() if self.subject_kind != SubjectKind.everyone else ""


class ObjectTypeGrants(BaseModel):
    object_type: str
    grants: list[Grant] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Row-level security & column masking (per dataset)
# ---------------------------------------------------------------------------

class PolicySubject(BaseModel):
    """A subject a row-rule or mask-exemption applies to."""
    subject_kind: SubjectKind
    subject: str = ""

    def normalized_subject(self) -> str:
        return self.subject.strip().lower() if self.subject_kind != SubjectKind.everyone else ""


class RowRule(PolicySubject):
    """Matching subjects may see rows whose policy column is in ``values``."""
    values: list[str] = Field(default_factory=list)


class RowPolicy(BaseModel):
    """Row-level security on a dataset: a non-admin user sees a row only if some
    rule matches them AND the row's ``column`` value is in that rule's values.
    A dataset with a row policy but no matching rule for the user => no rows."""
    column: str
    rules: list[RowRule] = Field(default_factory=list)


class MaskMode(str, Enum):
    null = "null"      # replace with NULL (keeps the column's type)
    redact = "redact"  # replace with "***"
    hash = "hash"      # replace with a sha256 prefix (stable pseudonym)


class ColumnMask(BaseModel):
    """Mask ``column`` for everyone except the exempt subjects (admins always
    see unmasked)."""
    column: str
    mode: MaskMode = MaskMode.redact
    exempt: list[PolicySubject] = Field(default_factory=list)


class DatasetPolicy(BaseModel):
    dataset: str
    row_policy: Optional[RowPolicy] = None
    column_masks: list[ColumnMask] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Classification markings (mandatory access control, propagated via lineage)
# ---------------------------------------------------------------------------

class Marking(BaseModel):
    name: str
    description: str = ""
    created_at: str = Field(default_factory=utcnow_iso)


class DatasetMarkings(BaseModel):
    dataset: str
    explicit: list[str] = Field(default_factory=list)   # admin-assigned
    effective: list[str] = Field(default_factory=list)  # explicit ∪ inherited via lineage


class UserClearances(BaseModel):
    username: str
    markings: list[str] = Field(default_factory=list)


class ObjectTypePermission(BaseModel):
    """The effective permission a specific user has on an object type."""

    can_view: bool
    can_edit: bool
