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
// above you". So the column says which — and so does the EMPTY state: an
// editor's empty page must never claim "nothing was recorded" when the truth
// is "nothing you can read" (rule E2; no count is disclosed either way).
//
// The filter bar sends `since`/`until`/`actor`/`action` as query params (the
// server contract, spec S4) and applies the same exact-match semantics to the
// fetched window client-side, so filtering behaves identically whether or not
// the server applies the params to the page it returns. No filter can widen:
// the server's min_read_role cut happens before any of this.

import { useMemo, useState } from "react";
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

/** Sign-in churn: high-volume, low-signal rows collapsed by default so the
 *  governance events they bury stay on the first screen. */
function isLoginNoise(e: AuditEvent): boolean {
  return e.action.startsWith("login_") || e.action === "logout";
}

interface AuditFilters {
  since: string; // yyyy-mm-dd, inclusive
  until: string; // yyyy-mm-dd, inclusive
  actor: string; // exact username
  action: string; // exact action name
}

const NO_FILTERS: AuditFilters = { since: "", until: "", actor: "", action: "" };

function filtersActive(f: AuditFilters): boolean {
  return !!(f.since || f.until || f.actor.trim() || f.action.trim());
}

/** The exact-match semantics the server params (S4) carry, applied to the
 *  fetched window so behavior is identical before and after the server side
 *  of the contract lands. Narrowing only — never widening. */
export function matchesAuditFilters(e: AuditEvent, f: AuditFilters): boolean {
  const day = e.timestamp.slice(0, 10);
  if (f.since && day < f.since) return false;
  if (f.until && day > f.until) return false;
  if (f.actor.trim() && e.actor !== f.actor.trim()) return false;
  if (f.action.trim() && e.action !== f.action.trim()) return false;
  return true;
}

const LIMIT = 200;

export function auditQueryString(f: AuditFilters): string {
  const q = new URLSearchParams();
  q.set("limit", String(LIMIT));
  if (f.since) q.set("since", f.since);
  if (f.until) q.set("until", f.until);
  if (f.actor.trim()) q.set("actor", f.actor.trim());
  if (f.action.trim()) q.set("action", f.action.trim());
  return q.toString();
}

function FilterBar({
  filters,
  onChange,
  actors,
  actions,
}: {
  filters: AuditFilters;
  onChange: (next: AuditFilters) => void;
  actors: string[];
  actions: string[];
}) {
  const set = (patch: Partial<AuditFilters>) => onChange({ ...filters, ...patch });
  return (
    <div
      className="toolbar"
      style={{ alignItems: "flex-end", flexWrap: "wrap", marginBottom: 12 }}
    >
      <div className="field" style={{ margin: 0 }}>
        <label htmlFor="audit-since">From</label>
        <input
          id="audit-since"
          type="date"
          value={filters.since}
          onChange={(e) => set({ since: e.target.value })}
        />
      </div>
      <div className="field" style={{ margin: 0 }}>
        <label htmlFor="audit-until">To</label>
        <input
          id="audit-until"
          type="date"
          value={filters.until}
          onChange={(e) => set({ until: e.target.value })}
        />
      </div>
      <div className="field" style={{ margin: 0 }}>
        <label htmlFor="audit-actor">Actor</label>
        <input
          id="audit-actor"
          className="mono"
          list="audit-actor-list"
          placeholder="anyone"
          value={filters.actor}
          onChange={(e) => set({ actor: e.target.value })}
        />
        <datalist id="audit-actor-list">
          {actors.map((a) => (
            <option key={a} value={a} />
          ))}
        </datalist>
      </div>
      <div className="field" style={{ margin: 0 }}>
        <label htmlFor="audit-action">Action</label>
        <input
          id="audit-action"
          className="mono"
          list="audit-action-list"
          placeholder="any action"
          value={filters.action}
          onChange={(e) => set({ action: e.target.value })}
        />
        <datalist id="audit-action-list">
          {actions.map((a) => (
            <option key={a} value={a} />
          ))}
        </datalist>
      </div>
      {filtersActive(filters) && (
        <button
          type="button"
          className="small"
          onClick={() => onChange({ ...NO_FILTERS })}
        >
          Clear filters
        </button>
      )}
    </div>
  );
}

export function AuditView() {
  const auth = useAuth();
  const canReadAll = auth.can("editor");
  const path = canReadAll ? "/audit" : "/audit/mine";

  const [filters, setFilters] = useState<AuditFilters>({ ...NO_FILTERS });
  const [showLogins, setShowLogins] = useState(false);

  const qs = auditQueryString(filters);
  const query = useQuery({
    queryKey: ["audit", path, qs],
    queryFn: () => api.get<AuditEvent[]>(`${API}${path}?${qs}`),
    staleTime: 5000,
    // Changing a filter must dim-and-replace the table, not unmount it (L1).
    placeholderData: (prev) => prev,
  });

  const rows = useMemo(() => query.data ?? [], [query.data]);
  const filtered = useMemo(
    () => rows.filter((e) => matchesAuditFilters(e, filters)),
    [rows, filters],
  );
  // An explicit action filter targeting a sign-in action overrides the noise
  // collapse — filtering for login_failed and seeing nothing would be a trap.
  const wantsLogins =
    showLogins || filters.action.trim().startsWith("login") || filters.action.trim() === "logout";
  const visible = wantsLogins ? filtered : filtered.filter((e) => !isLoginNoise(e));
  const hiddenLogins = filtered.length - visible.length;

  const actors = useMemo(
    () => Array.from(new Set(rows.map((e) => e.actor))).sort(),
    [rows],
  );
  const actions = useMemo(
    () => Array.from(new Set(rows.map((e) => e.action))).sort(),
    [rows],
  );

  // The empty sentence must be honest about WHY it is empty (rule E2):
  //  - filters active        → the filters excluded everything fetched;
  //  - editor, nothing at all → rows above the editor read level are cut
  //    server-side, so absence here is not absence in the log (no count is
  //    disclosed — that would leak how much is withheld);
  //  - admin, nothing at all  → admins read every row, so absence is real.
  function emptyMessage() {
    if (filtersActive(filters)) {
      return "No events match these filters.";
    }
    if (!auth.can("admin")) {
      return "No events you can read. Events above your read level are not shown here.";
    }
    return "No audit events recorded yet.";
  }

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
      {canReadAll && (
        <FilterBar
          filters={filters}
          onChange={setFilters}
          actors={actors}
          actions={actions}
        />
      )}
      {query.isLoading ? (
        <Spinner />
      ) : query.error ? (
        <ErrorBox error={query.error} onRetry={() => query.refetch()} />
      ) : visible.length === 0 ? (
        <>
          <EmptyState>{emptyMessage()}</EmptyState>
          {hiddenLogins > 0 && (
            <div className="toolbar" style={{ marginTop: 8 }}>
              <span className="dim" style={{ fontSize: 12.5 }}>
                {hiddenLogins} sign-in event{hiddenLogins === 1 ? "" : "s"} hidden.
              </span>
              <button type="button" className="small" onClick={() => setShowLogins(true)}>
                Show sign-in events
              </button>
            </div>
          )}
        </>
      ) : (
        <div style={query.isFetching ? { opacity: 0.6 } : undefined}>
          <DataTable
            columns={columns}
            rows={visible}
            rowKey={(e) => String(e.id)}
          />
          {canReadAll && (
            <div className="toolbar" style={{ marginTop: 8 }}>
              {hiddenLogins > 0 && !wantsLogins ? (
                <>
                  <span className="dim" style={{ fontSize: 12.5 }}>
                    {hiddenLogins} sign-in event{hiddenLogins === 1 ? "" : "s"} hidden.
                  </span>
                  <button
                    type="button"
                    className="small"
                    onClick={() => setShowLogins(true)}
                  >
                    Show sign-in events
                  </button>
                </>
              ) : (
                wantsLogins &&
                showLogins && (
                  <button
                    type="button"
                    className="small"
                    onClick={() => setShowLogins(false)}
                  >
                    Hide sign-in events
                  </button>
                )
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
