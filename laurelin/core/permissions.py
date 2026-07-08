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

from typing import Optional

from laurelin.core.db import MetadataStore
from laurelin.core.models import Grant, Role, SubjectKind, User


class PermissionService:
    def __init__(self, store: MetadataStore):
        self.store = store

    # -- groups ---------------------------------------------------------------

    def _user_groups(self, username: str) -> set[str]:
        return {g.lower() for g in self.store.groups_for_user(username)}

    # -- grant matching -------------------------------------------------------

    def _grant_matches(self, grant: Grant, user: User, groups: set[str]) -> bool:
        kind = grant.subject_kind
        if kind == SubjectKind.everyone:
            return True
        if kind == SubjectKind.role:
            return grant.normalized_subject() == user.role.value
        if kind == SubjectKind.user:
            return grant.normalized_subject() == user.username.lower()
        if kind == SubjectKind.group:
            return grant.normalized_subject() in groups
        return False

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

    def dataset_permission(self, user: Optional[User], dataset: str) -> tuple[bool, bool]:
        return self._evaluate(user, self._dataset_grants(dataset))

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
