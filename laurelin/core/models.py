"""Shared data models. These are the contracts between all Laurelin modules.

Every module (catalog, transforms, ontology, api) speaks in these types.
Keep this file dependency-light: pydantic only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------

class ColumnSchema(BaseModel):
    name: str
    type: str  # arrow type name, e.g. "string", "int64", "double", "timestamp[us]"


class DatasetInfo(BaseModel):
    name: str
    description: str = ""
    created_at: str = Field(default_factory=utcnow_iso)
    latest_version: Optional[int] = None
    # "managed": Laurelin owns the Parquet and versions it.
    # "federated": the bytes live elsewhere (Iceberg/Delta/Parquet/Postgres);
    # Laurelin governs the table and scans it in place, so it has no versions.
    kind: str = "managed"
    source: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_federated(self) -> bool:
        return self.kind == "federated"


class DatasetVersionInfo(BaseModel):
    dataset: str
    version: int
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
    """One saved query + presentation. Panels are executed client-side through
    the normal /query endpoint, so each viewer sees their own ACL/RLS-filtered
    view of the data — a dashboard adds no new read surface."""

    id: str
    title: str = ""
    sql: str
    chart: ChartKind = ChartKind.table
    # Column bindings (empty = infer: first text column as x, numeric as y).
    x: str = ""
    y: list[str] = Field(default_factory=list)
    width: int = Field(default=6, ge=1, le=12)  # 12-column grid


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
        return next((l for l in self.link_types if l.api_name == api_name), None)

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
