"""Loosening vs tightening, computed exactly — the comparator behind approvals.

The policy language is closed and declarative: ``Grant`` is four booleans over
a finite subject vocabulary, ``RowRule`` carries literal value lists, a
``ColumnMask`` is a mode enum plus exempt subjects, markings and clearances are
finite sets, and roles are a three-element total order. Every capability any
principal holds is therefore a pure function of those tables plus group
membership — so "does this change widen anyone's access?" has an exact answer,
and this module computes it by evaluating the **capability tuple before and
after** the proposed change for every enabled non-admin user. Admins are
skipped because nothing can widen an admin: they bypass grants and markings
(``permissions.py`` ``_evaluate``/``_has_clearance``).

**A change LOOSENS iff some user's after-tuple strictly exceeds their
before-tuple in any dimension** — gains view, gains edit, gains a row value, a
masked column becomes unmasked, a needed marking disappears. Otherwise it
tightens; a no-op diff is neutral and classifies with tightening (it applies
immediately, with a record).

Fail-closed rule for incomparables: only *provable* non-widenings classify as
tightening. A row-policy ``column`` change makes different rows visible and is
not comparable value-wise; any mask ``mode`` change (``redact -> hash`` reveals
stable equality classes, ``null -> redact`` changes type visibility) refuses a
mode lattice and classifies as loosening with ``reason="incomparable"``.

Where the evaluation duplicates :mod:`laurelin.core.permissions` it is because
the real evaluator reads group membership from the store, and half of the
questions here are about a *shadow* membership ("what would bob see if he were
in this group?"). Subject matching itself is NOT duplicated —
``PermissionService._subject_matches`` is called with the shadow group set —
so a divergence in who a grant names is impossible; only the fold over grants
is restated, with the same admin-bypass / empty-means-RBAC-default /
edit-implies-view rules, and ``tests/test_approvals.py`` pins the sharp edges
(emptying a list loosens, a shrunken list can still loosen edit).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from laurelin.core.db import MetadataStore, propagate_markings
from laurelin.core.models import Grant, Role, SubjectKind, User


@dataclass
class Diff:
    """The comparator's verdict on one proposed change."""

    classification: str  # "loosening" | "tightening"
    gains: list[str] = field(default_factory=list)  # per-principal capability gains
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "classification": self.classification,
            "gains": self.gains,
            "reason": self.reason,
        }


def _verdict(gains: list[str], reason: str = "") -> Diff:
    if gains:
        return Diff(classification="loosening", gains=gains, reason=reason)
    return Diff(classification="tightening", gains=[], reason=reason)


def _grants(raw: list[dict]) -> list[Grant]:
    return [g if isinstance(g, Grant) else Grant(**g) for g in raw or []]


def _eval_grants(perms, user: User, groups: set[str], grants: list[Grant]) -> tuple[bool, bool]:
    """``PermissionService._evaluate`` with the group set as an argument.

    Restated (not called) because the real one reads membership from the store
    and the group-membership comparator needs to ask about a membership that
    does not exist yet. Subject matching is delegated to the real
    ``_subject_matches`` so the two can never disagree about who a grant names.
    """
    if user.role == Role.admin:
        return (True, True)
    if not grants:
        # Empty list ⇒ global RBAC default (permissions.py:352-353). This is
        # why "emptying a grant list" is a loosening the per-user evaluation
        # catches and a row count would not.
        return (True, user.role.covers(Role.editor))
    can_view = can_edit = False
    for g in grants:
        if not perms._subject_matches(g.subject_kind, g.normalized_subject(), user, groups):
            continue
        if g.can_edit:
            can_edit = True
            can_view = True
        elif g.can_view:
            can_view = True
    return (can_view, can_edit)


def _candidates(users: list[User]) -> list[User]:
    """Whose capabilities a change could widen: every non-admin.

    Disabled users are INCLUDED, deliberately. The disabled flag is reversible
    state that ``update_user`` flips through ``identity_ticket`` — a
    ``kind="tightening"`` write that is exempt from classification and never
    queues (approvals.py). So a grant/clearance/membership that names only a
    disabled subject is latent loosening: it applies with no gain today, then
    the account is re-enabled with no re-classification and the access is live,
    walking straight past second-approver review. Classifying against the
    account's *policy* capability (as if enabled) closes that door — the write
    queues now, when the loosening is decided, not silently at enable time.
    Only admins are skipped: nothing can widen an admin (permissions.py
    _evaluate/_has_clearance bypass). Including disabled users can only ADD
    detected gains, never hide one, so the change is strictly fail-closed and
    leaves the wire behaviour of genuine tightenings unchanged.
    """
    return [u for u in users if u.role != Role.admin]


def _row_state(perms, user: User, groups: set[str], policy: Optional[dict]):
    """(column, frozenset(values)) the user may see, or ("*", None) = all rows."""
    rp = (policy or {}).get("row_policy")
    if not rp:
        return ("*", None)
    allowed: set[str] = set()
    for rule in rp.get("rules", []):
        kind = SubjectKind(rule.get("subject_kind"))
        subj = (rule.get("subject") or "").strip().lower() if kind != SubjectKind.everyone else ""
        if perms._subject_matches(kind, subj, user, groups):
            allowed.update(str(v) for v in rule.get("values", []))
    return (rp.get("column", ""), frozenset(allowed))


def _mask_state(perms, user: User, groups: set[str], policy: Optional[dict]) -> dict[str, str]:
    """column -> mode for every mask that applies to this user (exemption-aware)."""
    out: dict[str, str] = {}
    for mask in (policy or {}).get("column_masks", []):
        exempt = False
        for ex in mask.get("exempt", []):
            kind = SubjectKind(ex.get("subject_kind"))
            subj = (ex.get("subject") or "").strip().lower() if kind != SubjectKind.everyone else ""
            if perms._subject_matches(kind, subj, user, groups):
                exempt = True
                break
        if not exempt:
            out[mask.get("column", "")] = str(mask.get("mode", "redact"))
    return out


def _policy_gains(
    who: str,
    before_row, after_row,
    before_masks: dict[str, str], after_masks: dict[str, str],
) -> tuple[list[str], str]:
    gains: list[str] = []
    reason = ""
    bcol, bvals = before_row
    acol, avals = after_row
    if bcol == "*" and acol != "*":
        pass  # rows newly restricted: tightening
    elif bcol != "*" and acol == "*":
        gains.append(f"{who} row filter removed (all rows become visible)")
    elif bcol != "*" and acol != "*":
        if bcol != acol:
            gains.append(f"row policy column changes {bcol!r} -> {acol!r}")
            reason = "incomparable"
        elif not avals <= bvals:
            extra = sorted(avals - bvals)
            gains.append(f"{who} gains row values {extra[:5]} on {acol!r}")
    for col, mode in before_masks.items():
        after = after_masks.get(col)
        if after is None:
            gains.append(f"{who} sees column {col!r} unmasked")
        elif after != mode:
            gains.append(f"mask mode on {col!r} changes {mode} -> {after}")
            reason = "incomparable"
    return gains, reason


def _clearance_ok(store: MetadataStore, username: str, needed: set[str]) -> bool:
    return needed <= set(store.get_clearances(username))


# ---------------------------------------------------------------------------
# Per-kind comparators
# ---------------------------------------------------------------------------

def _diff_dataset_grants(store, perms, users, target, payload) -> Diff:
    before = _grants(store.grants_for_dataset(target))
    after = _grants(payload.get("grants", []))
    gains: list[str] = []
    for u in _candidates(users):
        if not perms._has_clearance(u, target):
            continue  # mandatory markings deny regardless of the grant change
        groups = perms._user_groups(u.username)
        bv, be = _eval_grants(perms, u, groups, before)
        av, ae = _eval_grants(perms, u, groups, after)
        if av and not bv:
            gains.append(f"{u.username} gains view on dataset {target!r}")
        if ae and not be:
            gains.append(f"{u.username} gains edit on dataset {target!r}")
    return _verdict(gains)


def _diff_ontology_grants(store, perms, users, target, payload) -> Diff:
    before = _grants(store.grants_for_type(target))
    after = _grants(payload.get("grants", []))
    gains: list[str] = []
    for u in _candidates(users):
        groups = perms._user_groups(u.username)
        bv, be = _eval_grants(perms, u, groups, before)
        av, ae = _eval_grants(perms, u, groups, after)
        if av and not bv:
            gains.append(f"{u.username} gains view on object type {target!r}")
        if ae and not be:
            gains.append(f"{u.username} gains edit on object type {target!r}")
    return _verdict(gains)


def _diff_dataset_policy(store, perms, users, target, payload) -> Diff:
    before = store.get_dataset_policy(target)
    after = payload.get("policy")
    gains: list[str] = []
    reason = ""
    for u in _candidates(users):
        groups = perms._user_groups(u.username)
        g, r = _policy_gains(
            u.username,
            _row_state(perms, u, groups, before),
            _row_state(perms, u, groups, after),
            _mask_state(perms, u, groups, before),
            _mask_state(perms, u, groups, after),
        )
        gains.extend(g)
        reason = reason or r
    return _verdict(gains, reason)


def _marking_shadow(store, explicit_after: dict[str, set[str]]):
    """Effective (enforced) marking sets before and after, via the one closure.

    ``propagate_markings`` is the same pure function the store and the importer
    run — a second implementation of *which markings apply* is the one
    duplication a governance layer cannot afford, so there is none here either.
    Its result includes each dataset's explicit set, which is exactly the
    enforced union the permission check reads.
    """
    datasets = [d.name for d in store.list_datasets()]
    edges = [(e.upstream_dataset, e.downstream_dataset) for e in store.list_lineage()]
    nodes = set(datasets) | {u for u, _ in edges} | {d for _, d in edges}
    explicit_before = {d: set(store.get_explicit_markings(d)) for d in nodes}
    shadow = {d: set(explicit_after.get(d, explicit_before[d])) for d in nodes}
    before = propagate_markings(datasets, edges, explicit_before)
    after = propagate_markings(datasets, edges, shadow)
    return datasets, before, after


def _marking_gains(store, perms, users, datasets, eff_before, eff_after) -> list[str]:
    gains: list[str] = []
    for u in _candidates(users):
        clear = set(store.get_clearances(u.username))
        groups = perms._user_groups(u.username)
        for ds in datasets:
            nb = eff_before.get(ds, set())
            na = eff_after.get(ds, set())
            if na == nb:
                continue
            ok_b = nb <= clear
            ok_a = na <= clear
            if ok_a and not ok_b:
                view, _ = _eval_grants(perms, u, groups, _grants(store.grants_for_dataset(ds)))
                if view:
                    gone = sorted(nb - na)
                    gains.append(
                        f"{u.username} gains view on dataset {ds!r} "
                        f"(marking {', '.join(gone) or '?'} no longer required)"
                    )
    return gains


def _diff_dataset_markings(store, perms, users, target, payload) -> Diff:
    wanted = {m.lower() for m in payload.get("markings", [])}
    datasets, before, after = _marking_shadow(store, {target: wanted})
    return _verdict(_marking_gains(store, perms, users, datasets, before, after))


def _diff_marking_delete(store, perms, users, target, payload) -> Diff:
    name = target.lower()
    explicit_after = {
        d.name: set(store.get_explicit_markings(d.name)) - {name}
        for d in store.list_datasets()
    }
    datasets, before, after = _marking_shadow(store, explicit_after)
    # Deletion is workspace-wide: the marking stops being required everywhere
    # it was attached or inherited, all at once.
    return _verdict(_marking_gains(store, perms, users, datasets, before, after))


def _diff_clearances(store, perms, users, target, payload) -> Diff:
    username = target.lower()
    user = next((u for u in users if u.username == username), None) or store.get_user(username)
    if user is None or user.role == Role.admin:
        return _verdict([], reason="user absent or admin (marking bypass)")
    # A disabled user is NOT skipped here: the account can be re-enabled by an
    # ungated identity write, so a clearance added while disabled is latent
    # loosening (see _candidates). Classify against policy capability regardless
    # of the disabled flag.
    new_clear = {m.lower() for m in payload.get("markings", [])}
    cur_clear = set(store.get_clearances(username))
    groups = perms._user_groups(username)
    gains: list[str] = []
    for d in store.list_datasets():
        needed = set(store.get_enforced_markings(d.name))
        if not needed:
            continue
        if needed <= new_clear and not needed <= cur_clear:
            view, _ = _eval_grants(perms, user, groups, _grants(store.grants_for_dataset(d.name)))
            if view:
                gains.append(f"{username} gains view on dataset {d.name!r} (clearance added)")
    return _verdict(gains)


def _group_referenced(store, name: str) -> bool:
    name = name.lower()
    for g in store.list_dataset_grants():
        if g["subject_kind"] == "group" and g["subject"].strip().lower() == name:
            return True
    for g in store.list_grants():
        if g["subject_kind"] == "group" and g["subject"].strip().lower() == name:
            return True
    for policy in store.list_dataset_policies().values():
        rp = policy.get("row_policy") or {}
        for rule in rp.get("rules", []):
            if rule.get("subject_kind") == "group" and (rule.get("subject") or "").strip().lower() == name:
                return True
        for mask in policy.get("column_masks", []):
            for ex in mask.get("exempt", []):
                if ex.get("subject_kind") == "group" and (ex.get("subject") or "").strip().lower() == name:
                    return True
    return False


def _diff_group_members(store, perms, users, target, payload) -> Diff:
    name = target.lower()
    current = set()
    for g in store.list_groups():
        if g["name"] == name:
            current = {m.lower() for m in g.get("members", [])}
    new = {m.lower() for m in payload.get("members", [])}
    changed = current ^ new
    if not changed:
        return _verdict([], reason="membership unchanged")
    if not _group_referenced(store, name):
        # No grant, row rule or mask exemption names this group: the change
        # cannot move any capability. Neutral ⇒ applies with a record.
        return _verdict([], reason="group referenced by no grant, rule or exemption")
    by_name = {u.username: u for u in _candidates(users)}
    grants_by_ds: dict[str, list[dict]] = {}
    for g in store.list_dataset_grants():
        grants_by_ds.setdefault(g["dataset"], []).append(g)
    grants_by_type: dict[str, list[dict]] = {}
    for g in store.list_grants():
        grants_by_type.setdefault(g["object_type"], []).append(g)
    policies = store.list_dataset_policies()
    gains: list[str] = []
    for username in sorted(changed):
        u = by_name.get(username)
        if u is None:
            continue
        groups_b = {g.lower() for g in store.groups_for_user(username)}
        groups_a = (groups_b | {name}) if username in new else (groups_b - {name})
        for ds, raw in grants_by_ds.items():
            if not perms._has_clearance(u, ds):
                continue
            gl = _grants(raw)
            bv, be = _eval_grants(perms, u, groups_b, gl)
            av, ae = _eval_grants(perms, u, groups_a, gl)
            if av and not bv:
                gains.append(f"{username} gains view on dataset {ds!r} via group {name!r}")
            if ae and not be:
                gains.append(f"{username} gains edit on dataset {ds!r} via group {name!r}")
        for ot, raw in grants_by_type.items():
            gl = _grants(raw)
            bv, be = _eval_grants(perms, u, groups_b, gl)
            av, ae = _eval_grants(perms, u, groups_a, gl)
            if av and not bv:
                gains.append(f"{username} gains view on object type {ot!r} via group {name!r}")
            if ae and not be:
                gains.append(f"{username} gains edit on object type {ot!r} via group {name!r}")
        for ds, policy in policies.items():
            g, _ = _policy_gains(
                username,
                _row_state(perms, u, groups_b, policy),
                _row_state(perms, u, groups_a, policy),
                _mask_state(perms, u, groups_b, policy),
                _mask_state(perms, u, groups_a, policy),
            )
            gains.extend(f"{s} (dataset {ds!r}, via group {name!r})" for s in g)
    return _verdict(gains)


def _diff_group_delete(store, perms, users, target, payload) -> Diff:
    return _diff_group_members(store, perms, users, target, {"members": []})


def _diff_user_role(store, perms, users, target, payload) -> Diff:
    username = target.lower()
    new_role = Role(payload["role"])
    existing = next((u for u in users if u.username == username), None)
    if existing is None and hasattr(store, "get_user"):
        existing = store.get_user(username)
    before_rank = existing.role.rank if existing is not None else -1
    if new_role.rank > before_rank:
        was = existing.role.value if existing is not None else "(no account)"
        return Diff(
            classification="loosening",
            gains=[f"{username} role rises {was} -> {new_role.value}"],
        )
    return _verdict([])


def _diff_workspace_member(store, perms, users, target, payload) -> Diff:
    slug = payload["slug"]
    username = payload["username"].lower()
    new_role = Role(payload["role"])
    before = store.member_role(slug, username)
    before_rank = before.rank if before is not None else -1
    if new_role.rank > before_rank:
        was = before.value if before is not None else "(not a member)"
        return Diff(
            classification="loosening",
            gains=[f"{username} workspace {slug!r} role rises {was} -> {new_role.value}"],
        )
    return _verdict([])


def _diff_approval_settings(store, perms, users, target, payload) -> Diff:
    if not payload.get("require_second_approver", False):
        # Turning review off is a governance loosening by definition: it
        # removes the requirement this very comparator feeds. It queues under
        # the regime it is disabling.
        return Diff(
            classification="loosening",
            gains=["second-approver review would be disabled for every future loosening"],
        )
    return _verdict([])


_COMPARATORS = {
    "dataset_grants": _diff_dataset_grants,
    "ontology_grants": _diff_ontology_grants,
    "dataset_policy": _diff_dataset_policy,
    "dataset_markings": _diff_dataset_markings,
    "marking_delete": _diff_marking_delete,
    "clearances": _diff_clearances,
    "group_members": _diff_group_members,
    "group_delete": _diff_group_delete,
    "user_role": _diff_user_role,
    "workspace_member": _diff_workspace_member,
    "approval_settings": _diff_approval_settings,
    # "import" records are filed by the import ceremony after its own digest
    # confirmation; they are never classified here.
}


def classify(store, perms, users: list[User], kind: str, target: str, payload: dict) -> Diff:
    """The comparator: exact where the language is closed, fail-closed elsewhere.

    Runs at file time AND again at apply time (approval staleness re-check).
    Cost is O(users x affected objects) over small in-memory tables — the same
    evaluators the request path already runs per request.
    """
    comparator = _COMPARATORS.get(kind)
    if comparator is None:
        # Fail closed: an unknown change kind cannot be shown not to widen.
        return Diff(classification="loosening", gains=[], reason=f"unknown change kind {kind!r}")
    return comparator(store, perms, users, target, payload)
