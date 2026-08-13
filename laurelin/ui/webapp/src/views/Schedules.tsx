// Schedules: the piece that makes a pipeline run without anyone pressing a
// button. A schedule binds a trigger — a cron expression, or "a dataset gained
// a version" — to an action — run a build, or sync a connector.
//
// Editor-gated, like builds, because a schedule runs pipeline code. Firing is
// exactly-once across replicas (leases), so this page is the same on every
// node.

import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../api";
import { useAuth } from "../auth";
import type {
  AuthoringWarning,
  Schedule,
  ScheduleAction,
  ScheduleTrigger,
} from "../types";
import {
  Badge,
  Column,
  DataTable,
  EmptyState,
  ErrorBox,
  FailureBadge,
  FailureNote,
  PageHeader,
  Spinner,
  WarningBox,
  fmtTime,
} from "../ui";

const NAME_RE = /^[a-z][a-z0-9_-]{0,63}$/;

const BLANK: Schedule = {
  name: "",
  enabled: true,
  trigger: "cron",
  cron: "0 2 * * *",
  upstream_dataset: "",
  action: "build",
  targets: [],
  source: "",
  next_run_at: null,
  last_run_at: null,
  last_status: null,
  last_failure: null,
  last_build_id: null,
  created_at: "",
  created_by: "",
};

function statusTone(s: Schedule["last_status"]): "green" | "red" | "neutral" {
  return s === "succeeded" ? "green" : s === "failed" ? "red" : "neutral";
}

function triggerSummary(s: Schedule): string {
  return s.trigger === "cron"
    ? `cron · ${s.cron}`
    : `when ${s.upstream_dataset || "?"} updates`;
}

function actionSummary(s: Schedule): string {
  if (s.action === "sync") return `sync ${s.source || "?"}`;
  return s.targets.length ? `build ${s.targets.join(", ")}` : "build all";
}

export function SchedulesView() {
  // `auth.can` and not `user.role`: in multi-workspace mode the effective role
  // is the membership role in the active workspace, and reading the account
  // role here showed an editor's controls to a workspace viewer.
  const auth = useAuth();
  const canEdit = auth.can("editor");
  const qc = useQueryClient();
  const [editing, setEditing] = useState<Schedule | null>(null);
  // Authoring hints from the last save. The write already succeeded — this used
  // to be a 400 that refused it — so they belong beside the list, not inside a
  // modal that is closing.
  const [warnings, setWarnings] = useState<AuthoringWarning[]>([]);

  const q = useQuery({
    queryKey: ["schedules"],
    queryFn: () => api.get<Schedule[]>(`${API}/schedules`),
    // Editor-gated on the server. Asking anyway buys a 403 in a red box, which
    // reads as a fault rather than as the deliberate boundary it is.
    enabled: canEdit,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["schedules"] });

  const runNow = useMutation({
    mutationFn: (name: string) => api.post(`${API}/schedules/${name}/run`, {}),
    onSettled: invalidate,
  });
  const remove = useMutation({
    mutationFn: (name: string) => api.del(`${API}/schedules/${name}`),
    onSettled: invalidate,
  });

  const columns: Column<Schedule>[] = [
    {
      label: "Name",
      className: "mono",
      render: (s) => (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          {s.name}
          {!s.enabled && <Badge tone="neutral">paused</Badge>}
        </span>
      ),
    },
    { label: "Trigger", render: (s) => <span className="dim">{triggerSummary(s)}</span> },
    { label: "Action", render: (s) => <span className="dim">{actionSummary(s)}</span> },
    {
      label: "Next run",
      render: (s) =>
        s.enabled && s.trigger === "cron" ? (
          <span className="dim">{fmtTime(s.next_run_at)}</span>
        ) : (
          <span className="faint">—</span>
        ),
    },
    {
      label: "Last run",
      render: (s) =>
        s.last_run_at ? (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            {/* R1: `last_error` held whatever the driver said on a failed sync,
                and this row is one privilege level below the person who wrote
                the connection string. The code is what an operator actually
                acts on. */}
            {s.last_failure ? (
              <FailureBadge failure={s.last_failure} />
            ) : (
              <Badge tone={statusTone(s.last_status)}>{s.last_status}</Badge>
            )}
            <span className="dim">{fmtTime(s.last_run_at)}</span>
          </span>
        ) : (
          <span className="faint">never</span>
        ),
    },
  ];

  if (canEdit) {
    columns.push({
      label: "",
      render: (s) => (
        <span className="toolbar" style={{ gap: 6 }}>
          <button
            className="small"
            disabled={runNow.isPending}
            onClick={() => runNow.mutate(s.name)}
            title="Fire now, without waiting for the window"
          >
            Run now
          </button>
          <button className="small" onClick={() => setEditing(s)}>
            Edit
          </button>
          <button
            className="small"
            onClick={() => {
              if (confirm(`Delete schedule ${s.name}?`)) remove.mutate(s.name);
            }}
          >
            Delete
          </button>
        </span>
      ),
    });
  }

  return (
    <div>
      <PageHeader
        title="Schedules"
        subtitle="Run builds and syncs on a cron, or when an upstream dataset changes."
        actions={
          canEdit ? (
            <button className="primary" onClick={() => setEditing({ ...BLANK })}>
              New schedule
            </button>
          ) : undefined
        }
      />

      {!canEdit && (
        <div className="withheld-box">
          <div className="withheld-head">Schedules are not shown to your role</div>
          <p>
            They reach an editor and above — the level that can write them. A
            schedule names a source or a build target, and firing one runs
            pipeline code.
          </p>
          <p style={{ marginTop: 8 }}>
            What the schedules actually produced is on the{" "}
            <Link to="/pipeline">Pipeline</Link> tab: every build, its status,
            and when it ran.
          </p>
        </div>
      )}
      {q.isLoading && canEdit && <Spinner />}
      {q.isError && <ErrorBox error={q.error} />}
      {(runNow.error || remove.error) && (
        <ErrorBox error={runNow.error || remove.error} />
      )}
      <WarningBox warnings={warnings} />
      {q.data &&
        (q.data.length === 0 ? (
          <EmptyState>
            No schedules yet.{canEdit ? " Create one to run a pipeline on its own." : ""}
          </EmptyState>
        ) : (
          <>
            <DataTable columns={columns} rows={q.data} rowKey={(s) => s.name} />
            {q.data.filter((s) => s.last_failure).map((s) => (
              <div key={s.name} style={{ marginTop: 10 }}>
                <div className="mono dim" style={{ fontSize: 12 }}>{s.name}</div>
                <FailureNote failure={s.last_failure!} />
              </div>
            ))}
          </>
        ))}

      {editing && (
        <ScheduleEditor
          initial={editing}
          existingNames={new Set((q.data ?? []).map((s) => s.name))}
          onClose={() => setEditing(null)}
          onSaved={(w) => {
            invalidate();
            setWarnings(w);
            setEditing(null);
          }}
        />
      )}
    </div>
  );
}

function ScheduleEditor({
  initial,
  existingNames,
  onClose,
  onSaved,
}: {
  initial: Schedule;
  existingNames: Set<string>;
  onClose: () => void;
  onSaved: (warnings: AuthoringWarning[]) => void;
}) {
  const [s, setS] = useState<Schedule>({ ...initial });
  const isNew = initial.name === "";
  const set = (patch: Partial<Schedule>) => setS((old) => ({ ...old, ...patch }));

  const save = useMutation<Schedule, Error, void>({
    mutationFn: () =>
      api.put<Schedule>(`${API}/schedules/${s.name}`, {
        enabled: s.enabled,
        trigger: s.trigger,
        cron: s.cron,
        upstream_dataset: s.upstream_dataset,
        action: s.action,
        targets: s.targets,
        source: s.source,
      }),
    onSuccess: (saved) => onSaved(saved.warnings ?? []),
  });

  const nameValid = NAME_RE.test(s.name);
  const nameTaken = isNew && existingNames.has(s.name);
  // The server validates the cron and the rest; the client only guards the
  // fields the chosen trigger/action actually uses, so Save isn't offered for
  // an obviously empty form.
  const complete =
    nameValid &&
    !nameTaken &&
    (s.trigger === "cron" ? s.cron.trim() !== "" : s.upstream_dataset.trim() !== "") &&
    (s.action === "sync" ? s.source.trim() !== "" : true);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" style={{ width: 560, maxWidth: "92vw" }} onClick={(e) => e.stopPropagation()}>
        <div className="card-title">{isNew ? "New schedule" : `Edit ${initial.name}`}</div>

        <div className="field">
          <label>Name</label>
          <input
            className="mono"
            value={s.name}
            disabled={!isNew}
            onChange={(e) => set({ name: e.target.value })}
            placeholder="nightly_rollup"
          />
          {isNew && !nameValid && s.name !== "" && (
            <p className="hint">Lowercase letters, digits, `-` and `_`; start with a letter.</p>
          )}
          {nameTaken && <p className="hint">A schedule with that name already exists.</p>}
        </div>

        <div className="toolbar" style={{ gap: 16, flexWrap: "wrap" }}>
          <div className="field">
            <label>Trigger</label>
            <select
              value={s.trigger}
              onChange={(e) => set({ trigger: e.target.value as ScheduleTrigger })}
            >
              <option value="cron">On a schedule (cron)</option>
              <option value="upstream">When a dataset changes</option>
            </select>
          </div>
          <div className="field">
            <label>Action</label>
            <select
              value={s.action}
              onChange={(e) => set({ action: e.target.value as ScheduleAction })}
            >
              <option value="build">Run a build</option>
              <option value="sync">Sync a source</option>
            </select>
          </div>
          <label className="field" style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
            <input
              type="checkbox"
              checked={s.enabled}
              onChange={(e) => set({ enabled: e.target.checked })}
            />
            Enabled
          </label>
        </div>

        {s.trigger === "cron" ? (
          <div className="field">
            <label>Cron expression</label>
            <input
              className="mono"
              value={s.cron}
              onChange={(e) => set({ cron: e.target.value })}
              placeholder="0 2 * * *"
            />
            <p className="hint">Standard 5-field cron. `0 2 * * *` is every night at 02:00.</p>
          </div>
        ) : (
          <div className="field">
            <label>Upstream dataset</label>
            <input
              className="mono"
              value={s.upstream_dataset}
              onChange={(e) => set({ upstream_dataset: e.target.value })}
              placeholder="raw_orders"
            />
            <p className="hint">Fires when this dataset gains a new version.</p>
          </div>
        )}

        {s.action === "build" ? (
          <div className="field">
            <label>Build targets (optional)</label>
            <input
              className="mono"
              value={s.targets.join(", ")}
              onChange={(e) =>
                set({ targets: e.target.value.split(",").map((t) => t.trim()).filter(Boolean) })
              }
              placeholder="empty = build everything"
            />
          </div>
        ) : (
          <div className="field">
            <label>Source to sync</label>
            <input
              className="mono"
              value={s.source}
              onChange={(e) => set({ source: e.target.value })}
              placeholder="orders_pull"
            />
          </div>
        )}

        {save.error && <ErrorBox error={save.error} />}

        <div className="toolbar" style={{ marginTop: 12, justifyContent: "flex-end" }}>
          <button onClick={onClose}>Cancel</button>
          <button
            className="primary"
            disabled={!complete || save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending ? "Saving…" : "Save schedule"}
          </button>
        </div>
      </div>
    </div>
  );
}
