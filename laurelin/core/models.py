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


class DatasetVersionInfo(BaseModel):
    dataset: str
    version: int
    created_at: str = Field(default_factory=utcnow_iso)
    row_count: int = 0
    schema_: list[ColumnSchema] = Field(default_factory=list, alias="schema")
    path: str = ""  # workspace-relative directory of this version
    build_id: Optional[str] = None
    source: str = "upload"  # "upload" | "transform" | "api"

    model_config = ConfigDict(populate_by_name=True)


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
    model so it can never leak through an API response."""

    id: str
    username: str
    role: Role = Role.viewer
    created_at: str = Field(default_factory=utcnow_iso)
    disabled: bool = False


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


class ObjectTypePermission(BaseModel):
    """The effective permission a specific user has on an object type."""

    can_view: bool
    can_edit: bool
