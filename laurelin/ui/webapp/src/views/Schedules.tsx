// Schedules: the piece that makes a pipeline run without anyone pressing a
// button. A schedule binds a trigger — a cron expression, or "a dataset gained
// a version" — to an action — a build, or a connector sync.
//
// Editor-gated, like builds, because a schedule runs pipeline code. Firing is
// exactly-once across replicas (leases), so this page is the same on every
// node.

import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../api";
import { useAuth } from "../auth";
import type {
  AuthoringWarning,
  Schedule,
  ScheduleAction,
  ScheduleTrigger,
  TransformSummary,
} from "../types";
import {
  Badge,
  Column,
  DataTable,
  EmptyState,
  ErrorBox,
  FailureBadge,
  FailureNote,
  LiveStatus,
  Modal,
  NAME_RULE,
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
  // #75 on the schedules surface: a target this reader may not view is
  // dropped by the server and the row says so with one unquantified bit
  // rather than reading as a shorter true list.
  const hidden = s.hidden_targets ? " + not shared with you" : "";
  if (!s.targets.length) return s.hidden_targets ? `build${hidden}` : "build all";
  return `build ${s.targets.join(", ")}${hidden}`;
}

export function SchedulesView() {
  // `auth.can` and not `user.role`: in multi-workspace mode the effective role
  // is the membership role in the active workspace, and reading the account
  // role here showed an editor's controls to a workspace viewer.
  const auth = useAuth();
  const canEdit = auth.can("editor");
  const qc = useQueryClient();
  const [editing, setEditing] = useState<Schedule | null>(null);
  // `?target=<dataset>`: the "Schedule builds" door on a dataset page carries
  // which dataset the user was looking at, so landing here opens the editor
  // with that target already ticked instead of making them re-pick it from
  // scratch. Read once at mount; the param is a handoff, not state.
  const [params, setParams] = useSearchParams();
  useEffect(() => {
    const target = params.get("target");
    if (target && canEdit) {
      setEditing({ ...BLANK, targets: [target] });
      setParams({}, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canEdit]);
  // Authoring hints from the last save. The write already succeeded — this used
  // to be a 400 that refused it — so they belong beside the list, not inside a
  // modal that is closing.
  const [warnings, setWarnings] = useState<AuthoringWarning[]>([]);
  // Run-now in flight: the schedule we fired and the last_run_at it had when
  // we fired it. While set, the list polls; when the row's last_run_at moves,
  // the run has landed and `outcome` says how it went. Without this, the
  // click was fire-and-forget: the row said "never" until a manual refresh.
  const [watching, setWatching] = useState<{ name: string; prevRunAt: string | null } | null>(null);
  const [outcome, setOutcome] = useState<Schedule | null>(null);

  const q = useQuery({
    queryKey: ["schedules"],
    queryFn: () => api.get<Schedule[]>(`${API}/schedules`),
    // Editor-gated on the server. Asking anyway buys a 403 in a red box, which
    // reads as a fault rather than as the deliberate boundary it is.
    enabled: canEdit,
    // Poll only while a run we triggered is pending, so the row converges.
    refetchInterval: watching ? 2000 : false,
  });

  const watchedRow = watching ? q.data?.find((r) => r.name === watching.name) : undefined;
  useEffect(() => {
    if (
      watching &&
      watchedRow &&
      watchedRow.last_run_at !== watching.prevRunAt &&
      // The server projects `last_status` through the queued build, so a row
      // can read "running" after the firing lands. Keep polling until the
      // build settles — "finished: running." is not an outcome.
      watchedRow.last_status !== "running"
    ) {
      setOutcome(watchedRow);
      setWatching(null);
    }
  }, [watching, watchedRow]);

  const invalidate = () => qc.invalidateQueries({ queryKey: ["schedules"] });

  const runNow = useMutation({
    mutationFn: (s: Schedule) => api.post(`${API}/schedules/${s.name}/run`, {}),
    onSuccess: (_data, s) => {
      setOutcome(null);
      setWatching({ name: s.name, prevRunAt: s.last_run_at });
    },
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
            {/* What the run actually produced, one click away. */}
            {s.last_build_id && (
              <Link
                className="mono"
                style={{ fontSize: 12 }}
                to={`/builds?build=${encodeURIComponent(s.last_build_id)}`}
              >
                build →
              </Link>
            )}
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
            disabled={runNow.isPending || watching?.name === s.name}
            onClick={() => runNow.mutate(s)}
            title="Fire now, without waiting for the window"
          >
            {watching?.name === s.name ? "Running…" : "Run now"}
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
        subtitle="Build pipelines and sync sources on a schedule, or when an upstream dataset changes."
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
            <Link to="/builds">Builds</Link> page: every build, its status,
            and when it ran.
          </p>
        </div>
      )}
      {q.isLoading && canEdit && <Spinner />}
      {q.isError && <ErrorBox error={q.error} onRetry={() => q.refetch()} />}
      {(runNow.error || remove.error) && (
        <ErrorBox error={runNow.error || remove.error} />
      )}
      {/* Run-now acknowledges, then converges: announced live, so the click
          has an audible/visible receipt, and the sentence changes to the
          outcome when the polled row reflects it. */}
      {watching && (
        <LiveStatus className="dim" style={{ fontSize: 13, margin: "8px 0" }}>
          Run of <span className="mono">{watching.name}</span> requested — the row
          below updates when it finishes.
        </LiveStatus>
      )}
      {outcome && !watching && (
        <LiveStatus className="dim" style={{ fontSize: 13, margin: "8px 0" }}>
          {/* When the firing produced a build, this is a BUILD outcome and it
              says so in the one converged sentence family (see the note at the
              matching site in Flows.tsx): "Build <id> finished: <outcome>."
              then "See the build." and nothing after. A sync firing produces
              no build, so it keeps its own subject — that is a different fact,
              not a second phrasing of the same one. */}
          {outcome.last_build_id ? (
            <>
              <strong>
                Build{" "}
                <span className="mono">{outcome.last_build_id}</span> finished:{" "}
                {outcome.last_status ?? "unknown"}.
              </strong>{" "}
              <Link to={`/builds?build=${encodeURIComponent(outcome.last_build_id)}`}>
                See the build
              </Link>
              .
            </>
          ) : (
            <>
              Run of <span className="mono">{outcome.name}</span> finished:{" "}
              {outcome.last_status ?? "unknown"}.
            </>
          )}
        </LiveStatus>
      )}
      <WarningBox warnings={warnings} />
      {q.data &&
        (q.data.length === 0 ? (
          <EmptyState>
            No schedules yet.
            {canEdit ? " Create one with the button above to build a pipeline on its own." : ""}
          </EmptyState>
        ) : (
          <>
            <DataTable columns={columns} rows={q.data} rowKey={(s) => s.name} />
            {q.data.filter((s) => s.last_failure).map((s) => (
              <div key={s.name} style={{ marginTop: 10 }}>
                <div className="mono dim" style={{ fontSize: 12 }}>{s.name}</div>
                <FailureNote failure={s.last_failure!} role={auth.role} />
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

  // The pipelines that exist are known to the server and rendered as pickers
  // everywhere else (the builder, Explore, the SQL sidebar) — a bare text
  // input here was a typo tax the save-then-warn banner then collected. The
  // known outputs become checkboxes; the free-text box stays for a target
  // that will exist later (authoring a schedule before its pipeline is a
  // supported order — the warning covers it).
  const transformsQ = useQuery({
    queryKey: ["transforms"],
    queryFn: () => api.get<TransformSummary[]>(`${API}/transforms`),
    enabled: s.action === "build",
    staleTime: 30_000,
  });
  const knownTargets = [...new Set((transformsQ.data ?? []).map((t) => t.output))].sort();
  // Free-text targets (not produced by any known pipeline yet), kept as the
  // author typed them; seeded once, after the pipeline list arrives, so a
  // target the list DOES know starts as a ticked box, not as text.
  const [extraText, setExtraText] = useState("");
  const [extraSeeded, setExtraSeeded] = useState(false);
  useEffect(() => {
    if (extraSeeded || !transformsQ.data) return;
    const ks = new Set(transformsQ.data.map((t) => t.output));
    setExtraText(initial.targets.filter((t) => !ks.has(t)).join(", "));
    setExtraSeeded(true);
  }, [extraSeeded, transformsQ.data, initial.targets]);
  const parseExtra = (text: string) =>
    text.split(",").map((t) => t.trim()).filter(Boolean);
  const setTargets = (checked: string[], extra: string) => {
    const merged = [...checked, ...parseExtra(extra).filter((t) => !checked.includes(t))];
    set({ targets: merged });
  };

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
    <Modal
      label={isNew ? "New schedule" : `Edit schedule ${initial.name}`}
      onClose={onClose}
      width={560}
    >
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
            <p className="hint">{NAME_RULE}</p>
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
              <option value="build">Build</option>
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
            {extraSeeded && knownTargets.length > 0 ? (
              <>
                <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 14px", margin: "2px 0 6px" }}>
                  {knownTargets.map((o) => (
                    <label key={o} className="check-inline" style={{ fontWeight: 400 }}>
                      <input
                        type="checkbox"
                        checked={s.targets.includes(o)}
                        onChange={(e) => {
                          const checked = knownTargets.filter((k) =>
                            k === o ? e.target.checked : s.targets.includes(k),
                          );
                          setTargets(checked, extraText);
                        }}
                      />
                      <span className="mono">{o}</span>
                    </label>
                  ))}
                </div>
                <input
                  className="mono"
                  value={extraText}
                  onChange={(e) => {
                    setExtraText(e.target.value);
                    setTargets(
                      knownTargets.filter((k) => s.targets.includes(k)),
                      e.target.value,
                    );
                  }}
                  placeholder="a target that doesn't exist yet, comma-separated"
                />
                <p className="hint">
                  Nothing ticked and nothing typed = build everything. A target
                  named before its pipeline exists saves with a warning, and
                  builds will fail until the pipeline does.
                </p>
              </>
            ) : (
              // Pipeline list still loading (or empty, or unavailable): the
              // plain input is the whole control, exactly as before.
              <input
                className="mono"
                value={s.targets.join(", ")}
                onChange={(e) =>
                  set({ targets: e.target.value.split(",").map((t) => t.trim()).filter(Boolean) })
                }
                placeholder="empty = build everything"
              />
            )}
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
    </Modal>
  );
}
