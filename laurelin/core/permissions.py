"""Fine-grained ontology access control.

Each object type has a list of grants. The model is deliberately simple and
predictable:

- **Admins** always have full access (bypass).
- If an object type has **no grants**, it inherits the global RBAC default:
  any authenticated user may *view* it, and ``editor``+ may *edit* (apply
  actions on) it. This keeps existing workspaces working unchanged.
- If an object type has **any grants**, it is locked to that allowlist: a user
  may view/edit only if some grant matches them (by ``everyone``, their role,
  a group they belong to, or their username) with the needed capability. A
  global editor with no matching grant is denied — that is the point of adding
  grants. ``can_edit`` implies ``can_view``.

Grants can thus both *restrict* (hide a type from most users) and *elevate*
(let a specific viewer edit one type), all per object type.

**Scope — important.** These grants gate the *ontology layer* only. They do NOT
secure the backing dataset: a user denied an object type can still read the same
rows through ``/api/v1/query`` and ``/api/v1/datasets/{name}/rows`` (any viewer
can query any dataset). Ontology grants are a presentation/authoring control,
not data confidentiality. Dataset-level access control (and thereby true
data-hiding) is a separate, larger piece of work (see docs/ROADMAP.md, WS8).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Optional

import pyarrow as pa
import pyarrow.compute as pc

from laurelin.core.db import MetadataStore
from laurelin.core.models import (
    ColumnMask,
    DatasetPolicy,
    Grant,
    MaskMode,
    Role,
    RowPolicy,
    SubjectKind,
    User,
)


@dataclass(frozen=True)
class PolicyPlan:
    """How to enforce a dataset policy for one user on one scan.

    ``lazy`` plans push a filter and/or a projection into the Parquet scan;
    non-lazy plans materialize the table and call ``apply`` (the exact path,
    used where a rule has no Arrow equivalent).
    """

    lazy: bool
    filter: Any = None
    projection: Optional[dict] = None
    apply: Optional[Callable] = None


class PermissionService:
    def __init__(self, store: MetadataStore):
        self.store = store

    # -- groups ---------------------------------------------------------------

    def _user_groups(self, username: str) -> set[str]:
        return {g.lower() for g in self.store.groups_for_user(username)}

    # -- grant matching -------------------------------------------------------

    def _subject_matches(
        self, kind: SubjectKind, subject_norm: str, user: User, groups: set[str]
    ) -> bool:
        if kind == SubjectKind.everyone:
            return True
        if kind == SubjectKind.role:
            return subject_norm == user.role.value
        if kind == SubjectKind.user:
            return subject_norm == user.username.lower()
        if kind == SubjectKind.group:
            return subject_norm in groups
        return False

    def _grant_matches(self, grant: Grant, user: User, groups: set[str]) -> bool:
        return self._subject_matches(
            grant.subject_kind, grant.normalized_subject(), user, groups
        )

    def _evaluate(self, user: Optional[User], grants: list[Grant]) -> tuple[bool, bool]:
        """Core rule shared by ontology and dataset grants: admin bypass; no
        grants -> global RBAC default; any grant -> allowlist."""
        if user is None:
            return (False, False)
        if user.role == Role.admin:
            return (True, True)
        if not grants:
            return (True, user.role.covers(Role.editor))
        groups = self._user_groups(user.username)
        can_view = can_edit = False
        for g in grants:
            if not self._grant_matches(g, user, groups):
                continue
            if g.can_edit:
                can_edit = True
                can_view = True
            elif g.can_view:
                can_view = True
        return (can_view, can_edit)

    # -- object-type (ontology) permissions -----------------------------------

    def _grants(self, object_type: str) -> list[Grant]:
        return [Grant(**g) for g in self.store.grants_for_type(object_type)]

    def permission(self, user: Optional[User], object_type: str) -> tuple[bool, bool]:
        """Return ``(can_view, can_edit)`` for ``user`` on the ONTOLOGY grants of
        ``object_type`` (not composed with the backing dataset — use
        ``object_type_permission`` for the effective access)."""
        return self._evaluate(user, self._grants(object_type))

    def can_view(self, user: Optional[User], object_type: str) -> bool:
        return self.permission(user, object_type)[0]

    def can_edit(self, user: Optional[User], object_type: str) -> bool:
        return self.permission(user, object_type)[1]

    # -- dataset permissions --------------------------------------------------

    def _dataset_grants(self, dataset: str) -> list[Grant]:
        return [Grant(**g) for g in self.store.grants_for_dataset(dataset)]

    def _has_clearance(self, user: Optional[User], dataset: str) -> bool:
        """Mandatory access control: a non-admin must hold clearance for every
        (effective, lineage-propagated) marking on the dataset. Admins/superadmins
        are the data stewards and bypass markings (so they can't lock themselves
        out); markings gate editors and viewers."""
        if user is None:
            return False
        if user.role == Role.admin:
            return True
        needed = set(self.store.get_effective_markings(dataset))
        if not needed:
            return True
        return needed <= set(self.store.get_clearances(user.username))

    def dataset_permission(self, user: Optional[User], dataset: str) -> tuple[bool, bool]:
        # Discretionary ACL, then MANDATORY markings override (deny if uncleared).
        view, edit = self._evaluate(user, self._dataset_grants(dataset))
        if not self._has_clearance(user, dataset):
            return (False, False)
        return (view, edit)

    def can_view_dataset(self, user: Optional[User], dataset: str) -> bool:
        return self.dataset_permission(user, dataset)[0]

    def can_edit_dataset(self, user: Optional[User], dataset: str) -> bool:
        return self.dataset_permission(user, dataset)[1]

    def viewable_datasets(self, user: Optional[User], names: list[str]) -> set[str]:
        return {n for n in names if self.can_view_dataset(user, n)}

    # -- composed object-type access ------------------------------------------

    def object_type_permission(
        self, user: Optional[User], object_type: str, backing_dataset: str
    ) -> tuple[bool, bool]:
        """Effective access to an object type = the ontology grant composed with
        the backing dataset's access. You must be able to view the backing
        dataset to view its objects (objects ARE the dataset rows), so this
        closes the gap where an ontology grant alone left the data readable via
        the dataset/query APIs. Ontology edit still needs ontology edit rights,
        but also requires view (which requires dataset view)."""
        dv, _ = self.dataset_permission(user, backing_dataset)
        ov, oe = self.permission(user, object_type)
        view = dv and ov
        edit = view and oe
        return (view, edit)

    # -- row-level security & column masking ----------------------------------

    def dataset_policy(self, dataset: str) -> Optional[DatasetPolicy]:
        raw = self.store.get_dataset_policy(dataset)
        if not raw:
            return None
        return DatasetPolicy(
            dataset=dataset,
            row_policy=raw.get("row_policy"),
            column_masks=raw.get("column_masks", []),
        )

    def apply_table_policy(
        self, user: Optional[User], dataset: str, table: "pa.Table"
    ) -> "pa.Table":
        """Filter rows and mask columns of ``table`` for ``user`` per the dataset's
        policy. Admins (and no-policy datasets) pass through unchanged. This is the
        single choke point applied by the row API, the SQL workbench, and ontology
        object materialization — so RLS can't be bypassed through any read path."""
        if user is None:
            return table.slice(0, 0)
        if user.role == Role.admin:
            return table
        policy = self.dataset_policy(dataset)
        if policy is None:
            return table
        groups = self._user_groups(user.username)
        if policy.row_policy is not None:
            table = self._filter_rows(table, policy.row_policy, user, groups)
        for mask in policy.column_masks:
            table = self._mask_column(table, mask, user, groups)
        return table

    def has_dataset_policy(self, dataset: str) -> bool:
        return self.store.get_dataset_policy(dataset) is not None

    def row_policy_fn(self, user: Optional[User], dataset: str):
        """A ``(table)->table`` filter for the row API, or None when nothing needs
        filtering (admin, or the dataset has no policy) so the caller can fast-path."""
        if user is None or user.role == Role.admin:
            return None
        if not self.has_dataset_policy(dataset):
            return None
        return lambda t: self.apply_table_policy(user, dataset, t)

    def query_policy_fn(self, user: Optional[User]):
        """A ``(dataset, table)->table`` filter for the SQL workbench / ontology,
        or None for admins (no filtering)."""
        if user is not None and user.role == Role.admin:
            return None
        return lambda ds, t: self.apply_table_policy(user, ds, t)

    def per_dataset_policy_fn(self, user: Optional[User]):
        """A ``(dataset) -> Optional[(table)->table]`` resolver for the SQL
        workbench: None per dataset means no filtering is needed, so the
        catalog can register it as a lazy (out-of-core) scan instead of
        materializing it through the policy."""
        if user is not None and user.role == Role.admin:
            return lambda ds: None

        def for_dataset(ds: str):
            if user is None:
                return lambda t: t.slice(0, 0)  # fail closed
            if not self.has_dataset_policy(ds):
                return None
            return lambda t: self.apply_table_policy(user, ds, t)

        return for_dataset

    # -- pushdown planning ------------------------------------------------------
    #
    # The policy engine above operates on materialized tables, which costs
    # 3-4x at multi-million-row scale because it defeats scan pushdown. Where a
    # policy can be expressed as an Arrow filter + projection, it is applied
    # *inside* the Parquet scan instead: same enforcement, no materialization.
    # Anything not expressible (currently: hash masking, which has no Arrow
    # compute equivalent) reports "materialize" and takes the exact path.

    def arrow_policy_fn(self, user: Optional[User]):
        """A ``(dataset, arrow_schema) -> plan`` resolver, where plan is:

        * ``None`` — no filtering needed; scan the dataset as-is.
        * ``PolicyPlan(lazy=True, filter=expr|None, projection=dict|None)`` —
          push this filter/projection into the scan.
        * ``PolicyPlan(lazy=False, apply=fn)`` — materialize and call ``fn``.
        """
        if user is not None and user.role == Role.admin:
            return lambda ds, schema: None

        def plan_for(dataset: str, schema: "pa.Schema"):
            if user is None:
                return PolicyPlan(lazy=True, filter=pc.scalar(False))  # fail closed
            policy = self.dataset_policy(dataset)
            if policy is None:
                return None
            return self._plan(policy, dataset, schema, user)

        return plan_for

    def _plan(self, policy, dataset: str, schema: "pa.Schema", user: User):
        groups = self._user_groups(user.username)
        names = set(schema.names)

        filter_expr = None
        if policy.row_policy is not None:
            rp = policy.row_policy
            if rp.column not in names:
                return PolicyPlan(lazy=True, filter=pc.scalar(False))  # fail closed
            allowed: set[str] = set()
            for rule in rp.rules:
                if self._subject_matches(
                    rule.subject_kind, rule.normalized_subject(), user, groups
                ):
                    allowed.update(str(v) for v in rule.values)
            if not allowed:
                return PolicyPlan(lazy=True, filter=pc.scalar(False))
            # NULLs are never "in" the set, so they are excluded — matching the
            # fill_null(False) fail-closed behavior of the table path.
            field = pc.field(rp.column)
            if schema.field(rp.column).type != pa.string():
                # Compare on the string rendering, exactly as the table path
                # does. This costs row-group pruning, but only for non-string
                # policy columns (tenant/region keys are normally strings).
                field = field.cast(pa.string())
            filter_expr = field.isin(sorted(allowed))

        projection = None
        for mask in policy.column_masks:
            if mask.column not in names:
                continue
            if any(
                self._subject_matches(ex.subject_kind, ex.normalized_subject(), user, groups)
                for ex in mask.exempt
            ):
                continue  # exempt: real value
            if mask.mode == MaskMode.hash:
                # No Arrow compute equivalent for sha256; stay exact.
                return PolicyPlan(
                    lazy=False,
                    apply=lambda t: self.apply_table_policy(user, dataset, t),
                )
            if projection is None:
                projection = {n: pc.field(n) for n in schema.names}
            if mask.mode == MaskMode.null:
                projection[mask.column] = pc.scalar(None).cast(
                    schema.field(mask.column).type
                )
            else:  # redact
                projection[mask.column] = pc.scalar("***")

        if filter_expr is None and projection is None:
            return None
        return PolicyPlan(lazy=True, filter=filter_expr, projection=projection)

    def _filter_rows(
        self, table: "pa.Table", rp: RowPolicy, user: User, groups: set[str]
    ) -> "pa.Table":
        if rp.column not in table.column_names:
            return table.slice(0, 0)  # fail closed if the policy column is missing
        allowed: set[str] = set()
        for rule in rp.rules:
            if self._subject_matches(rule.subject_kind, rule.normalized_subject(), user, groups):
                allowed.update(str(v) for v in rule.values)
        if not allowed:
            return table.slice(0, 0)  # policy present but no rule grants this user rows
        col_as_str = pc.cast(table.column(rp.column), pa.string())
        mask = pc.is_in(col_as_str, value_set=pa.array(sorted(allowed), pa.string()))
        # NULLs in the policy column are never "in" the set -> excluded (fail closed).
        mask = pc.fill_null(mask, False)
        return table.filter(mask)

    def _mask_column(
        self, table: "pa.Table", mask: ColumnMask, user: User, groups: set[str]
    ) -> "pa.Table":
        if mask.column not in table.column_names:
            return table
        for ex in mask.exempt:
            if self._subject_matches(ex.subject_kind, ex.normalized_subject(), user, groups):
                return table  # this user is exempt -> see the real value
        idx = table.column_names.index(mask.column)
        col = table.column(mask.column)
        n = len(col)
        if mask.mode == MaskMode.null:
            new = pa.nulls(n, type=col.type)
            field = table.schema.field(idx)
        elif mask.mode == MaskMode.redact:
            new = pa.array(["***"] * n, pa.string())
            field = pa.field(mask.column, pa.string())
        else:  # hash
            vals = col.to_pylist()
            new = pa.array(
                [
                    None if v is None else hashlib.sha256(str(v).encode()).hexdigest()[:16]
                    for v in vals
                ],
                pa.string(),
            )
            field = pa.field(mask.column, pa.string())
        return table.set_column(idx, field, new)

    # -- validation for the management API ------------------------------------

    def validate_grants(self, grants: list[Grant]) -> None:
        """Reject grants that reference unknown roles/groups or malformed rows.
        Raises ValueError with a clear message."""
        for g in grants:
            if not (g.can_view or g.can_edit):
                raise ValueError(
                    f"Grant for {g.subject_kind.value}:{g.subject!r} grants nothing "
                    "(set can_view and/or can_edit)"
                )
            if g.subject_kind == SubjectKind.everyone:
                continue
            subj = g.subject.strip()
            if not subj:
                raise ValueError(f"Grant of kind {g.subject_kind.value} needs a subject")
            if g.subject_kind == SubjectKind.role:
                if subj.lower() not in {r.value for r in Role}:
                    raise ValueError(f"Unknown role in grant: {subj!r}")
            elif g.subject_kind == SubjectKind.group:
                if not self.store.group_exists(subj):
                    raise ValueError(f"Unknown group in grant: {subj!r}")
            elif g.subject_kind == SubjectKind.user:
                if self.store.get_user(subj) is None:
                    raise ValueError(f"Unknown user in grant: {subj!r}")
