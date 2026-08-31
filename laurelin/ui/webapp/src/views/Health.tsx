// Health: an operator glances and sees what is wrong — failed builds, failing
// expectations, silent schedules, stale datasets — reddest first.
//
// Disclosure contract (mirrors the API, which is the real authority):
// * The rollup is filtered per caller: nothing here lists a dataset the viewer
//   cannot read, and there is deliberately NO unfiltered total anywhere on the
//   page — different viewers legitimately see different counts.
// * A viewer sees failures as {code, subject} and expectation chips as
//   name(column)/severity. Expectation *messages* and *measured* values are
//   editor-authored prose and data-about-data; they ride in `detail`, which
//   the server omits below editor. The UI renders them only when present.
// * Schedule and source names reach editors and above (same `detail` rule);
//   a viewer sees `schedule overdue`, not which schedule.

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../api";
import { useAuth } from "../auth";
import type { AlertWebhook, DatasetHealth, HealthEvent, HealthStatus, Role } from "../types";
import {
  Badge,
  EmptyState,
  ErrorBox,
  FailureBadge,
  FailureNote,
  PageHeader,
  Spinner,
  fmtTime,
} from "../ui";
import { InlineError } from "./admin/shared";

// Red sections first; healthy and unknown collapse below the fold.
const SECTION_ORDER: HealthStatus[] = ["failing", "overdue", "stale", "healthy", "unknown"];

const STATUS_TONE: Record<HealthStatus, "red" | "gold" | "green" | "neutral"> = {
  failing: "red",
  overdue: "red",
  stale: "gold",
  healthy: "green",
  unknown: "neutral",
};

const SECTION_LABEL: Record<HealthStatus, string> = {
  failing: "Failing",
  overdue: "Overdue",
  stale: "Stale",
  healthy: "Healthy",
  unknown: "Unmonitored",
};

const SECTION_HINT: Record<HealthStatus, string> = {
  failing: "The latest build, expectation or sync failed.",
  overdue: "A schedule that should have fired has not — including when the scheduler itself is down.",
  stale: "The declared freshness window was missed.",
  healthy: "Latest build succeeded, schedules on time, freshness met.",
  unknown:
    "No builds, no schedule, no freshness declaration. Not red on purpose: an ad-hoc upload is undeclared, not broken. Declare a freshness window to monitor it.",
};

/** "41m ago" — coarse on purpose; the exact stamp is in the tooltip. */
export function fmtAge(ts: string | null | undefined): string {
  if (!ts) return "never";
  const then = Date.parse(ts);
  if (Number.isNaN(then)) return "—";
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function fmtWindow(seconds: number): string {
  if (seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

// ------------------------------------------------------------- freshness

function FreshnessEditor({ h }: { h: DatasetHealth }) {
  const qc = useQueryClient();
  // Hours in the input: nobody declares freshness in raw seconds.
  const [hours, setHours] = useState(
    h.expected_fresh_within != null ? String(h.expected_fresh_within / 3600) : "",
  );
  const save = useMutation({
    mutationFn: (seconds: number | null) =>
      api.put(`${API}/datasets/${encodeURIComponent(h.dataset)}/freshness`, {
        expected_fresh_seconds: seconds,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["health"] }),
  });
  const parsed = parseFloat(hours);
  const valid = hours.trim() !== "" && !Number.isNaN(parsed) && parsed > 0;
  return (
    <div className="toolbar" style={{ gap: 8, alignItems: "center", marginTop: 8 }}>
      <label style={{ fontSize: 12 }}>Expect fresh within</label>
      <input
        className="mono"
        style={{ width: 70 }}
        value={hours}
        placeholder="24"
        onChange={(e) => setHours(e.target.value)}
      />
      <span className="dim" style={{ fontSize: 12 }}>hours</span>
      <button
        className="small"
        disabled={!valid || save.isPending}
        onClick={() => save.mutate(Math.round(parsed * 3600))}
      >
        {save.isPending ? "Saving…" : "Declare"}
      </button>
      {h.expected_fresh_within != null && (
        <button
          className="small"
          disabled={save.isPending}
          onClick={() => {
            setHours("");
            save.mutate(null);
          }}
        >
          Clear
        </button>
      )}
      <InlineError err={save.error} />
    </div>
  );
}

// ------------------------------------------------------------- one dataset row

function HealthRow({ h, canEdit, role }: { h: DatasetHealth; canEdit: boolean; role: Role }) {
  const [open, setOpen] = useState(false);
  const detail = h.detail;
  // Editor-and-above, like the rest of `detail`: the schedules whose last run
  // failed. The server names them only to roles that can read schedules.
  const failedSchedules = Array.isArray(detail?.schedule_run_failed)
    ? (detail!.schedule_run_failed as string[])
    : [];
  return (
    <div className="card" style={{ marginBottom: 8, padding: "10px 14px" }}>
      <div
        style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", flexWrap: "wrap" }}
        onClick={() => setOpen(!open)}
      >
        <span className="mono" style={{ fontWeight: 600 }}>{h.dataset}</span>
        <Badge tone={STATUS_TONE[h.status]}>{h.status}</Badge>
        {h.last_failure && <FailureBadge failure={h.last_failure} />}
        {h.schedule_overdue && (
          <span title="An enabled schedule targeting this dataset is past its window. If the scheduler process died, this is how it shows.">
            <Badge tone="red">schedule overdue</Badge>
          </span>
        )}
        {h.sync_failing && <Badge tone="red">sync failing</Badge>}
        {failedSchedules.length > 0 && (
          <span
            tabIndex={0}
            title="A schedule targeting this dataset failed on its last run — the failure detail below says why."
            aria-label="A schedule targeting this dataset failed on its last run — the failure detail below says why."
          >
            <Badge tone="red">schedule run failed</Badge>
          </span>
        )}
        {h.failing_expectations.map((e, i) => (
          <span
            key={i}
            title={`Expectation ${e.name} on ${e.column || "the dataset"} — severity ${e.severity}.`}
          >
            <Badge tone={e.severity === "error" ? "red" : "gold"}>
              {e.name}
              {e.column ? `(${e.column})` : ""}
            </Badge>
          </span>
        ))}
        <span style={{ flex: 1 }} />
        <span className="dim" style={{ fontSize: 12 }} title={fmtTime(h.last_success_at)}>
          {h.last_success_at ? `last success ${fmtAge(h.last_success_at)}` : "no successful version"}
        </span>
        {h.expected_fresh_within != null && (
          <span
            className="dim mono"
            style={{ fontSize: 12 }}
            title="Declared freshness window. Older than this => stale."
          >
            ≤ {fmtWindow(h.expected_fresh_within)}
          </span>
        )}
      </div>

      {open && (
        <div style={{ marginTop: 8 }}>
          {h.last_failure && <FailureNote failure={h.last_failure} role={role} />}
          {/* Editor-and-above detail: the server omits `detail` below editor,
              so this block simply does not render for a viewer. */}
          {detail?.expectations && detail.expectations.length > 0 && (
            <div className="mono dim" style={{ fontSize: 12, marginTop: 6 }}>
              {detail.expectations.map((e, i) => (
                <div key={i}>
                  {String(e.expectation ?? "")}
                  {e.measured !== undefined && e.measured !== null && (
                    <> — measured {String(e.measured)}</>
                  )}
                  {e.message && <> — {String(e.message)}</>}
                </div>
              ))}
            </div>
          )}
          {(detail?.transform || detail?.source || detail?.overdue_schedules || failedSchedules.length > 0) && (
            <div className="dim" style={{ fontSize: 12, marginTop: 6 }}>
              {detail?.transform && <span>transform <span className="mono">{String(detail.transform)}</span> · </span>}
              {detail?.source && <span>source <span className="mono">{String(detail.source)}</span> · </span>}
              {detail?.overdue_schedules && (
                <span>
                  overdue schedule{(detail.overdue_schedules as string[]).length > 1 ? "s" : ""}{" "}
                  <span className="mono">{(detail.overdue_schedules as string[]).join(", ")}</span>
                  {failedSchedules.length > 0 && " · "}
                </span>
              )}
              {failedSchedules.length > 0 && (
                <span>
                  failed schedule{failedSchedules.length > 1 ? "s" : ""}{" "}
                  <span className="mono">{failedSchedules.join(", ")}</span>
                </span>
              )}
            </div>
          )}
          {h.last_build_id && (
            <div className="dim" style={{ fontSize: 12, marginTop: 6 }}>
              last build{" "}
              {/* The id is a door, not a fact: `?build=` opens that card. */}
              <Link className="mono" to={`/builds?build=${encodeURIComponent(h.last_build_id)}`}>
                {h.last_build_id}
              </Link>
              {h.last_build_status && <> ({h.last_build_status})</>}
              {h.last_scheduled_run_at && <> · last scheduled run {fmtAge(h.last_scheduled_run_at)}</>}
            </div>
          )}
          {canEdit && <FreshnessEditor h={h} />}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------- events feed

function EventsFeed() {
  const q = useQuery({
    queryKey: ["health", "events"],
    queryFn: () => api.get<HealthEvent[]>(`${API}/health/events`),
  });
  if (q.isLoading) return <Spinner />;
  if (q.error) return <ErrorBox error={q.error} onRetry={() => q.refetch()} />;
  const events = q.data ?? [];
  if (events.length === 0) {
    return (
      <EmptyState>
        {/* no in-app door: transitions are RECORDED by the system, never
            created by a reader. */}
        No health transitions recorded yet — one is recorded when a dataset
        changes state, after a build or a scheduled sync runs.
      </EmptyState>
    );
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>When</th>
            <th>Dataset</th>
            <th>Event</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.seq}>
              <td className="dim" title={fmtTime(e.at)}>{fmtAge(e.at)}</td>
              <td className="mono">{e.dataset}</td>
              <td>
                <Badge tone={e.event === "dataset_healthy" ? "green" : "red"}>
                  {e.event.replace("dataset_", "")}
                </Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------- admin webhook note

function WebhookNote() {
  // Admin-only summary; config lives in Admin > Alerts. The list route is
  // ADMIN, so this quietly renders nothing for anyone else.
  const q = useQuery({
    queryKey: ["alert-webhooks"],
    queryFn: () => api.get<AlertWebhook[]>(`${API}/alerts/webhooks`),
    retry: false,
  });
  if (!q.data) return null;
  const enabled = q.data.filter((w) => w.enabled).length;
  return (
    <p className="dim" style={{ fontSize: 12.5 }}>
      Outbound alerts:{" "}
      {q.data.length === 0
        ? "no webhooks configured (off by default)"
        : `${enabled} of ${q.data.length} webhook${q.data.length === 1 ? "" : "s"} enabled`}{" "}
      — configured under Admin → Alert webhooks.
    </p>
  );
}

// ------------------------------------------------------------- view

/**
 * The rollup is a list of the datasets this reader may view. It may also
 * arrive wrapped, carrying one extra bit — `others_exist` — that says whether
 * the workspace holds datasets withheld from this reader.
 *
 * Why the bit is needed: an empty rollup has two causes that read identically
 * and mean opposite things. "You cannot see anything here" is a governance
 * fact; "there is nothing here yet" is a first-run fact with a next step. The
 * page used to state the first unconditionally, so an administrator standing
 * on a brand-new workspace was told they were being withheld from — the exact
 * inverse of the empty-vs-withheld rule the Datasets page already keeps.
 *
 * Why a boolean and nothing more: a count or a name would turn a health page
 * into an enumeration oracle for hidden datasets. One bit is what a 404 for a
 * withheld dataset already concedes, so it discloses nothing new.
 */
type HealthRollupBody =
  | DatasetHealth[]
  | { datasets: DatasetHealth[]; others_exist?: boolean };

function rollupRows(body: HealthRollupBody | undefined): DatasetHealth[] {
  if (body == null) return [];
  return Array.isArray(body) ? body : (body.datasets ?? []);
}

function rollupOthersExist(body: HealthRollupBody | undefined): boolean | undefined {
  if (body == null || Array.isArray(body)) return undefined;
  return body.others_exist;
}

export function HealthView() {
  const auth = useAuth();
  const canEdit = auth.can("editor");
  const rollup = useQuery({
    queryKey: ["health", "datasets"],
    queryFn: () => api.get<HealthRollupBody>(`${API}/health/datasets`),
    refetchInterval: 30_000, // a health page that goes stale is its own joke
  });
  const rows = rollupRows(rollup.data);
  // The one unquantified bit: does the workspace hold datasets this reader
  // cannot view? Never a count, never a name — the same bit a 404 for a
  // withheld dataset already concedes. Absent (an older server) means we do
  // not know, and a surface that does not know must not guess.
  const othersExist = rollupOthersExist(rollup.data);

  const sections = useMemo(() => {
    const by: Partial<Record<HealthStatus, DatasetHealth[]>> = {};
    for (const h of rows) (by[h.status] ??= []).push(h);
    return by;
  }, [rollup.data]);

  const redCount =
    (sections.failing?.length ?? 0) +
    (sections.overdue?.length ?? 0) +
    (sections.stale?.length ?? 0);

  return (
    <div>
      <PageHeader
        title="Health"
        subtitle="Freshness, failing builds and expectations, and schedules that went quiet — for the datasets you can read."
      />
      {auth.can("admin") && <WebhookNote />}

      {rollup.isLoading && <Spinner />}
      {rollup.error != null && (
        <ErrorBox error={rollup.error} onRetry={() => rollup.refetch()} />
      )}

      {rollup.data && rows.length === 0 && (
        <EmptyState>
          {othersExist === false ? (
            <>
              No datasets yet — import a file on{" "}
              <Link to="/datasets">Datasets</Link> to make one.
            </>
          ) : (
            // Either the server told us other datasets exist, or it is old
            // enough not to say. Both cases keep the withholding sentence:
            // claiming emptiness we have not established would be the same
            // false-green class of bug pointed the other way.
            <>No datasets you can read.</>
          )}
        </EmptyState>
      )}

      {rollup.data && rows.length > 0 && redCount === 0 && (
        <div className="card" style={{ marginBottom: 16 }}>
          <Badge tone="green">all clear</Badge>{" "}
          <span className="dim" style={{ fontSize: 13 }}>
            Nothing failing, overdue or stale among the datasets you can read.
          </span>
        </div>
      )}

      {SECTION_ORDER.map((status) => {
        const rows = sections[status];
        if (!rows || rows.length === 0) return null;
        const collapsed = status === "healthy" || status === "unknown";
        const body = rows
          .slice()
          .sort((a, b) => a.dataset.localeCompare(b.dataset))
          .map((h) => <HealthRow key={h.dataset} h={h} canEdit={canEdit} role={auth.role} />);
        return (
          <section key={status} style={{ marginBottom: 20 }}>
            {collapsed ? (
              <details>
                <summary style={{ cursor: "pointer", marginBottom: 8 }}>
                  <span style={{ fontSize: 15, fontWeight: 600 }}>
                    {SECTION_LABEL[status]} ({rows.length})
                  </span>{" "}
                  <span className="dim" style={{ fontSize: 12.5 }}>{SECTION_HINT[status]}</span>
                </summary>
                {body}
              </details>
            ) : (
              <>
                <h2 style={{ fontSize: 15, marginBottom: 4 }}>
                  {SECTION_LABEL[status]} ({rows.length})
                </h2>
                <p className="dim" style={{ fontSize: 12.5, marginTop: 0 }}>
                  {SECTION_HINT[status]}
                </p>
                {body}
              </>
            )}
          </section>
        );
      })}

      <section style={{ marginTop: 28 }}>
        <h2 style={{ fontSize: 15, marginBottom: 8 }}>Recent transitions</h2>
        <EventsFeed />
      </section>
    </div>
  );
}
