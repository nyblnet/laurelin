import { useQuery } from "@tanstack/react-query";

import { api, API } from "../api";
import type { AuditEvent } from "../types";
import {
  Badge,
  Column,
  DataTable,
  EmptyState,
  ErrorBox,
  PageHeader,
  Spinner,
  fmtTime,
  fmtValue,
} from "../ui";

type Tone = "neutral" | "green" | "red" | "blue";

function actionTone(action: string): Tone {
  if (
    action === "login_succeeded" ||
    action === "setup_completed" ||
    action.includes("build") ||
    action.endsWith("_succeeded") ||
    action.endsWith("_finished")
  ) {
    return "green";
  }
  if (
    action === "login_failed" ||
    action === "login_throttled" ||
    action.endsWith("_deleted") ||
    action.endsWith("_revoked") ||
    action.endsWith("_failed")
  ) {
    return "red";
  }
  if (action.endsWith("_created") || action.endsWith("_updated")) {
    return "blue";
  }
  return "neutral";
}

function fmtDetails(details: Record<string, unknown>): string {
  const keys = Object.keys(details ?? {});
  if (keys.length === 0) return "—";
  return keys.map((k) => `${k}=${fmtValue(details[k])}`).join(", ");
}

const columns: Column<AuditEvent>[] = [
  {
    label: "Time",
    render: (e) => fmtTime(e.timestamp),
    className: "mono",
  },
  {
    label: "Actor",
    render: (e) => <span className="mono">{e.actor}</span>,
  },
  {
    label: "Action",
    render: (e) => <Badge tone={actionTone(e.action)}>{e.action}</Badge>,
  },
  {
    label: "Details",
    render: (e) => <span className="mono dim">{fmtDetails(e.details)}</span>,
  },
];

export function AuditView() {
  const query = useQuery({
    queryKey: ["audit", 100],
    queryFn: () => api.get<AuditEvent[]>(`${API}/audit?limit=100`),
    staleTime: 5000,
  });

  return (
    <div>
      <PageHeader
        title="Audit log"
        subtitle="Recent activity in this workspace."
      />
      {query.isLoading ? (
        <Spinner />
      ) : query.error ? (
        <ErrorBox error={query.error} />
      ) : !query.data || query.data.length === 0 ? (
        <EmptyState>No audit events recorded yet.</EmptyState>
      ) : (
        <DataTable
          columns={columns}
          rows={query.data}
          rowKey={(e) => String(e.id)}
        />
      )}
    </div>
  );
}
