"""Change approval for governance writes: one chokepoint, a record for everything.

The gate is **the ``MetadataStore`` governance-write methods themselves**: each
one demands a :class:`ChangeTicket` as a required keyword argument, so every
caller — REST route, MCP tool (which calls the routes), SCIM push, flow
governance, CLI — must declare under which authority it is writing. Route
decorators were rejected deliberately: ``restrict_output_to_author`` and any
future service caller never pass through a route, and a guard on one path that
is absent on the next is this codebase's recurring wound.

Construction of tickets is the greppable escape hatch, policed the same way
``serialize.as_author`` is: ``tests/test_approvals.py`` asserts by AST that
``ChangeTicket(`` appears only in this module, and that each factory below is
called only from the module its docstring names.

Posture (single-admin behaviour is a decision, not an accident):

* **Default: record-and-self-approve.** Every loosening files a proposal
  record and auto-approves it in the same call (``self_approved``,
  ``decided_by = proposer``). The record *is* the product even when
  self-approved; wire behaviour of every existing route is unchanged (200,
  applied) and a one-person workspace can never deadlock.
* **Second-approver mode: opt-in** (``workspace_settings`` key ``approvals``).
  Loosenings queue (202 + proposal id); the approver must differ from the
  proposer, compared by user id. Enabling refuses unless >= 2 active enabled
  admins exist; disabling is itself a loosening and queues under the regime it
  is disabling.
* **Tightenings always apply immediately** — incident containment is never
  queued; a 3am mask addition needs nobody. They still file an auto-approved
  record so the inbox shows the whole history.
* **no_auth mode** auto-approves with ``kind="local"``: anything else
  deadlocks the only local mode of operation.
* **Control-plane writes in multi-workspace mode** (workspace membership, user
  roles) file records in the control store, where second-approver mode is
  never enabled — the superadmin tier is above workspace admins and its
  ceremony is the record, not the queue. In single mode identity lives in the
  workspace store and role changes queue like everything else.

**Scope boundary, so approvals are not oversold**: a local process with
filesystem access opens ``MetadataStore`` directly (the CLI does; metadata.db
is 0600 per task #51). No in-process gate constrains a hostile local operator.
Approvals govern the network surface and the honest-operator record; they are
not a defense against root.

**SCIM composition risk, accepted knowingly and recorded**: configuring
``LAURELIN_SCIM_TOKEN`` is the admin's standing authorization for IdP-driven
membership, so SCIM group pushes auto-approve (``kind="scim_sync"``) — an IdP
push can widen access through an existing group grant with no queued approval.
The auto-filed proposal record makes it visible after the fact; queueing would
break push semantics.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal, Optional

from laurelin.core import policy_diff
from laurelin.core.models import Role, User, utcnow_iso
from laurelin.core.policy_diff import Diff

SETTINGS_KEY = "approvals"


@dataclass(frozen=True)
class ChangeTicket:
    """The declaration every governance store write must carry.

    ``kind`` says under which authority the write happens; ``proposal_id``
    links the write to its record when one exists. The store method's only job
    is to *demand* the ticket — classification and queueing live above it, in
    :class:`ApprovalService`.
    """

    kind: Literal[
        "approved",          # a second admin approved the proposal
        "self_approved",     # single-admin posture: applied with a record
        "tightening",        # provably non-widening; applies immediately
        "import_confirmed",  # the import ceremony's digest confirmation
        "scim_sync",         # IdP push under the standing SCIM token
        "local",             # no_auth / CLI: filesystem possession is the credential
    ]
    actor: str
    proposal_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Ticket factories — each names its sole legitimate caller and is policed by AST
# ---------------------------------------------------------------------------

def local_ticket(actor: str) -> ChangeTicket:
    """CLI / no_auth writes. Possession of the workspace directory (or the
    server's ``--no-auth`` flag) is the stated credential; see the scope
    boundary in the module docstring. Called from ``laurelin/cli.py`` only."""
    return ChangeTicket(kind="local", actor=actor)


def scim_ticket() -> ChangeTicket:
    """IdP-driven writes under ``LAURELIN_SCIM_TOKEN`` — the standing
    authorization. Called from ``laurelin/api/scim_routes.py`` only."""
    return ChangeTicket(kind="scim_sync", actor="scim")


def identity_ticket(actor: str) -> ChangeTicket:
    """Non-role identity writes (password, disabled flag): exempt from
    approval — neither hands anyone a capability their role did not already
    carry. Called from ``laurelin/api/auth_routes.py`` only."""
    return ChangeTicket(kind="tightening", actor=actor)


def flow_output_ticket(store, dataset: str, author: str) -> ChangeTicket:
    """The flow-build author grant, re-verified rather than trusted.

    ``restrict_output_to_author`` writes an author-only grant and early-outs
    when any grant exists — provably tightening. This factory re-checks that
    precondition cheaply at write time so a future caller cannot borrow the
    ticket for a write that is not the one proven. Called from
    ``laurelin/transforms/flow_governance.py`` only.
    """
    if store.grants_for_dataset(dataset):
        raise ValueError(
            f"flow_output_ticket: {dataset!r} already has grants; the author "
            "grant is only provably tightening on an ungranted output"
        )
    return ChangeTicket(kind="tightening", actor=author)


def file_record(
    store,
    *,
    kind: str,
    target: str,
    payload: dict,
    proposer: str,
    proposer_id: str = "",
    rationale: str = "",
    state: str = "approved",
    classification: str = "loosening",
    diff: Optional[dict] = None,
    ticket_kind: str = "",
    decided_by: str = "",
    applied_at: Optional[str] = None,
) -> str:
    """File one proposal record and return its id.

    Used by :class:`ApprovalService` for every submitted change, by the SCIM
    router for its auto-approved sync records, and by the import path for its
    ``kind="import"`` record alongside the digest ceremony.
    """
    pid = "p_" + uuid.uuid4().hex[:12]
    now = utcnow_iso()
    store.create_proposal(
        {
            "id": pid,
            "kind": kind,
            "target": target,
            "payload": payload,
            "diff": diff or {},
            "rationale": rationale,
            "proposer": proposer,
            "proposer_id": proposer_id,
            "created_at": now,
            "state": state,
            "classification": classification,
            "ticket_kind": ticket_kind,
            "decided_by": decided_by if state != "pending" else "",
            "decided_at": now if state != "pending" else None,
            "applied_at": applied_at,
        }
    )
    return pid


class ApprovalError(RuntimeError):
    """A refusal in the approval flow; routes map it to HTTP 409."""

    def __init__(self, detail: str, diff: Optional[dict] = None):
        super().__init__(detail)
        self.detail = detail
        self.diff = diff


@dataclass(frozen=True)
class Outcome:
    """What ``submit`` did: applied now, or queued behind a second approver."""

    applied: bool
    proposal_id: Optional[str]
    classification: str
    diff: Diff


class ApprovalService:
    """Classify, record, queue and apply governance changes.

    ``store`` is the store the change targets (the active workspace store, the
    identity store for role changes, the control store for workspace
    membership); proposals and settings live in that same store, so the record
    sits next to the tables it governs. ``users`` is the identity population
    the comparator evaluates — passed in because identity may live in a
    different store than the change (multi-workspace mode).
    """

    def __init__(
        self,
        store,
        perms=None,
        *,
        users: Optional[list[User]] = None,
        local: bool = False,
    ):
        self.store = store
        self.perms = perms
        self.users = users or []
        self.local = local

    # -- posture ------------------------------------------------------------

    def require_second_approver(self) -> bool:
        settings = self.store.get_setting(SETTINGS_KEY) or {}
        return bool(settings.get("require_second_approver", False))

    # -- the front door -----------------------------------------------------

    def submit(
        self,
        *,
        kind: str,
        target: str,
        payload: dict,
        actor: str,
        actor_id: str = "",
        rationale: str = "",
    ) -> Outcome:
        """Classify the change, file its record, and apply or queue it."""
        diff = policy_diff.classify(self.store, self.perms, self.users, kind, target, payload)
        if diff.classification == "tightening":
            ticket_kind = "tightening"
        elif self.local:
            ticket_kind = "local"
        elif self.require_second_approver():
            pid = file_record(
                self.store, kind=kind, target=target, payload=payload,
                proposer=actor, proposer_id=actor_id, rationale=rationale,
                state="pending", classification=diff.classification,
                diff=diff.as_dict(),
            )
            self.store.log_audit(
                "proposal_filed",
                {"proposal_id": pid, "kind": kind, "target": target,
                 "classification": diff.classification},
                actor=actor,
            )
            return Outcome(applied=False, proposal_id=pid,
                           classification=diff.classification, diff=diff)
        else:
            ticket_kind = "self_approved"
        # Filed pending, applied, then decided — so a write that raises leaves
        # an honest pending record rather than one that claims it applied.
        pid = file_record(
            self.store, kind=kind, target=target, payload=payload,
            proposer=actor, proposer_id=actor_id, rationale=rationale,
            state="pending", classification=diff.classification,
            diff=diff.as_dict(),
        )
        ticket = ChangeTicket(kind=ticket_kind, actor=actor, proposal_id=pid)
        self._apply(kind, target, payload, ticket)
        self.store.decide_proposal(
            pid, state="approved", decided_by=actor,
            applied_at=utcnow_iso(), ticket_kind=ticket_kind,
        )
        self._audit_change(kind, target, payload, ticket, actor)
        return Outcome(applied=True, proposal_id=pid,
                       classification=diff.classification, diff=diff)

    # -- decisions ----------------------------------------------------------

    def approve(self, proposal_id: str, *, approver: str, approver_id: str) -> dict:
        """Reclassify against current state, then apply — atomically, in here.

        The staleness trap this closes: a diff computed at file time can be a
        different diff at apply time after an intervening change (precedent:
        the import path's refuse-if-report-changed digest). If the diff moved,
        the approve is refused, the proposal is marked ``superseded``, and the
        refusal carries what is true *now* — the approver re-files or approves
        reality, never a stale summary.
        """
        p = self.store.get_proposal(proposal_id)
        if p is None:
            raise KeyError(f"Proposal not found: {proposal_id!r}")
        if p["state"] != "pending":
            raise ApprovalError(f"Proposal {proposal_id} is {p['state']}, not pending")
        if p["proposer_id"] and approver_id == p["proposer_id"]:
            raise ApprovalError(
                "A proposal cannot be approved by its proposer; a second "
                "admin must review it"
            )
        fresh = policy_diff.classify(
            self.store, self.perms, self.users, p["kind"], p["target"], p["payload"]
        )
        if fresh.as_dict() != p["diff"]:
            self.store.decide_proposal(
                proposal_id, state="superseded", decided_by=approver,
                decision_reason="state changed since filing; the recorded diff no longer holds",
                diff=fresh.as_dict(), classification=fresh.classification,
            )
            self.store.log_audit(
                "proposal_superseded",
                {"proposal_id": proposal_id, "kind": p["kind"], "target": p["target"]},
                actor=approver,
            )
            raise ApprovalError(
                f"Proposal {proposal_id} was filed against state that has since "
                "changed; it is now superseded. Re-file against what is true now.",
                diff=fresh.as_dict(),
            )
        ticket = ChangeTicket(kind="approved", actor=approver, proposal_id=proposal_id)
        self._apply(p["kind"], p["target"], p["payload"], ticket)
        try:
            self.store.decide_proposal(
                proposal_id, state="approved", decided_by=approver,
                applied_at=utcnow_iso(), ticket_kind="approved",
            )
        except ValueError as exc:
            # Two admins raced; the pending-state predicate in the UPDATE let
            # exactly one decide. The writes are whole-list replaces of the
            # same payload, so the double apply is idempotent.
            raise ApprovalError(str(exc)) from exc
        self.store.log_audit(
            "proposal_approved",
            {"proposal_id": proposal_id, "kind": p["kind"], "target": p["target"]},
            actor=approver,
        )
        self._audit_change(p["kind"], p["target"], p["payload"], ticket, approver)
        out = self.store.get_proposal(proposal_id)
        assert out is not None
        return out

    def reject(self, proposal_id: str, *, approver: str, reason: str = "") -> dict:
        p = self.store.get_proposal(proposal_id)
        if p is None:
            raise KeyError(f"Proposal not found: {proposal_id!r}")
        if p["state"] != "pending":
            raise ApprovalError(f"Proposal {proposal_id} is {p['state']}, not pending")
        try:
            self.store.decide_proposal(
                proposal_id, state="rejected", decided_by=approver,
                decision_reason=reason,
            )
        except ValueError as exc:
            raise ApprovalError(str(exc)) from exc
        self.store.log_audit(
            "proposal_rejected",
            {"proposal_id": proposal_id, "kind": p["kind"], "target": p["target"],
             "reason": reason},
            actor=approver,
        )
        out = self.store.get_proposal(proposal_id)
        assert out is not None
        return out

    def withdraw(self, proposal_id: str, *, actor: str, actor_id: str) -> dict:
        p = self.store.get_proposal(proposal_id)
        if p is None:
            raise KeyError(f"Proposal not found: {proposal_id!r}")
        if p["state"] != "pending":
            raise ApprovalError(f"Proposal {proposal_id} is {p['state']}, not pending")
        if p["proposer_id"] and actor_id != p["proposer_id"]:
            raise ApprovalError("Only the proposer may withdraw a proposal")
        try:
            self.store.decide_proposal(proposal_id, state="withdrawn", decided_by=actor)
        except ValueError as exc:
            raise ApprovalError(str(exc)) from exc
        self.store.log_audit(
            "proposal_withdrawn",
            {"proposal_id": proposal_id, "kind": p["kind"], "target": p["target"]},
            actor=actor,
        )
        out = self.store.get_proposal(proposal_id)
        assert out is not None
        return out

    # -- settings -----------------------------------------------------------

    def set_second_approver(self, enable: bool, *, actor: str, actor_id: str,
                            active_admins: int) -> Outcome:
        """Flip second-approver mode — itself a governed change.

        Enabling is a tightening and applies immediately, but refuses unless
        at least two active, enabled admins exist: a mode nobody can satisfy
        is a deadlock, not a control. (The existing self-demote/self-disable
        guards keep the admin count from later falling to zero via the API.)
        Disabling is a loosening and queues under the regime it is disabling —
        the mode cannot be switched off unilaterally by the person it
        constrains.
        """
        if enable and active_admins < 2:
            raise ApprovalError(
                "Second-approver mode needs at least 2 active admin users; "
                f"this server has {active_admins}"
            )
        return self.submit(
            kind="approval_settings", target=SETTINGS_KEY,
            payload={"require_second_approver": bool(enable)},
            actor=actor, actor_id=actor_id,
        )

    # -- application --------------------------------------------------------

    def _apply(self, kind: str, target: str, payload: dict, ticket: ChangeTicket) -> None:
        """The one dispatch from a proposal to its store write.

        Every branch calls the ticketed store method; ``approve`` reuses this
        verbatim, so what a second admin applies is exactly what ``submit``
        would have applied — one code path, not a replayed request.
        """
        s = self.store
        if kind == "dataset_grants":
            s.set_grants_for_dataset(target, payload["grants"], ticket=ticket)
        elif kind == "ontology_grants":
            s.set_grants_for_type(target, payload["grants"], ticket=ticket)
        elif kind == "dataset_policy":
            s.set_dataset_policy(target, payload["policy"], ticket=ticket)
        elif kind == "dataset_markings":
            s.set_explicit_markings(target, payload["markings"], ticket=ticket)
            s.recompute_all_markings()  # propagate downstream through lineage
        elif kind == "marking_delete":
            s.delete_marking(target, ticket=ticket)
            s.recompute_all_markings()  # its removal ripples through effective sets
        elif kind == "clearances":
            s.set_clearances(target, payload["markings"], ticket=ticket)
        elif kind == "group_members":
            s.set_group_members(target, payload["members"], ticket=ticket)
        elif kind == "group_delete":
            s.delete_group(target, ticket=ticket)
        elif kind == "user_role":
            s.update_user(target, role=Role(payload["role"]).value, ticket=ticket)
        elif kind == "workspace_member":
            s.set_member(payload["slug"], payload["username"].lower(),
                         Role(payload["role"]), ticket=ticket)
        elif kind == "approval_settings":
            s.set_setting(SETTINGS_KEY,
                          {"require_second_approver": bool(payload["require_second_approver"])})
        else:
            raise ValueError(f"No apply dispatch for change kind {kind!r}")

    def _audit_change(self, kind: str, target: str, payload: dict,
                      ticket: ChangeTicket, actor: str) -> None:
        """The per-change audit event, carrying proposal_id and ticket_kind."""
        action = {
            "dataset_grants": "dataset_permissions_set",
            "ontology_grants": "ontology_permissions_set",
            "dataset_policy": "dataset_policy_set",
            "dataset_markings": "dataset_markings_set",
            "marking_delete": "marking_deleted",
            "clearances": "clearances_set",
            "group_members": "group_members_set",
            "group_delete": "group_deleted",
            "user_role": "user_role_set",
            "workspace_member": "workspace_member_set",
            "approval_settings": "approval_settings_set",
        }.get(kind, kind)
        details: dict = {"proposal_id": ticket.proposal_id, "ticket_kind": ticket.kind}
        if kind == "dataset_grants":
            details["dataset"] = target
            details["grant_count"] = len(payload.get("grants", []))
        elif kind == "ontology_grants":
            details["object_type"] = target
            details["grant_count"] = len(payload.get("grants", []))
        elif kind == "dataset_policy":
            policy = payload.get("policy")
            details["dataset"] = target
            details["row_policy"] = bool(policy and policy.get("row_policy"))
            details["masked_columns"] = [
                m.get("column") for m in (policy or {}).get("column_masks", [])
            ]
        elif kind == "dataset_markings":
            details["dataset"] = target
            details["markings"] = payload.get("markings", [])
        elif kind == "marking_delete":
            details["name"] = target
        elif kind == "clearances":
            details["username"] = target
            details["markings"] = payload.get("markings", [])
        elif kind == "group_members":
            details["name"] = target
            details["count"] = len(payload.get("members", []))
        elif kind == "group_delete":
            details["name"] = target
        elif kind == "user_role":
            details["username"] = target
            details["role"] = payload.get("role")
        elif kind == "workspace_member":
            details["slug"] = payload.get("slug")
            details["username"] = payload.get("username")
            details["role"] = payload.get("role")
        elif kind == "approval_settings":
            details["require_second_approver"] = payload.get("require_second_approver")
        self.store.log_audit(action, details, actor=actor)
