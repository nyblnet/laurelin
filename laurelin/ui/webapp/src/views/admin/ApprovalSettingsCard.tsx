// Admin > Approvals > second-approver toggle.
//
// Enabling refuses without >= 2 active admins (the server 409s; the refusal is
// surfaced inline, not swallowed). Disabling is itself a governance loosening
// and QUEUES under the very regime it is disabling — the response is a queued
// proposal, and this card says so instead of pretending the switch flipped.

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../../api";
import type { ApprovalSettings } from "../../types";
import { Badge, Spinner } from "../../ui";
import { InlineError } from "./shared";

interface SettingsPutResult {
  require_second_approver?: boolean;
  applied?: boolean;
  proposal_id?: string;
  queued?: boolean;
}

export function ApprovalSettingsCard() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["approval-settings"],
    queryFn: () => api.get<ApprovalSettings>(`${API}/settings/approvals`),
  });

  const put = useMutation({
    mutationFn: (enable: boolean) =>
      api.put<SettingsPutResult>(`${API}/settings/approvals`, {
        require_second_approver: enable,
      }),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["approval-settings"] });
      void qc.invalidateQueries({ queryKey: ["proposals"] });
    },
  });

  if (q.isLoading) return <Spinner />;
  const on = q.data?.require_second_approver ?? false;
  const admins = q.data?.active_admins;
  const disableQueued =
    put.data && (put.data.queued || put.data.applied === false) && put.data.proposal_id;

  return (
    <div className="card" style={{ marginTop: 12, marginBottom: 4 }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <span style={{ fontWeight: 600 }}>Second-approver mode</span>
        <Badge tone={on ? "green" : "neutral"}>{on ? "on" : "off"}</Badge>
        <span style={{ flex: 1 }} />
        <button
          className={on ? "small" : "primary small"}
          disabled={put.isPending}
          onClick={() => put.mutate(!on)}
        >
          {put.isPending ? "Saving…" : on ? "Turn off (queues)" : "Turn on"}
        </button>
      </div>
      <p className="dim" style={{ fontSize: 12.5, marginBottom: 0 }}>
        {on
          ? "Loosenings queue until a different admin approves. Turning this off is itself a loosening and files a proposal another admin must approve."
          : "Loosenings apply immediately with a self-approved record. Turning this on requires at least two active admin accounts" +
            (admins != null ? ` (currently ${admins})` : "") +
            ", so a one-person workspace cannot deadlock itself."}
      </p>
      {disableQueued && (
        <p style={{ fontSize: 12.5 }}>
          <Badge tone="gold">queued</Badge> Filed as proposal{" "}
          <span className="mono">{put.data!.proposal_id}</span> — a second admin
          must approve turning review off.
        </p>
      )}
      <InlineError err={put.error} />
    </div>
  );
}
