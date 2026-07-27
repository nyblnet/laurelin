// Dashboards: grids of saved queries rendered as charts.
//
// Every panel executes through POST /query with the *viewer's* credentials,
// so RLS / ACLs / markings apply per user — a dashboard is presentation, not
// a data grant. Editors can create dashboards and add panels (also directly
// from the SQL workbench via "Add to dashboard").

import { useState } from "react";
import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../api";
import { useAuth } from "../auth";
import { Chart } from "../charts";
import type {
  AggregateResult,
  ChartKind,
  Dashboard,
  DashboardPanel,
  ObjectTypeDef,
  QueryResult,
} from "../types";
import {
  Column,
  DataTable,
  EmptyState,
  ErrorBox,
  PageHeader,
  Spinner,
  fmtTime,
} from "../ui";

const NAME_RE = /^[a-z][a-z0-9_-]{0,63}$/;
const CHART_KINDS: ChartKind[] = ["table", "bar", "line", "area", "stat"];

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
          {auth.can("editor")
            ? " — create one above, or save a query from the SQL workbench."
            : "."}
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
 * Both panel sources reduce to {columns, rows}, so the chart and table below
 * never learn which one they were fed. An object panel goes through
 * /aggregate rather than SQL over the backing dataset, which is what makes it
 * agree with the object list: aggregation sees the edit overlay, raw SQL
 * doesn't.
 */
async function fetchPanel(panel: DashboardPanel): Promise<QueryResult> {
  if (!panel.object_type) {
    return api.post<QueryResult>(`${API}/query`, { sql: panel.sql, max_rows: 1000 });
  }
  const agg = await api.post<AggregateResult>(
    `${API}/ontology/objects/${panel.object_type}/aggregate`,
    {
      group_by: panel.group_by ?? [],
      metrics: panel.metrics ?? [],
      filters: panel.filters ?? {},
      search: panel.search || null,
      limit: 1000,
    },
  );
  const columns = [
    ...(panel.group_by ?? []),
    ...(panel.metrics ?? []).map((m, i) => m.alias || m.op || `metric_${i}`),
  ];
  return { columns, rows: agg.groups, row_count: agg.groups.length, truncated: agg.truncated };
}

function PanelBody({ panel }: { panel: DashboardPanel }) {
  const q = useQuery({
    queryKey: ["panel", panel.id, panel.sql, panel.object_type,
               JSON.stringify(panel.metrics), JSON.stringify(panel.group_by),
               JSON.stringify(panel.filters), panel.search],
    queryFn: () => fetchPanel(panel),
    staleTime: 30_000,
  });

  if (q.isLoading) return <Spinner />;
  if (q.isError) return <ErrorBox error={q.error} />;
  const result = q.data!;

  if (panel.chart === "table" || result.columns.length === 0) {
    return (
      <div className="table-wrap" style={{ maxHeight: 280, overflowY: "auto" }}>
        <table>
          <thead>
            <tr>
              {result.columns.map((c) => (
                <th key={c} className="mono">{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.slice(0, 100).map((row, i) => (
              <tr key={i}>
                {result.columns.map((c) => (
                  <td key={c} className="mono">{String(row[c] ?? "")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  return <Chart data={result} kind={panel.chart} x={panel.x} y={panel.y} />;
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
  onSave,
  onCancel,
}: {
  initial: DashboardPanel;
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
    : Boolean(p.sql.trim());

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
            <label>SQL</label>
            <textarea
              className="mono"
              rows={5}
              value={p.sql}
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

  const dashQ = useQuery({
    queryKey: ["dashboard", name],
    queryFn: () => api.get<Dashboard>(`${API}/dashboards/${name}`),
  });

  const save = useMutation({
    mutationFn: (panels: DashboardPanel[]) => {
      const d = dashQ.data!;
      return api.put<Dashboard>(`${API}/dashboards/${name}`, {
        title: d.title,
        description: d.description,
        panels,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dashboard", name] });
      qc.invalidateQueries({ queryKey: ["dashboards"] });
    },
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
    const exists = dash.panels.some((q) => q.id === p.id);
    const panels = exists
      ? dash.panels.map((q) => (q.id === p.id ? p : q))
      : [...dash.panels, p];
    save.mutate(panels);
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
      {save.isError && <ErrorBox error={save.error} />}

      {dash.panels.length === 0 ? (
        <EmptyState>
          No panels yet{canEdit ? " — add one, or send a query here from the SQL workbench." : "."}
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
                <div style={{ fontWeight: 600, fontSize: 13.5 }}>
                  {p.title || <span className="faint mono">{p.sql.slice(0, 48)}</span>}
                </div>
                {canEdit && (
                  <span style={{ display: "inline-flex", gap: 6, flexShrink: 0 }}>
                    <button className="small" onClick={() => setEditing(p)}>
                      Edit
                    </button>
                    <button
                      className="small danger"
                      onClick={() => save.mutate(dash.panels.filter((q) => q.id !== p.id))}
                    >
                      ✕
                    </button>
                  </span>
                )}
              </div>
              <PanelBody panel={p} />
            </div>
          ))}
        </div>
      )}

      {editing && (
        <PanelEditor
          initial={editing}
          onSave={upsertPanel}
          onCancel={() => setEditing(null)}
        />
      )}
    </div>
  );
}
