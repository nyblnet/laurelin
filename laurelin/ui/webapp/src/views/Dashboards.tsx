// Dashboards: grids of saved queries rendered as charts.
//
// R2 splits a dashboard down the middle. The *picture* is presentation and
// everyone who can open the board gets it: the panel's title, chart kind, axes
// and width. The *query* is an instruction an editor wrote, and it is served
// only to a principal who could have written it — a viewer's panel arrives with
// no `sql` key at all.
//
// A viewer still gets a working dashboard, because the rows now come from
// POST /dashboards/{name}/panels/{id}/run, which loads the stored panel and
// executes it **as the caller**. The old invariant — "storing a dashboard
// grants nobody new read access" — holds for a new reason: it used to be true
// because the browser ran the query with the viewer's credentials, and it is
// true now because the server does, down the same ACL / row-security / masking
// path. Two viewers with different row policies still see different rows in the
// same panel. Neither of them ever receives the SQL.
//
// Editing goes through the per-panel routes rather than re-PUTting the board.
// That is not tidiness: a read-modify-write client that ever holds a trimmed
// panel would blank the query of every panel it did not touch.

import { useState } from "react";
import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../api";
import { useAuth } from "../auth";
import { Chart } from "../charts";
import type {
  AuthoringWarning,
  ChartKind,
  Dashboard,
  DashboardPanel,
  ObjectTypeDef,
  PanelRunResult,
} from "../types";
import { panelIsWhole } from "../types";
import {
  Column,
  DataTable,
  EmptyState,
  ErrorBox,
  PageHeader,
  Spinner,
  WarningBox,
  Withheld,
  fmtTime,
} from "../ui";

const NAME_RE = /^[a-z][a-z0-9_-]{0,63}$/;
const CHART_KINDS: ChartKind[] = ["table", "bar", "line", "area", "stat", "pie", "scatter"];

export function DashboardsView() {
  return (
    <Routes>
      <Route path="/" element={<DashboardList />} />
      <Route path=":name" element={<DashboardPage />} />
    </Routes>
  );
}

function newPanelId(): string {
  return Math.random().toString(36).slice(2, 10);
}

// ------------------------------------------------------------------- list

function DashboardList() {
  const auth = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [newName, setNewName] = useState("");
  const [newTitle, setNewTitle] = useState("");

  const dashQ = useQuery({
    queryKey: ["dashboards"],
    queryFn: () => api.get<Dashboard[]>(`${API}/dashboards`),
  });

  const create = useMutation({
    mutationFn: () =>
      api.put<Dashboard>(`${API}/dashboards/${newName.trim()}`, {
        title: newTitle.trim() || newName.trim(),
        panels: [],
      }),
    onSuccess: (d) => {
      qc.invalidateQueries({ queryKey: ["dashboards"] });
      navigate(`/dashboards/${d.name}`);
    },
  });

  const columns: Column<Dashboard>[] = [
    {
      label: "Dashboard",
      render: (d) => (
        <Link to={`/dashboards/${d.name}`}>{d.title || d.name}</Link>
      ),
    },
    { label: "Name", className: "mono dim", render: (d) => d.name },
    { label: "Panels", className: "num", render: (d) => String(d.panels.length) },
    { label: "Updated", render: (d) => <span className="dim">{fmtTime(d.updated_at)}</span> },
  ];

  const nameOk = NAME_RE.test(newName.trim());

  return (
    <div>
      <PageHeader
        title="Dashboards"
        subtitle="Saved queries rendered as charts — every panel respects the viewer's data access."
      />
      {auth.can("editor") && (
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="toolbar" style={{ marginBottom: 0, gap: 12, flexWrap: "wrap" }}>
            <div className="field">
              <label>Name</label>
              <input
                className="mono"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="revenue"
                autoComplete="off"
              />
            </div>
            <div className="field" style={{ flex: "1 1 220px" }}>
              <label>Title</label>
              <input
                value={newTitle}
                onChange={(e) => setNewTitle(e.target.value)}
                placeholder="Revenue overview"
                autoComplete="off"
              />
            </div>
            <button
              className="primary"
              disabled={!nameOk || create.isPending}
              onClick={() => create.mutate()}
            >
              {create.isPending ? "Creating…" : "Create dashboard"}
            </button>
          </div>
          {create.isError && <ErrorBox error={create.error} />}
        </div>
      )}
      {dashQ.isLoading ? (
        <Spinner />
      ) : dashQ.isError ? (
        <ErrorBox error={dashQ.error} />
      ) : dashQ.data!.length === 0 ? (
        <EmptyState>
          No dashboards yet
          {auth.can("editor") ? (
            <>
              {/* Both doors here can actually put a panel on a dashboard.
                  This used to point at Analyses, which cannot — a user who
                  obeyed the hint built a chart there and then stalled with no
                  way to finish the task. */}
              {" "}— create one above, shape a chart by clicking in{" "}
              <Link to="/explore">Explore</Link> and save it here, or send a query
              from <Link to="/workbench">SQL</Link>.
            </>
          ) : (
            "."
          )}
        </EmptyState>
      ) : (
        <DataTable
          columns={columns}
          rows={dashQ.data!}
          rowKey={(d) => d.name}
          onRowClick={(d) => navigate(`/dashboards/${d.name}`)}
        />
      )}
    </div>
  );
}

// ------------------------------------------------------------------- panel

/**
 * One route runs both panel sources, and the client no longer knows or needs to
 * know which one a panel is.
 *
 * This used to branch: SQL panels went to POST /query with the SQL the client
 * was holding, object panels to /aggregate with the group_by the client was
 * holding. Both required the client to have the instruction, which is exactly
 * what a viewer no longer receives. The server holds it, applies the caller's
 * permissions, and returns {columns, rows} either way.
 */
function PanelBody({ dashboard, panel }: { dashboard: string; panel: DashboardPanel }) {
  const q = useQuery({
    // Keyed on identity, not contents. The key used to include the SQL, so
    // editing a panel refetched it for free — and a viewer now has no contents
    // to key on at all. Writes invalidate the `["panel-run", <dashboard>]`
    // prefix instead (see `afterWrite`), which is the only thing that can still
    // tell this cache the query changed.
    queryKey: ["panel-run", dashboard, panel.id],
    queryFn: () =>
      api.post<PanelRunResult>(
        `${API}/dashboards/${encodeURIComponent(dashboard)}/panels/${encodeURIComponent(panel.id)}/run`,
        { max_rows: 1000 },
      ),
    staleTime: 30_000,
  });

  if (q.isLoading) return <Spinner />;
  if (q.isError) return <ErrorBox error={q.error} />;
  const result = q.data!;

  if (panel.chart === "table" || result.columns.length === 0) {
    return <PanelTable result={result} />;
  }
  return (
    <Chart
      data={result}
      kind={panel.chart}
      x={panel.x}
      y={panel.y}
      series={panel.series}
      stacked={panel.stacked}
    />
  );
}

/**
 * The table panel, honest about what it holds. It used to slice the first 100
 * rows silently — a 1000-row result rendered 100 with nothing anywhere saying
 * so, which for a table (the one kind whose whole job is showing the rows) is
 * a wrong answer, not a style choice. Every fetched row renders now, the
 * count is stated, a server-side truncation gets a badge, and header-click
 * sorting is client-side over the loaded rows — which is exactly why the
 * badge exists: "sorted within the first 1000" is only honest if the reader
 * can see the "first 1000" part.
 */
function PanelTable({ result }: { result: PanelRunResult }) {
  const [sort, setSort] = useState<{ column: string; dir: 1 | -1 } | null>(null);

  const rows = result.rows;
  const sorted =
    sort === null
      ? rows
      : [...rows].sort((a, b) => {
          const va = a[sort.column];
          const vb = b[sort.column];
          if (va == null) return 1;
          if (vb == null) return -1;
          if (typeof va === "number" && typeof vb === "number")
            return (va - vb) * sort.dir;
          return String(va).localeCompare(String(vb)) * sort.dir;
        });

  return (
    <div>
      <div className="table-wrap" style={{ maxHeight: 280, overflowY: "auto" }}>
        <table>
          <thead>
            <tr>
              {result.columns.map((c) => (
                <th
                  key={c}
                  className="mono"
                  style={{ cursor: "pointer", userSelect: "none" }}
                  title="Sort by this column (within the loaded rows)"
                  onClick={() =>
                    setSort((s) =>
                      s?.column === c
                        ? s.dir === 1
                          ? { column: c, dir: -1 }
                          : null
                        : { column: c, dir: 1 },
                    )
                  }
                >
                  {c}
                  {sort?.column === c ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.map((row, i) => (
              <tr key={i}>
                {result.columns.map((c) => (
                  <td key={c} className="mono">{String(row[c] ?? "")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="faint" style={{ fontSize: 11, marginTop: 6, display: "flex", gap: 8, alignItems: "center" }}>
        {result.row_count.toLocaleString("en-US")} row{result.row_count === 1 ? "" : "s"}
        {result.truncated && (
          <span
            className="badge badge-gold"
            title="The result is larger than what was fetched. Sorting here reorders only the loaded rows."
          >
            first {result.row_count.toLocaleString("en-US")} of a larger result
          </span>
        )}
        {sort && result.truncated && <span>· sorted within loaded rows</span>}
      </div>
    </div>
  );
}

// ------------------------------------------------- object panel source fields

const AGG_OPS = ["count", "count_distinct", "sum", "avg", "min", "max", "median"];

function ObjectSourceFields({
  p,
  set,
  types,
}: {
  p: DashboardPanel;
  set: (patch: Partial<DashboardPanel>) => void;
  types: ObjectTypeDef[];
}) {
  const type = types.find((t) => t.api_name === p.object_type);
  const properties = Object.keys(type?.properties ?? {});
  const metrics = p.metrics ?? [];

  const setMetric = (i: number, patch: Partial<(typeof metrics)[number]>) =>
    set({ metrics: metrics.map((m, j) => (j === i ? { ...m, ...patch } : m)) });

  return (
    <>
      <div className="toolbar" style={{ gap: 12, flexWrap: "wrap" }}>
        <div className="field">
          <label>Object type</label>
          <select
            value={p.object_type}
            // Properties belong to a type, so carrying the old ones over
            // would leave a panel grouping by a field that no longer exists.
            onChange={(e) => set({ object_type: e.target.value, group_by: [], metrics: [{ op: "count", alias: "count" }] })}
          >
            {types.map((t) => (
              <option key={t.api_name} value={t.api_name}>
                {t.display_name || t.api_name}
              </option>
            ))}
          </select>
        </div>
        <div className="field" style={{ flex: "1 1 200px" }}>
          <label>Group by</label>
          <select
            multiple
            value={p.group_by ?? []}
            onChange={(e) =>
              set({ group_by: Array.from(e.target.selectedOptions, (o) => o.value) })
            }
            size={Math.min(4, Math.max(2, properties.length))}
          >
            {properties.map((prop) => (
              <option key={prop} value={prop}>{prop}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="field">
        <label>Metrics</label>
        {metrics.map((m, i) => (
          <div className="toolbar" key={i} style={{ gap: 8, marginBottom: 6 }}>
            <select value={m.op} onChange={(e) => setMetric(i, { op: e.target.value })}>
              {AGG_OPS.map((op) => (
                <option key={op} value={op}>{op}</option>
              ))}
            </select>
            <select
              value={m.property ?? ""}
              onChange={(e) => setMetric(i, { property: e.target.value || null })}
              disabled={m.op === "count"}
              title={m.op === "count" ? "count needs no property" : undefined}
            >
              <option value="">—</option>
              {properties.map((prop) => (
                <option key={prop} value={prop}>{prop}</option>
              ))}
            </select>
            <input
              className="mono"
              value={m.alias ?? ""}
              onChange={(e) => setMetric(i, { alias: e.target.value })}
              placeholder="alias"
              style={{ width: 130 }}
            />
            <button
              className="small"
              disabled={metrics.length === 1}
              title={metrics.length === 1 ? "a panel needs at least one metric" : undefined}
              onClick={() => set({ metrics: metrics.filter((_, j) => j !== i) })}
            >
              Remove
            </button>
          </div>
        ))}
        <button
          className="small"
          onClick={() => set({ metrics: [...metrics, { op: "count", alias: `metric_${metrics.length + 1}` }] })}
        >
          Add metric
        </button>
      </div>

      <div className="field">
        <label>Search (optional)</label>
        <input
          value={p.search ?? ""}
          onChange={(e) => set({ search: e.target.value })}
          placeholder="narrow the objects before grouping"
        />
      </div>
    </>
  );
}

// ------------------------------------------------------------- panel editor

function PanelEditor({
  initial,
  dashboard,
  onSave,
  onCancel,
}: {
  initial: DashboardPanel;
  /** The board the panel lands on, so the no-code pointer prefills it. */
  dashboard: string;
  onSave: (p: DashboardPanel) => void;
  onCancel: () => void;
}) {
  const [p, setP] = useState<DashboardPanel>({ ...initial });
  const set = (patch: Partial<DashboardPanel>) => setP((old) => ({ ...old, ...patch }));
  const typesQ = useQuery({
    queryKey: ["object-types"],
    queryFn: () => api.get<ObjectTypeDef[]>(`${API}/ontology/object-types`),
    staleTime: 60_000,
  });
  const types = typesQ.data ?? [];
  const complete = p.object_type
    ? Boolean(p.metrics?.length)
    : Boolean((p.sql ?? "").trim());

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal" style={{ width: 620, maxWidth: "92vw" }} onClick={(e) => e.stopPropagation()}>
        <div className="card-title">
          {initial.sql || initial.object_type ? "Edit panel" : "Add panel"}
        </div>
        <div className="toolbar" style={{ gap: 12, flexWrap: "wrap", marginTop: 10 }}>
          <div className="field" style={{ flex: "1 1 200px" }}>
            <label>Title</label>
            <input value={p.title} onChange={(e) => set({ title: e.target.value })} placeholder="Revenue by region" />
          </div>
          <div className="field">
            <label>Chart</label>
            <select value={p.chart} onChange={(e) => set({ chart: e.target.value as ChartKind })}>
              {CHART_KINDS.map((k) => (
                <option key={k} value={k}>{k}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Width (1–12)</label>
            <input
              type="number"
              min={1}
              max={12}
              value={p.width}
              onChange={(e) => set({ width: Math.max(1, Math.min(12, Number(e.target.value) || 6)) })}
              style={{ width: 80 }}
            />
          </div>
        </div>
        <div className="field">
          <label>Source</label>
          <div className="toolbar" style={{ gap: 8 }}>
            <button
              className={!p.object_type ? "primary small" : "small"}
              onClick={() => set({ object_type: "", metrics: [], group_by: [] })}
            >
              SQL
            </button>
            <button
              className={p.object_type ? "primary small" : "small"}
              onClick={() =>
                set({
                  sql: "",
                  object_type: types[0]?.api_name ?? "",
                  metrics: p.metrics?.length ? p.metrics : [{ op: "count", alias: "count" }],
                })
              }
              disabled={types.length === 0}
            >
              Objects
            </button>
          </div>
          <p className="hint" style={{ marginTop: 6 }}>
            {p.object_type
              ? "Aggregates objects, so the chart includes edits made by actions."
              : "Raw SQL over datasets. It won't reflect the ontology's edit overlay."}
          </p>
        </div>

        {!p.object_type ? (
          <div className="field">
            {/* The no-code path, offered beside the SQL wall — only for a NEW
                panel: Explore cannot reopen a raw-SQL panel, so pointing an
                edit there would dead-end. The link carries the dashboard name
                so Explore's save dialog is already aimed back here. */}
            {!(initial.sql || initial.object_type) && (
              <p className="hint" style={{ marginTop: 0 }}>
                Prefer clicking to writing SQL? Build this panel in{" "}
                <Link to={`/explore?dashboard=${encodeURIComponent(dashboard)}`}>
                  Explore
                </Link>{" "}
                — pick a dataset, shape it, and save it to this dashboard.
              </p>
            )}
            <label>SQL</label>
            <textarea
              className="mono"
              rows={5}
              // `?? ""` and not `|| ""`: the editor is only opened for a panel
              // that arrived whole (see `canEditPanel`), so this coalesce is
              // for a *new* panel, never for a withheld one. Prefilling a form
              // from a value the server declined to send would write the hole
              // back as if it were data.
              value={p.sql ?? ""}
              onChange={(e) => set({ sql: e.target.value })}
              placeholder="SELECT region, sum(amount) AS total FROM sales GROUP BY region"
              style={{ width: "100%", resize: "vertical" }}
            />
          </div>
        ) : (
          <ObjectSourceFields p={p} set={set} types={types} />
        )}
        <div className="toolbar" style={{ gap: 12, flexWrap: "wrap" }}>
          <div className="field">
            <label>X column (optional)</label>
            <input className="mono" value={p.x} onChange={(e) => set({ x: e.target.value })} placeholder="inferred" />
          </div>
          <div className="field" style={{ flex: "1 1 200px" }}>
            <label>Y columns (comma-separated, optional)</label>
            <input
              className="mono"
              value={p.y.join(", ")}
              onChange={(e) =>
                set({ y: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })
              }
              placeholder="all numeric columns"
            />
          </div>
          <div className="field">
            <label>Split by (optional)</label>
            <input
              className="mono"
              value={p.series ?? ""}
              onChange={(e) => set({ series: e.target.value })}
              placeholder="a category column"
              title="A result column whose values become the series"
              style={{ width: 140 }}
            />
          </div>
          {p.chart === "bar" && (
            <label className="check-inline" style={{ alignSelf: "flex-end", paddingBottom: 8 }}>
              <input
                type="checkbox"
                checked={p.stacked ?? false}
                onChange={(e) => set({ stacked: e.target.checked })}
              />
              <span>stacked</span>
            </label>
          )}
        </div>
        <div className="toolbar" style={{ marginTop: 12, justifyContent: "flex-end" }}>
          <button onClick={onCancel}>Cancel</button>
          <button className="primary" disabled={!complete} onClick={() => onSave(p)}>
            Save panel
          </button>
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ detail

function DashboardPage() {
  const { name = "" } = useParams();
  const auth = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [editing, setEditing] = useState<DashboardPanel | null>(null);
  const [warnings, setWarnings] = useState<AuthoringWarning[]>([]);

  const dashQ = useQuery({
    queryKey: ["dashboard", name],
    queryFn: () => api.get<Dashboard>(`${API}/dashboards/${name}`),
  });

  // After any write, drop the cached rows for this board as well as the board
  // itself — the panel's rows are keyed on its id, not on its SQL, so nothing
  // else would tell the cache that the query changed.
  const afterWrite = (d: Dashboard) => {
    setWarnings(d.warnings ?? []);
    qc.invalidateQueries({ queryKey: ["dashboard", name] });
    qc.invalidateQueries({ queryKey: ["dashboards"] });
    qc.invalidateQueries({ queryKey: ["panel-run", name] });
  };

  // One panel at a time. The whole-board PUT still exists for title and
  // description, but it is never used to carry panels from a fetched record:
  // a client that re-sends panels it was only shown is one demotion away from
  // blanking the queries it was not shown.
  const savePanel = useMutation({
    mutationFn: (p: DashboardPanel) => {
      const exists = (dashQ.data?.panels ?? []).some((q) => q.id === p.id);
      const base = `${API}/dashboards/${encodeURIComponent(name)}/panels`;
      return exists
        ? api.put<Dashboard>(`${base}/${encodeURIComponent(p.id)}`, p)
        : api.post<Dashboard>(base, p);
    },
    onSuccess: afterWrite,
  });

  const deletePanel = useMutation({
    mutationFn: (id: string) =>
      api.del<Dashboard>(
        `${API}/dashboards/${encodeURIComponent(name)}/panels/${encodeURIComponent(id)}`,
      ),
    onSuccess: afterWrite,
  });

  const del = useMutation({
    mutationFn: () => api.del(`${API}/dashboards/${name}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dashboards"] });
      navigate("/dashboards");
    },
  });

  if (dashQ.isLoading) return <Spinner />;
  if (dashQ.isError) return <ErrorBox error={dashQ.error} />;
  const dash = dashQ.data!;
  const canEdit = auth.can("editor");

  function upsertPanel(p: DashboardPanel) {
    savePanel.mutate(p);
    setEditing(null);
  }

  return (
    <div>
      <div style={{ marginBottom: 12 }}>
        <Link to="/dashboards">← Dashboards</Link>
      </div>
      <PageHeader
        title={dash.title || dash.name}
        subtitle={dash.description || undefined}
        actions={
          canEdit ? (
            <span style={{ display: "inline-flex", gap: 8 }}>
              <button
                className="small"
                onClick={() =>
                  setEditing({
                    id: newPanelId(),
                    title: "",
                    sql: "",
                    chart: "bar",
                    x: "",
                    y: [],
                    width: 6,
                  })
                }
              >
                Add panel
              </button>
              <button
                className="small danger"
                onClick={() => {
                  if (window.confirm(`Delete dashboard "${dash.name}"?`)) del.mutate();
                }}
              >
                Delete
              </button>
            </span>
          ) : undefined
        }
      />
      {/* Zero-step sharing, stated instead of implied: without this line a
          new user cannot tell whether the dashboard they just made is private
          or workspace-visible without asking someone. An indicator only — no
          sharing controls, no friction. */}
      <p className="hint" style={{ marginTop: -6, marginBottom: 14 }}>
        Visible to everyone in this workspace, computed with each viewer's own
        data access.
      </p>
      {savePanel.isError && <ErrorBox error={savePanel.error} />}
      {deletePanel.isError && <ErrorBox error={deletePanel.error} />}
      <WarningBox warnings={warnings} />

      {dash.panels.length === 0 ? (
        <EmptyState>
          No panels yet
          {canEdit ? (
            <>
              {" "}— add one, shape a chart in <Link to="/explore">Explore</Link>, or send a
              query here from the SQL page.
            </>
          ) : (
            "."
          )}
        </EmptyState>
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(12, 1fr)",
            gap: 14,
          }}
        >
          {dash.panels.map((p) => (
            <div
              key={p.id}
              className="card"
              style={{ gridColumn: `span ${Math.max(1, Math.min(12, p.width))}`, minWidth: 0 }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "baseline",
                  justifyContent: "space-between",
                  gap: 8,
                  marginBottom: 8,
                }}
              >
                {/* Always a label. The server fills "Panel {n}" at write time,
                    so this never falls back to a slice of the query — which is
                    what it used to do, and which is precisely the text a viewer
                    is no longer given. */}
                <div style={{ fontWeight: 600, fontSize: 13.5 }}>{p.title}</div>
                {canEdit && (
                  <span style={{ display: "inline-flex", gap: 6, flexShrink: 0 }}>
                    {panelIsWhole(p) ? (
                      // A *flow* panel has nodes; a SQL panel serializes
                      // `flow: {}` (the model's default), and `!== undefined`
                      // once sent SQL panels into Explore, which crashed the
                      // whole app trying to read `.nodes` of undefined.
                      Array.isArray((p.flow as any)?.nodes) && (p.flow as any).nodes.length > 0 ? (
                        // A flow panel was shaped in Explore, so it is edited
                        // there — one shaping UI, not two drifting copies.
                        <button
                          className="small"
                          title="Reopens this panel's shaping in Explore"
                          onClick={() =>
                            navigate(
                              `/explore?dashboard=${encodeURIComponent(dash.name)}&panel=${encodeURIComponent(p.id)}`,
                            )
                          }
                        >
                          Edit in Explore
                        </button>
                      ) : (
                        <button className="small" onClick={() => setEditing(p)}>
                          Edit
                        </button>
                      )
                    ) : (
                      // Can happen to an editor only in an odd state (a demoted
                      // session, a stale tab). Offering "Edit" would open a form
                      // with an empty SQL box over a panel that has one.
                      <Withheld
                        what="This panel's query"
                        role="editor"
                        label="not editable"
                        why="Reload the page; if it persists, your session's role changed."
                      />
                    )}
                    <button
                      className="small danger"
                      disabled={deletePanel.isPending}
                      onClick={() => deletePanel.mutate(p.id)}
                    >
                      ✕
                    </button>
                  </span>
                )}
              </div>
              <PanelBody dashboard={dash.name} panel={p} />
            </div>
          ))}
        </div>
      )}

      {editing && (
        <PanelEditor
          initial={editing}
          dashboard={dash.name}
          onSave={upsertPanel}
          onCancel={() => setEditing(null)}
        />
      )}
    </div>
  );
}
