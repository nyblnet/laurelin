// Admin > Approvals: the governance-change inbox.
//
// Pending proposals on top — what would change, rendered so a human can judge
// it (the comparator's per-principal gains, not raw JSON), who proposed it and
// why, approve/reject with a reason. In second-approver mode an admin cannot
// approve their own proposal, and the button says so instead of failing
// silently. History below. Contents are admin-only end to end: the API
// serializes proposals at admin level and this whole view lives behind the
// admin gate.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, ApiError, api } from "../../api";
import { useAuth } from "../../auth";
import type { ApprovalSettings, Proposal } from "../../types";
import { Badge, EmptyState, ErrorBox, Spinner, fmtTime } from "../../ui";
import { InlineError } from "./shared";
import { fmtAge } from "../Health";

// One human sentence per change kind — what approving would DO.
const KIND_LABEL: Record<string, string> = {
  dataset_grants: "Replace access grants on dataset",
  ontology_grants: "Replace access grants on object type",
  dataset_policy: "Change row policy / column masks on dataset",
  dataset_markings: "Change classification markings on dataset",
  marking_delete: "Delete marking",
  clearances: "Change clearances for user",
  group_members: "Change members of group",
  group_delete: "Delete group",
  user_role: "Change role of user",
  workspace_member: "Change workspace membership",
  approval_settings: "Change approval settings",
  import: "Workspace import (digest ceremony)",
  scim_sync: "SCIM directory sync",
};

function kindLabel(p: Proposal): string {
  return KIND_LABEL[p.kind] ?? p.kind;
}

/** The payload, summarized like a human change description — not raw JSON. */
function payloadSummary(p: Proposal): string | null {
  const pl = p.payload as Record<string, unknown>;
  if (!pl) return null;
  if (Array.isArray(pl.grants)) {
    const grants = pl.grants as { subject_kind: string; subject: string; can_view: boolean; can_edit: boolean }[];
    if (grants.length === 0) {
      return "Empties the grant list — the dataset falls back to role-based default access (that is a widening, not a lockdown).";
    }
    return grants
      .map(
        (g) =>
          `${g.subject_kind === "everyone" ? "everyone" : `${g.subject_kind} ${g.subject}`}: ${
            g.can_edit ? "edit" : g.can_view ? "view" : "none"
          }`,
      )
      .join(" · ");
  }
  if (Array.isArray(pl.markings)) {
    const m = pl.markings as string[];
    return m.length === 0 ? "Removes every marking/clearance in the list." : `New set: ${m.join(", ")}`;
  }
  if (Array.isArray(pl.members)) {
    const m = pl.members as string[];
    return m.length === 0 ? "Removes every member." : `New members: ${m.join(", ")}`;
  }
  if (typeof pl.role === "string") return `New role: ${pl.role}`;
  if (pl.policy !== undefined) {
    if (pl.policy === null) return "Removes the entire policy (row rules and masks).";
    const pol = pl.policy as { row_policy?: { column?: string; rules?: unknown[] } | null; column_masks?: { column: string; mode: string }[] };
    const bits: string[] = [];
    if (pol.row_policy) {
      bits.push(`row policy on ${pol.row_policy.column} (${pol.row_policy.rules?.length ?? 0} rule${(pol.row_policy.rules?.length ?? 0) === 1 ? "" : "s"})`);
    } else {
      bits.push("no row policy");
    }
    const masks = pol.column_masks ?? [];
    bits.push(
      masks.length === 0
        ? "no masks"
        : `masks: ${masks.map((m) => `${m.column} (${m.mode})`).join(", ")}`,
    );
    return bits.join(" · ");
  }
  if (typeof pl.require_second_approver === "boolean") {
    return pl.require_second_approver
      ? "Requires a second admin to approve future loosenings."
      : "Turns second-approver review OFF — future loosenings self-approve.";
  }
  return null;
}

const STATE_TONE: Record<string, "neutral" | "gold" | "green" | "red" | "blue"> = {
  pending: "gold",
  approved: "green",
  rejected: "red",
  withdrawn: "neutral",
  superseded: "neutral",
};

function DiffBlock({ p }: { p: Proposal }) {
  const loosening = p.diff?.classification === "loosening";
  return (
    <div style={{ marginTop: 6 }}>
      <Badge tone={loosening ? "red" : "green"}>
        {p.diff?.classification ?? p.classification ?? "?"}
      </Badge>
      {p.diff?.reason && (
        <span className="dim" style={{ fontSize: 12, marginLeft: 8 }}>
          ({p.diff.reason})
        </span>
      )}
      {loosening && (p.diff?.gains?.length ?? 0) > 0 && (
        <ul style={{ margin: "6px 0 0 18px", fontSize: 12.5 }}>
          {p.diff.gains.slice(0, 12).map((g, i) => (
            <li key={i}>{g}</li>
          ))}
          {p.diff.gains.length > 12 && (
            <li className="dim">…and {p.diff.gains.length - 12} more</li>
          )}
        </ul>
      )}
      {loosening && (p.diff?.gains?.length ?? 0) === 0 && (
        <span className="dim" style={{ fontSize: 12, marginLeft: 8 }}>
          Queued fail-closed: the comparator could not prove this harmless.
        </span>
      )}
    </div>
  );
}

function PendingCard({
  p,
  requireSecond,
  onDecided,
}: {
  p: Proposal;
  requireSecond: boolean;
  onDecided: () => void;
}) {
  const auth = useAuth();
  const me = auth.user;
  const [reason, setReason] = useState("");
  const mine = !!me && (p.proposer_id === me.id || p.proposer === me.username);

  const approve = useMutation({
    mutationFn: () => api.post<Proposal>(`${API}/proposals/${p.id}/approve`, {}),
    onSettled: onDecided,
  });
  const reject = useMutation({
    mutationFn: () => api.post<Proposal>(`${API}/proposals/${p.id}/reject`, { reason }),
    onSettled: onDecided,
  });
  const withdraw = useMutation({
    mutationFn: () => api.post<Proposal>(`${API}/proposals/${p.id}/withdraw`, {}),
    onSettled: onDecided,
  });

  const cannotApproveOwn = requireSecond && mine;
  const summary = payloadSummary(p);

  return (
    <div className="card" style={{ marginBottom: 10 }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <span style={{ fontWeight: 600 }}>
          {kindLabel(p)} <span className="mono">{p.target}</span>
        </span>
        <Badge tone="gold">pending</Badge>
        <span style={{ flex: 1 }} />
        <span className="dim" style={{ fontSize: 12 }} title={fmtTime(p.created_at)}>
          {p.proposer ? `proposed by ${p.proposer}` : "proposed"} {fmtAge(p.created_at)}
          {mine && <span className="faint"> (you)</span>}
        </span>
        <span className="mono faint" style={{ fontSize: 11 }}>{p.id}</span>
      </div>

      {summary && <div style={{ fontSize: 13, marginTop: 6 }}>{summary}</div>}
      <DiffBlock p={p} />
      {p.rationale && (
        <div className="dim" style={{ fontSize: 12.5, marginTop: 6 }}>
          Rationale: {p.rationale}
        </div>
      )}

      <div className="toolbar" style={{ gap: 8, marginTop: 10, alignItems: "center", flexWrap: "wrap" }}>
        <span title={cannotApproveOwn ? "You proposed this change. Second-approver mode is on, so a different admin must review it — that is the point of the mode." : undefined}>
          <button
            className="primary small"
            disabled={approve.isPending || cannotApproveOwn}
            onClick={() => approve.mutate()}
          >
            {approve.isPending ? "Applying…" : "Approve & apply"}
          </button>
        </span>
        {cannotApproveOwn && (
          <span className="dim" style={{ fontSize: 12 }}>
            Your own proposal — a second admin must approve it.
          </span>
        )}
        <input
          style={{ flex: "1 1 180px", maxWidth: 320 }}
          placeholder="Reason (required to reject)"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
        <button
          className="danger small"
          disabled={reject.isPending || reason.trim() === ""}
          onClick={() => reject.mutate()}
        >
          Reject
        </button>
        {mine && (
          <button className="small" disabled={withdraw.isPending} onClick={() => withdraw.mutate()}>
            Withdraw
          </button>
        )}
      </div>
      {/* A 409 on approve is the staleness guard doing its job: the workspace
          changed since filing and the proposal is now superseded. Surface the
          server's sentence — it carries the recomputed diff's meaning. */}
      <InlineError err={approve.error ?? reject.error ?? withdraw.error} />
      {approve.error instanceof ApiError && approve.error.status === 409 && (
        <p className="dim" style={{ fontSize: 12 }}>
          The workspace changed since this was filed; re-make the change to
          file a fresh proposal against current state.
        </p>
      )}
    </div>
  );
}

export function ApprovalsSection() {
  const qc = useQueryClient();
  const settings = useQuery({
    queryKey: ["approval-settings"],
    queryFn: () => api.get<ApprovalSettings>(`${API}/settings/approvals`),
  });
  const requireSecond = settings.data?.require_second_approver ?? false;
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["proposals"] });
    // A decision may have applied a grant/policy/marking change.
    void qc.invalidateQueries({ queryKey: ["dataset-permissions"] });
    void qc.invalidateQueries({ queryKey: ["dataset-policies"] });
    void qc.invalidateQueries({ queryKey: ["markings"] });
    void qc.invalidateQueries({ queryKey: ["groups"] });
    void qc.invalidateQueries({ queryKey: ["users"] });
  };

  const q = useQuery({
    queryKey: ["proposals"],
    queryFn: () => api.get<Proposal[]>(`${API}/proposals`),
  });

  const pending = (q.data ?? []).filter((p) => p.state === "pending");
  const decided = (q.data ?? []).filter((p) => p.state !== "pending");

  return (
    <section style={{ marginTop: 28 }}>
      <h2 style={{ fontSize: 15, marginBottom: 4 }}>Approvals</h2>
      <p className="dim" style={{ fontSize: 12.5, marginTop: 0 }}>
        Every governance loosening files a proposal. With second-approver mode
        off it self-approves with a record; with it on, changes below wait for
        a different admin. Tightenings (adding a mask, narrowing a grant) apply
        immediately, always — incident containment is never queued.
      </p>

      {q.isLoading && <Spinner />}
      {q.isError && <ErrorBox error={q.error} />}

      {q.data && pending.length === 0 && (
        <EmptyState>Nothing waiting for review.</EmptyState>
      )}
      {pending.map((p) => (
        <PendingCard key={p.id} p={p} requireSecond={requireSecond} onDecided={invalidate} />
      ))}

      {decided.length > 0 && (
        <details style={{ marginTop: 12 }}>
          <summary style={{ cursor: "pointer", fontSize: 13.5, fontWeight: 600 }}>
            History ({decided.length})
          </summary>
          <div className="table-wrap" style={{ marginTop: 8 }}>
            <table>
              <thead>
                <tr>
                  <th>Change</th>
                  <th>Class</th>
                  <th>Via</th>
                  <th>Proposed</th>
                  <th>Decided</th>
                  <th>Outcome</th>
                </tr>
              </thead>
              <tbody>
                {decided.map((p) => (
                  <tr key={p.id}>
                    <td>
                      {kindLabel(p)} <span className="mono">{p.target}</span>
                    </td>
                    <td>
                      <Badge tone={p.classification === "loosening" ? "red" : "green"}>
                        {p.classification || "—"}
                      </Badge>
                    </td>
                    <td className="dim" style={{ fontSize: 12 }}>
                      {p.ticket_kind || "—"}
                    </td>
                    <td className="dim" style={{ fontSize: 12 }} title={fmtTime(p.created_at)}>
                      {p.proposer || "—"} · {fmtAge(p.created_at)}
                    </td>
                    <td className="dim" style={{ fontSize: 12 }} title={fmtTime(p.decided_at)}>
                      {p.decided_by || "—"}
                    </td>
                    <td>
                      <Badge tone={STATE_TONE[p.state] ?? "neutral"}>{p.state}</Badge>
                      {p.decision_reason && (
                        <span className="dim" style={{ fontSize: 12, marginLeft: 6 }}>
                          {p.decision_reason}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </section>
  );
}
