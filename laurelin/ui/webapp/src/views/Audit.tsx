// The audit trail, at three privilege levels — because R2 says a field is
// readable only at the level that could have written it, and an audit row's
// `details` is an open bag written by whoever logged the event.
//
//   viewer  → GET /audit/mine. Their own rows, whole. Nothing is disclosed by
//             construction: you cannot learn a secret from a row you wrote.
//   editor  → GET /audit. Every row whose writer declared `min_read_role` down
//             to editor, without `details`.
//   admin   → the same route, with `details`.
//
// The interesting case is the editor's. `details` arrives as an *absent key*,
// not an empty object, and rendering absent as "—" would say "this event
// carried no information" when the truth is "this information is one level
// above you". So the column says which.

import { useQuery } from "@tanstack/react-query";

import { api, API } from "../api";
import { useAuth } from "../auth";
import type { AuditEvent } from "../types";
import {
  Badge,
  Column,
  DataTable,
  EmptyState,
  ErrorBox,
  PageHeader,
  Spinner,
  Withheld,
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
  const keys = Object.keys(details);
  if (keys.length === 0) return "—";
  return keys.map((k) => `${k}=${fmtValue(details[k])}`).join(", ");
}

function DetailsCell({ event }: { event: AuditEvent }) {
  if (event.details === undefined) {
    return (
      <Withheld
        what="What an audit event recorded"
        role="admin"
        why="An audit detail is whatever the code that logged it chose to put there — including, historically, an object's own property values. Only the level that writes them reads them."
      />
    );
  }
  return <span className="mono dim">{fmtDetails(event.details)}</span>;
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
    render: (e) => <DetailsCell event={e} />,
  },
];

export function AuditView() {
  const auth = useAuth();
  const canReadAll = auth.can("editor");
  const path = canReadAll ? "/audit" : "/audit/mine";

  const query = useQuery({
    queryKey: ["audit", path, 100],
    queryFn: () => api.get<AuditEvent[]>(`${API}${path}?limit=100`),
    staleTime: 5000,
  });

  return (
    <div>
      <PageHeader
        title={canReadAll ? "Audit log" : "My activity"}
        subtitle={
          canReadAll
            ? "Recent activity in this workspace."
            : "Everything you did here. Other people's activity is shown to editors and admins — an audit entry can carry the data the action touched, so it is read at the level that writes it."
        }
      />
      {query.isLoading ? (
        <Spinner />
      ) : query.error ? (
        <ErrorBox error={query.error} />
      ) : !query.data || query.data.length === 0 ? (
        <EmptyState>
          {canReadAll
            ? "No audit events recorded yet."
            : "You have not done anything here yet."}
        </EmptyState>
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
