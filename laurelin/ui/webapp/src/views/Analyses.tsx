// Analyses: the multi-cell governed notebook — Code Workbook parity, no code.
//
// An analyst adds cells top to bottom. Each cell is EITHER a SQL query (the
// workbench's textarea, inline) OR a point-and-click shaping step (Explore's
// card stack), and a shaping cell's SOURCE picker offers datasets *and*
// earlier shaping cells — that one picker is the entire chaining UX. Each
// cell shows its result table and, optionally, a chart.
//
// There is deliberately no code cell. That is Foundry's actual "Code"
// workbook and the RCE surface --lock-pipelines exists to close; everything
// here compiles to governed SQL through paths already built and attacked.
//
// Sharing is the permission model, as with dashboards: a viewer opening the
// same URL receives the layout (titles, chart bindings) and gets each cell's
// ROWS from POST /analyses/{name}/cells/{id}/run, executed server-side as
// THEMSELVES — their ACL, row policy and masks, over the whole chain, in one
// compiled statement. The query text is never in their payload; "no query
// text is renderable" is the server's projection, not CSS.
//
// Display order is cosmetic; *execution* order is the DAG in each cell's
// inputs. Reordering cells never edits inputs.

import { useEffect, useMemo, useState } from "react";
import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api, ApiError } from "../api";
import { useAuth } from "../auth";
import { Chart } from "../charts";
import type {
  Analysis,
  AnalysisCell,
  CellPreviewResult,
  ChartKind,
  Dataset,
  FlowKind,
  FlowSchemaResult,
  QueryResult,
} from "../types";
import { cellIsWhole } from "../types";
import {
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
import {
  DATE_BUCKETS,
  FILTER_OPS,
  MEASURE_FNS,
  NUMERIC_FNS,
  defaultAlias,
  type ExploreBucket,
  type ExploreFilter,
  type ExploreFilterOp,
} from "./explore/model";
import {
  cellFragment,
  cellResultColumns,
  emptyShaping,
  explainCellRefusal,
  shapingFromCell,
  shapingIssues,
  sourceCell,
  sourceDataset,
  type CellShaping,
  type RefusalContext,
} from "./analyses/model";

const NAME_RE = /^[a-z][a-z0-9_-]{0,63}$/;
const CHART_KINDS: ChartKind[] = ["table", "bar", "line", "area", "stat", "pie", "scatter"];
const PREVIEW_ROWS = 200;

export function AnalysesView() {
  return (
    <Routes>
      <Route path="/" element={<AnalysisList />} />
      <Route path=":name" element={<AnalysisPage />} />
    </Routes>
  );
}

// ------------------------------------------------------------------- list

function AnalysisList() {
  const auth = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [newName, setNewName] = useState("");
  const [newTitle, setNewTitle] = useState("");

  const listQ = useQuery({
    queryKey: ["analyses"],
    queryFn: () => api.get<Analysis[]>(`${API}/analyses`),
  });

  const create = useMutation({
    mutationFn: () =>
      api.put<Analysis>(`${API}/analyses/${newName.trim()}`, {
        title: newTitle.trim() || newName.trim(),
        cells: [],
      }),
    onSuccess: (a) => {
      qc.invalidateQueries({ queryKey: ["analyses"] });
      navigate(`/analyses/${a.name}`);
    },
  });

  const columns: Column<Analysis>[] = [
    {
      label: "Analysis",
      render: (a) => <Link to={`/analyses/${a.name}`}>{a.title || a.name}</Link>,
    },
    { label: "Name", className: "mono dim", render: (a) => a.name },
    { label: "Cells", className: "num", render: (a) => String(a.cells.length) },
    { label: "Updated", render: (a) => <span className="dim">{fmtTime(a.updated_at)}</span> },
  ];

  const nameOk = NAME_RE.test(newName.trim());

  return (
    <div>
      <PageHeader
        title="Analyses"
        subtitle="Multi-step notebooks over governed data — each cell queries or shapes, later cells build on earlier ones, and every result respects the reader's data access."
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
                placeholder="churn-investigation"
                autoComplete="off"
              />
            </div>
            <div className="field" style={{ flex: "1 1 220px" }}>
              <label>Title</label>
              <input
                value={newTitle}
                onChange={(e) => setNewTitle(e.target.value)}
                placeholder="Churn investigation"
                autoComplete="off"
              />
            </div>
            <button
              className="primary"
              disabled={!nameOk || create.isPending}
              onClick={() => create.mutate()}
            >
              {create.isPending ? "Creating…" : "New analysis"}
            </button>
          </div>
          {create.isError && <ErrorBox error={create.error} />}
        </div>
      )}
      {listQ.isLoading ? (
        <Spinner />
      ) : listQ.isError ? (
        <ErrorBox error={listQ.error} />
      ) : listQ.data!.length === 0 ? (
        <EmptyState>
          No analyses yet{auth.can("editor") ? " — create one above." : "."}
        </EmptyState>
      ) : (
        <DataTable
          columns={columns}
          rows={listQ.data!}
          rowKey={(a) => a.name}
          onRowClick={(a) => navigate(`/analyses/${a.name}`)}
        />
      )}
    </div>
  );
}

// ------------------------------------------------------------ result table

function ResultTable({ result, maxHeight = 300 }: { result: QueryResult; maxHeight?: number }) {
  return (
    <div>
      <div className="table-wrap" style={{ maxHeight, overflowY: "auto" }}>
        <table>
          <thead>
            <tr>
              {result.columns.map((c) => (
                <th key={c} className="mono">{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.map((row, i) => (
              <tr key={i}>
                {result.columns.map((c) => (
                  <td key={c} className="mono">{fmtValue(row[c])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="faint" style={{ fontSize: 11, marginTop: 6, display: "flex", gap: 8 }}>
        {result.row_count.toLocaleString("en-US")} row{result.row_count === 1 ? "" : "s"}
        {result.truncated && (
          <span className="badge badge-gold">first {result.row_count.toLocaleString("en-US")} of a larger result</span>
        )}
      </div>
    </div>
  );
}

function CellResult({
  result,
  chart,
  x,
  y,
  series,
  stacked,
}: {
  result: QueryResult;
  chart: ChartKind;
  x: string;
  y: string[];
  series: string;
  stacked: boolean;
}) {
  if (chart === "table" || result.columns.length === 0) {
    return <ResultTable result={result} />;
  }
  return (
    <div>
      <Chart data={result} kind={chart} x={x} y={y} series={series} stacked={stacked} />
      <details style={{ marginTop: 8 }}>
        <summary className="faint" style={{ fontSize: 11.5, cursor: "pointer" }}>
          {result.row_count.toLocaleString("en-US")} row{result.row_count === 1 ? "" : "s"} — show table
        </summary>
        <ResultTable result={result} />
      </details>
    </div>
  );
}

// ------------------------------------------------------------- viewer cell

/** A viewer's cell: title + rows/chart from the run route. The payload this
 *  screen holds contains no query text at all — there is nothing to hide,
 *  because nothing arrived. */
function ViewerCell({ analysis, cell }: { analysis: string; cell: AnalysisCell }) {
  const q = useQuery({
    // Keyed on identity, not contents — a viewer has no contents to key on.
    queryKey: ["cell-run", analysis, cell.id],
    queryFn: () =>
      api.post<QueryResult>(
        `${API}/analyses/${encodeURIComponent(analysis)}/cells/${encodeURIComponent(cell.id)}/run`,
        { max_rows: 1000 },
      ),
    staleTime: 30_000,
  });

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 8 }}>
        <div style={{ fontWeight: 600, fontSize: 13.5 }}>{cell.title}</div>
        <button className="small" disabled={q.isFetching} onClick={() => q.refetch()}>
          {q.isFetching ? "Running…" : "Run again"}
        </button>
      </div>
      {q.isLoading ? (
        <Spinner label="Running…" />
      ) : q.isError ? (
        <ErrorBox error={q.error} />
      ) : (
        <CellResult
          result={q.data!}
          chart={cell.chart}
          x={cell.x}
          y={cell.y}
          series={cell.series ?? ""}
          stacked={cell.stacked ?? false}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------- bindings

interface Bindings {
  chart: ChartKind;
  x: string;
  y: string[];
  series: string;
  stacked: boolean;
}

const AUTO_BINDINGS: Bindings = { chart: "table", x: "", y: [], series: "", stacked: false };

function BindingsRow({
  bindings,
  set,
  columns,
  kinds,
}: {
  bindings: Bindings;
  set: (b: Bindings) => void;
  columns: string[];
  kinds: Record<string, FlowKind>;
}) {
  const numeric = columns.filter((c) => kinds[c] === "number");
  return (
    <div>
      <div className="toolbar" style={{ gap: 6, marginBottom: 6, flexWrap: "wrap" }}>
        {CHART_KINDS.map((k) => (
          <button
            key={k}
            className={`small${bindings.chart === k ? " primary" : ""}`}
            onClick={() => set({ ...bindings, chart: k })}
          >
            {k}
          </button>
        ))}
      </div>
      {bindings.chart !== "table" && bindings.chart !== "stat" && (
        <div className="toolbar" style={{ gap: 10, flexWrap: "wrap" }}>
          <div className="field">
            <label>X axis</label>
            <select value={bindings.x} onChange={(e) => set({ ...bindings, x: e.target.value })}>
              <option value="">(infer)</option>
              {columns.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Y (values)</label>
            <select
              multiple
              size={Math.min(3, Math.max(2, numeric.length))}
              value={bindings.y}
              onChange={(e) =>
                set({ ...bindings, y: Array.from(e.target.selectedOptions, (o) => o.value) })
              }
            >
              {(numeric.length > 0 ? numeric : columns).map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Split by</label>
            <select
              value={bindings.series}
              onChange={(e) => set({ ...bindings, series: e.target.value })}
            >
              <option value="">—</option>
              {columns.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>
          {bindings.chart === "bar" && (
            <label className="check-inline" style={{ alignSelf: "flex-end", paddingBottom: 8 }}>
              <input
                type="checkbox"
                checked={bindings.stacked}
                onChange={(e) => set({ ...bindings, stacked: e.target.checked })}
              />
              <span>stacked</span>
            </label>
          )}
        </div>
      )}
    </div>
  );
}

// -------------------------------------------------------------- draft model

interface DraftCell {
  /** Stable local key (never reused; survives id assignment on save). */
  key: string;
  /** Server id once saved; null for a brand-new cell. */
  id: string | null;
  title: string;
  kind: "sql" | "shaping";
  sql: string;
  /** Parsed shaping state; null when the saved fragment is not a shape these
   *  cards emit (e.g. hand-written IR with a join) — shown, not editable. */
  shaping: CellShaping | null;
  /** The saved record, kept whole for reorder PUTs and unparseable cells. */
  raw: AnalysisCell | null;
  bindings: Bindings;
  width: number;
  dirty: boolean;
}

let keyCounter = 0;
function nextKey(): string {
  return `k${++keyCounter}`;
}

function draftFromCell(c: AnalysisCell): DraftCell {
  const isSql = (c.sql ?? "") !== "";
  return {
    key: nextKey(),
    id: c.id,
    title: c.title,
    kind: isSql ? "sql" : "shaping",
    sql: c.sql ?? "",
    shaping: isSql ? null : shapingFromCell(c.flow, c.inputs, c.top),
    raw: c,
    bindings: {
      chart: c.chart,
      x: c.x,
      y: c.y,
      series: c.series ?? "",
      stacked: c.stacked ?? false,
    },
    width: c.width,
    dirty: false,
  };
}

/** The wire dict for one draft cell — presentation + whichever source it is. */
function cellPayload(d: DraftCell, kinds: Record<string, FlowKind>): Record<string, unknown> | null {
  const base = {
    title: d.title,
    chart: d.bindings.chart,
    x: d.bindings.x,
    y: d.bindings.y,
    series: d.bindings.series,
    stacked: d.bindings.stacked,
    width: d.width,
  };
  if (d.kind === "sql") {
    if (!d.sql.trim()) return null;
    return { ...base, sql: d.sql };
  }
  if (d.shaping) {
    const frag = cellFragment(d.shaping, kinds);
    if (!frag) return null;
    return { ...base, flow: frag.flow, inputs: frag.inputs, top: frag.top };
  }
  // Unparseable saved shaping cell: carry the stored instruction unchanged.
  if (d.raw && cellIsWhole(d.raw)) {
    return { ...base, flow: d.raw.flow, inputs: d.raw.inputs ?? [], top: d.raw.top ?? null };
  }
  return null;
}

// ------------------------------------------------------------ shaping cards

function ShapingCards({
  state,
  set,
  columns,
  kinds,
  sources,
}: {
  state: CellShaping;
  set: (fn: (s: CellShaping) => CellShaping) => void;
  /** Columns + kinds of the picked source (dataset schema or upstream cell's
   *  preview schema). */
  columns: string[];
  kinds: Record<string, FlowKind>;
  /** What the source picker offers. */
  sources: { value: string; label: string; hint?: string }[];
}) {
  const aggregating = state.measures.length > 0;
  const resultCols = cellResultColumns(state, columns);

  return (
    <div>
      <div className="toolbar" style={{ gap: 10, flexWrap: "wrap" }}>
        <div className="field">
          <label>Reads from</label>
          <select
            value={state.source}
            onChange={(e) => {
              const source = e.target.value;
              // Columns belong to a source; shaping does not survive a swap.
              set(() => emptyShaping(source));
            }}
          >
            <option value="">Pick a source…</option>
            {sources.map((s) => (
              <option key={s.value} value={s.value} title={s.hint}>{s.label}</option>
            ))}
          </select>
        </div>
      </div>

      {state.source && (
        <>
          {/* -------------------------------------------------------- filter */}
          <div className="an-card">
            <div className="an-card-title">Filter</div>
            {state.filters.map((f, i) => {
              const kind = kinds[f.column] ?? "";
              const needsValue = f.op !== "is_null" && f.op !== "is_not_null";
              const isList = f.op === "in" || f.op === "not_in";
              return (
                <div key={i} className="an-row">
                  <select
                    value={f.column}
                    onChange={(e) =>
                      set((s) => ({
                        ...s,
                        filters: s.filters.map((x, j) => (j === i ? { ...x, column: e.target.value } : x)),
                      }))
                    }
                  >
                    <option value="">Pick a column…</option>
                    {columns.map((c) => (
                      <option key={c} value={c}>{c}</option>
                    ))}
                  </select>
                  <select
                    value={f.op}
                    onChange={(e) =>
                      set((s) => ({
                        ...s,
                        filters: s.filters.map((x, j) =>
                          j === i ? { ...x, op: e.target.value as ExploreFilterOp } : x,
                        ),
                      }))
                    }
                  >
                    {(Object.keys(FILTER_OPS) as ExploreFilterOp[]).map((op) => (
                      <option key={op} value={op}>{FILTER_OPS[op]}</option>
                    ))}
                  </select>
                  {needsValue && !isList && (
                    <input
                      value={f.value}
                      placeholder={
                        kind === "time" ? "YYYY-MM-DD" : kind === "number" ? "e.g. 100" : kind === "boolean" ? "true / false" : "value"
                      }
                      onChange={(e) =>
                        set((s) => ({
                          ...s,
                          filters: s.filters.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)),
                        }))
                      }
                    />
                  )}
                  {isList && (
                    <input
                      value={f.values.join(", ")}
                      placeholder="value, value, value"
                      onChange={(e) =>
                        set((s) => ({
                          ...s,
                          filters: s.filters.map((x, j) =>
                            j === i
                              ? { ...x, values: e.target.value.split(",").map((v) => v.trim()).filter(Boolean) }
                              : x,
                          ),
                        }))
                      }
                    />
                  )}
                  <button
                    className="an-x"
                    title="Remove this filter"
                    onClick={() => set((s) => ({ ...s, filters: s.filters.filter((_, j) => j !== i) }))}
                  >
                    ×
                  </button>
                </div>
              );
            })}
            <button
              className="an-add"
              onClick={() =>
                set((s) => ({
                  ...s,
                  filters: [...s.filters, { column: "", op: "eq", value: "", values: [] } as ExploreFilter],
                }))
              }
            >
              + keep only rows where…
            </button>
          </div>

          {/* ----------------------------------------------------- summarise */}
          <div className="an-card">
            <div className="an-card-title">Summarise</div>
            {!aggregating && (
              <div className="hint" style={{ marginBottom: 6 }}>
                No summaries — this cell returns the rows themselves. Add one to aggregate.
              </div>
            )}
            {state.measures.map((m, i) => (
              <div key={i} className="an-row">
                <select
                  value={m.fn}
                  onChange={(e) => {
                    const fn = e.target.value as typeof m.fn;
                    set((s) => ({
                      ...s,
                      measures: s.measures.map((x, j) =>
                        j === i
                          ? {
                              ...x, fn,
                              column: fn === "count_star" ? "" : x.column,
                              alias:
                                x.alias === defaultAlias(x.fn, x.column) || !x.alias
                                  ? defaultAlias(fn, fn === "count_star" ? "" : x.column)
                                  : x.alias,
                            }
                          : x,
                      ),
                    }));
                  }}
                >
                  {Object.entries(MEASURE_FNS).map(([fn, label]) => (
                    <option key={fn} value={fn}>{label}</option>
                  ))}
                </select>
                {m.fn !== "count_star" && (
                  <select
                    value={m.column}
                    onChange={(e) => {
                      const column = e.target.value;
                      set((s) => ({
                        ...s,
                        measures: s.measures.map((x, j) =>
                          j === i
                            ? {
                                ...x, column,
                                alias:
                                  x.alias === defaultAlias(x.fn, x.column) || !x.alias
                                    ? defaultAlias(x.fn, column)
                                    : x.alias,
                              }
                            : x,
                        ),
                      }));
                    }}
                  >
                    <option value="">Pick a column…</option>
                    {(NUMERIC_FNS.has(m.fn)
                      ? columns.filter((c) => !kinds[c] || kinds[c] === "number")
                      : columns
                    ).map((c) => (
                      <option key={c} value={c}>{c}</option>
                    ))}
                  </select>
                )}
                <input
                  value={m.alias}
                  placeholder="name in the result"
                  onChange={(e) =>
                    set((s) => ({
                      ...s,
                      measures: s.measures.map((x, j) => (j === i ? { ...x, alias: e.target.value } : x)),
                    }))
                  }
                />
                <button
                  className="an-x"
                  title="Remove this summary"
                  onClick={() => set((s) => ({ ...s, measures: s.measures.filter((_, j) => j !== i) }))}
                >
                  ×
                </button>
              </div>
            ))}
            <button
              className="an-add"
              onClick={() =>
                set((s) => ({
                  ...s,
                  measures: [...s.measures, { fn: "count_star", column: "", alias: defaultAlias("count_star", "") }],
                }))
              }
            >
              + add a summary
            </button>
          </div>

          {/* ------------------------------------------------------ group by */}
          {aggregating && (
            <div className="an-card">
              <div className="an-card-title">Group by</div>
              {state.groups.map((g, i) => {
                const kind = kinds[g.column] ?? "";
                return (
                  <div key={i} className="an-row">
                    <select
                      value={g.column}
                      onChange={(e) => {
                        const column = e.target.value;
                        set((s) => ({
                          ...s,
                          groups: s.groups.map((x, j) =>
                            j === i ? { column, bucket: "" as ExploreBucket, binWidth: "", parse: false } : x,
                          ),
                          sort: column && !s.sort ? { column, dir: "asc" } : s.sort,
                        }));
                      }}
                    >
                      <option value="">Pick a column…</option>
                      {columns.map((c) => (
                        <option key={c} value={c}>{c}</option>
                      ))}
                    </select>
                    {(kind === "time" || kind === "text") && (
                      <select
                        value={g.bucket === "bin" ? "" : g.bucket}
                        onChange={(e) => {
                          const bucket = e.target.value as ExploreBucket;
                          const parse = kind === "text" && !!bucket;
                          set((s) => ({
                            ...s,
                            groups: s.groups.map((x, j) => (j === i ? { ...x, bucket, parse } : x)),
                          }));
                        }}
                      >
                        <option value="">exact values</option>
                        {Object.entries(DATE_BUCKETS).map(([k, v]) => (
                          <option key={k} value={k}>
                            {kind === "text" ? `read as dates, by ${v}` : `by ${v}`}
                          </option>
                        ))}
                      </select>
                    )}
                    {kind === "number" && (
                      <>
                        <select
                          value={g.bucket === "bin" ? "bin" : ""}
                          onChange={(e) => {
                            const bin = e.target.value === "bin";
                            set((s) => ({
                              ...s,
                              groups: s.groups.map((x, j) =>
                                j === i
                                  ? { ...x, bucket: (bin ? "bin" : "") as ExploreBucket, binWidth: bin ? x.binWidth || "10" : "", parse: false }
                                  : x,
                              ),
                            }));
                          }}
                        >
                          <option value="">exact values</option>
                          <option value="bin">in ranges of…</option>
                        </select>
                        {g.bucket === "bin" && (
                          <input
                            style={{ width: 80 }}
                            value={g.binWidth}
                            onChange={(e) =>
                              set((s) => ({
                                ...s,
                                groups: s.groups.map((x, j) => (j === i ? { ...x, binWidth: e.target.value } : x)),
                              }))
                            }
                          />
                        )}
                      </>
                    )}
                    <button
                      className="an-x"
                      title="Remove this grouping"
                      onClick={() => set((s) => ({ ...s, groups: s.groups.filter((_, j) => j !== i) }))}
                    >
                      ×
                    </button>
                  </div>
                );
              })}
              <button
                className="an-add"
                onClick={() =>
                  set((s) => ({
                    ...s,
                    groups: [...s.groups, { column: "", bucket: "" as ExploreBucket, binWidth: "", parse: false }],
                  }))
                }
              >
                + group by…
              </button>
            </div>
          )}

          {/* --------------------------------------------------- order + top */}
          <div className="an-card">
            <div className="an-card-title">Order &amp; Top N</div>
            <div className="an-row">
              <select
                value={state.sort?.column ?? ""}
                onChange={(e) => {
                  const column = e.target.value;
                  set((s) => ({ ...s, sort: column ? { column, dir: s.sort?.dir ?? "asc" } : null }));
                }}
              >
                <option value="">unordered</option>
                {resultCols.map((c) => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
              {state.sort && (
                <select
                  value={state.sort.dir}
                  onChange={(e) =>
                    set((s) => ({ ...s, sort: s.sort ? { ...s.sort, dir: e.target.value as "asc" | "desc" } : null }))
                  }
                >
                  <option value="asc">smallest first</option>
                  <option value="desc">largest first</option>
                </select>
              )}
              <input
                style={{ width: 110 }}
                value={state.top}
                placeholder="top N (all)"
                onChange={(e) => set((s) => ({ ...s, top: e.target.value }))}
              />
            </div>
          </div>
        </>
      )}
    </div>
  );
}

// -------------------------------------------------------------- editor cell

function EditorCell({
  analysis,
  draft,
  position,
  drafts,
  update,
  onSave,
  onDelete,
  onMove,
  reportMeta,
  cellMeta,
  saving,
}: {
  analysis: string;
  draft: DraftCell;
  position: number;
  drafts: DraftCell[];
  update: (fn: (d: DraftCell) => DraftCell) => void;
  onSave: () => void;
  onDelete: () => void;
  onMove: (dir: -1 | 1) => void;
  /** Report this cell's compiled result schema, for downstream pickers. */
  reportMeta: (cellId: string, meta: { schema: string[]; kinds: Record<string, FlowKind> }) => void;
  cellMeta: Record<string, { schema: string[]; kinds: Record<string, FlowKind> }>;
  saving: boolean;
}) {
  // ------------------------------------------------- source columns + kinds
  const ds = draft.shaping ? sourceDataset(draft.shaping.source) : null;
  const upCell = draft.shaping ? sourceCell(draft.shaping.source) : null;

  const schemaQ = useQuery({
    queryKey: ["flow-schema", ds],
    queryFn: () => api.get<FlowSchemaResult>(`${API}/flows/schema?dataset=${encodeURIComponent(ds!)}`),
    enabled: draft.kind === "shaping" && !!ds,
    staleTime: 30_000,
  });
  const upMeta = upCell ? cellMeta[upCell] : undefined;
  const sourceColumns = ds ? schemaQ.data?.columns ?? [] : upMeta?.schema ?? [];
  const sourceKinds: Record<string, FlowKind> = ds ? schemaQ.data?.kinds ?? {} : upMeta?.kinds ?? {};

  // --------------------------------------------------------------- preview
  // The draft closure this cell's preview runs: its ancestors (as their
  // CURRENT drafts, so the preview reflects the screen) plus itself. Only
  // the ancestors need to be serializable — unrelated half-finished cells
  // must not block this cell's preview.
  const qc = useQueryClient();
  const previewBody = useMemo(() => {
    if (draft.kind === "sql") return null;
    const byId = new Map(drafts.filter((d) => d.id).map((d) => [d.id!, d]));
    // Literal-typing kinds for a cell's synthesis: its dataset's schema (from
    // the query cache its own card already populated) or its upstream cell's
    // compiled kinds.
    const kindsFor = (d: DraftCell): Record<string, FlowKind> => {
      if (!d.shaping) return {};
      const dds = sourceDataset(d.shaping.source);
      if (dds) {
        return qc.getQueryData<FlowSchemaResult>(["flow-schema", dds])?.kinds ?? {};
      }
      const up = sourceCell(d.shaping.source);
      return (up && cellMeta[up]?.kinds) || {};
    };
    const chain: Record<string, unknown>[] = [];
    const visit = (id: string): boolean => {
      const d = byId.get(id);
      if (!d) return false;
      if (chain.some((c) => c.id === id)) return true;
      const up = d.shaping ? sourceCell(d.shaping.source) : null;
      if (up && !visit(up)) return false;
      // An unedited saved ancestor rides as stored; an edited one reflects
      // the screen, so what previews is what the analyst is looking at.
      const payload =
        !d.dirty && d.raw && cellIsWhole(d.raw)
          ? { flow: d.raw.flow, inputs: d.raw.inputs ?? [], top: d.raw.top ?? null }
          : cellPayload(d, kindsFor(d));
      if (!payload || (payload as any).sql !== undefined || !(payload as any).flow) return false;
      chain.push({ id: d.id, title: d.title || "cell", ...payload });
      return true;
    };
    if (upCell && !visit(upCell)) return null;
    const selfPayload = cellPayload(draft, sourceKinds);
    if (!selfPayload || (selfPayload as any).sql !== undefined) return null;
    const selfId = draft.id ?? "draft";
    return { cells: [...chain, { id: selfId, ...selfPayload }], cell_id: selfId, max_rows: PREVIEW_ROWS };
  }, [draft, drafts, cellMeta, sourceKinds, upCell, qc]);

  const previewKey = previewBody ? JSON.stringify(previewBody) : "";
  const [debouncedKey, setDebouncedKey] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebouncedKey(previewKey), 400);
    return () => clearTimeout(t);
  }, [previewKey]);

  const previewQ = useQuery({
    queryKey: ["cell-preview", analysis, draft.key, debouncedKey],
    queryFn: ({ signal }) =>
      api.post<CellPreviewResult>(`${API}/analyses/preview`, JSON.parse(debouncedKey), signal),
    enabled: draft.kind === "shaping" && !!debouncedKey,
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  });

  // Downstream cells build their pickers from this cell's compiled schema.
  useEffect(() => {
    if (draft.id && previewQ.data) {
      reportMeta(draft.id, { schema: previewQ.data.schema, kinds: previewQ.data.kinds });
    }
  }, [draft.id, previewQ.data, reportMeta]);

  // ---------------------------------------------------------- SQL run (200)
  const sqlRun = useMutation({
    mutationFn: () => api.post<QueryResult>(`${API}/query`, { sql: draft.sql, max_rows: PREVIEW_ROWS }),
  });

  const result: QueryResult | undefined = draft.kind === "sql" ? sqlRun.data : previewQ.data;
  const resultKinds: Record<string, FlowKind> = useMemo(() => {
    if (draft.kind === "shaping") return previewQ.data?.kinds ?? {};
    const out: Record<string, FlowKind> = {};
    const sample = sqlRun.data?.rows[0];
    for (const c of sqlRun.data?.columns ?? []) {
      const v = sample?.[c];
      out[c] = typeof v === "number" ? "number" : typeof v === "boolean" ? "boolean" : "text";
    }
    return out;
  }, [draft.kind, previewQ.data, sqlRun.data]);

  const issues = draft.shaping ? shapingIssues(draft.shaping, sourceKinds) : [];

  const refusalCtx: RefusalContext = useMemo(() => {
    const position: Record<string, number> = {};
    const fragments: Record<string, { id: string; kind: string }[]> = {};
    drafts.forEach((d, i) => {
      if (!d.id) return;
      position[d.id] = i + 1;
      const frag = d.shaping ? cellFragment(d.shaping, {})?.flow : d.raw?.flow;
      if (frag?.nodes) {
        fragments[d.id] = (frag.nodes as any[]).map((n) => ({ id: n.id, kind: n.kind }));
      }
    });
    return { position, fragments };
  }, [drafts]);

  const canSave =
    draft.kind === "sql"
      ? draft.sql.trim() !== ""
      : draft.shaping
        ? issues.length === 0
        : draft.raw !== null; // unparseable but whole: presentation-only saves

  // What the source picker offers: datasets + earlier SAVED shaping cells.
  const datasetsQ = useQuery({
    queryKey: ["datasets"],
    queryFn: () => api.get<Dataset[]>(`${API}/datasets`),
    enabled: draft.kind === "shaping",
  });
  const sources = useMemo(() => {
    const out: { value: string; label: string; hint?: string }[] = [];
    for (let i = 0; i < drafts.length; i++) {
      const d = drafts[i];
      if (d === draft) break; // only EARLIER cells: execution reads upward
      if (d.kind !== "shaping" || !d.id) continue;
      out.push({ value: `cell:${d.id}`, label: `Cell ${i + 1} — ${d.title || d.id}` });
    }
    for (const d of datasetsQ.data ?? []) {
      out.push({ value: `ds:${d.name}`, label: d.name });
    }
    return out;
  }, [drafts, draft, datasetsQ.data]);

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
        <span className="badge">{position}</span>
        <input
          value={draft.title}
          placeholder={`Cell ${position}`}
          onChange={(e) => update((d) => ({ ...d, title: e.target.value, dirty: true }))}
          style={{ flex: "1 1 200px", fontWeight: 600 }}
        />
        <span className={`badge ${draft.kind === "sql" ? "badge-gold" : ""}`}
              title={draft.kind === "sql"
                ? "A SQL cell can't be referenced by later cells — only shaping cells chain."
                : "A shaping cell — later cells can read its output."}>
          {draft.kind === "sql" ? "SQL — can't be referenced by later cells" : "shaping"}
        </span>
        <span style={{ flexShrink: 0, display: "inline-flex", gap: 6 }}>
          <button className="small" title="Move up (display order only — execution follows the cell links)" onClick={() => onMove(-1)} disabled={position === 1}>↑</button>
          <button className="small" title="Move down (display order only)" onClick={() => onMove(1)}>↓</button>
          <button className="small primary" disabled={!canSave || saving} onClick={onSave}>
            {saving ? "Saving…" : draft.id ? (draft.dirty ? "Save" : "Saved") : "Save cell"}
          </button>
          <button className="small danger" onClick={onDelete}>✕</button>
        </span>
      </div>

      {draft.kind === "sql" ? (
        <div>
          <textarea
            className="mono"
            rows={4}
            value={draft.sql}
            onChange={(e) => update((d) => ({ ...d, sql: e.target.value, dirty: true }))}
            placeholder="SELECT region, sum(amount) AS total FROM sales GROUP BY region"
            style={{ width: "100%", resize: "vertical" }}
          />
          <div className="toolbar" style={{ marginTop: 6 }}>
            <button
              className="small"
              disabled={!draft.sql.trim() || sqlRun.isPending}
              onClick={() => sqlRun.mutate()}
            >
              {sqlRun.isPending ? "Running…" : "Run"}
            </button>
          </div>
          {sqlRun.isError && <ErrorBox error={sqlRun.error} />}
        </div>
      ) : draft.shaping ? (
        <div>
          <ShapingCards
            state={draft.shaping}
            set={(fn) => update((d) => ({ ...d, shaping: fn(d.shaping!), dirty: true }))}
            columns={sourceColumns}
            kinds={sourceKinds}
            sources={sources}
          />
          {upCell && !upMeta && (
            <div className="hint">Waiting for Cell above to preview — its columns feed these pickers.</div>
          )}
          {issues.length > 0 && draft.shaping.source && (
            <div className="an-note">{issues[0]}</div>
          )}
          {previewQ.isError && (
            <div className="an-note an-note-bad">
              {previewQ.error instanceof ApiError
                ? explainCellRefusal(previewQ.error.detail, refusalCtx)
                : String(previewQ.error)}
            </div>
          )}
        </div>
      ) : (
        <div className="an-note">
          This cell's shaping has a form these cards can't edit (it was likely
          written through the API). Its title, chart and layout can still be
          changed; the shaping itself is preserved as saved.
        </div>
      )}

      {result && (
        <div style={{ marginTop: 10 }}>
          <BindingsRow
            bindings={draft.bindings}
            set={(b) => update((d) => ({ ...d, bindings: b, dirty: true }))}
            columns={result.columns}
            kinds={resultKinds}
          />
          <div style={{ marginTop: 8 }}>
            <CellResult
              result={result}
              chart={draft.bindings.chart}
              x={draft.bindings.x}
              y={draft.bindings.y}
              series={draft.bindings.series}
              stacked={draft.bindings.stacked}
            />
          </div>
          <div className="faint" style={{ fontSize: 11.5, marginTop: 6 }}>
            preview · computed with your data access
            {draft.kind === "sql" && " · viewers will see up to 1000 rows"}
          </div>
        </div>
      )}
      {draft.kind === "shaping" && previewQ.isFetching && <Spinner label="Running…" />}
    </div>
  );
}

// ------------------------------------------------------------------ detail

function AnalysisPage() {
  const { name = "" } = useParams();
  const auth = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();

  const anaQ = useQuery({
    queryKey: ["analysis", name],
    queryFn: () => api.get<Analysis>(`${API}/analyses/${name}`),
  });

  if (anaQ.isLoading) return <Spinner />;
  if (anaQ.isError) return <ErrorBox error={anaQ.error} />;
  const ana = anaQ.data!;

  if (!auth.can("editor")) {
    return (
      <div>
        <div style={{ marginBottom: 12 }}>
          <Link to="/analyses">← Analyses</Link>
        </div>
        <PageHeader title={ana.title || ana.name} subtitle={ana.description || undefined} />
        {ana.cells.length === 0 ? (
          <EmptyState>This analysis has no cells yet.</EmptyState>
        ) : (
          ana.cells.map((c) => <ViewerCell key={c.id} analysis={ana.name} cell={c} />)
        )}
        <p className="hint">
          Every result above was computed with your data access — the analysis's
          instructions run on the server, as you.
        </p>
      </div>
    );
  }

  return (
    <AnalysisEditor
      key={ana.name}
      ana={ana}
      onDeleted={() => {
        qc.invalidateQueries({ queryKey: ["analyses"] });
        navigate("/analyses");
      }}
    />
  );
}

function AnalysisEditor({ ana, onDeleted }: { ana: Analysis; onDeleted: () => void }) {
  const qc = useQueryClient();
  const [drafts, setDrafts] = useState<DraftCell[]>(() => ana.cells.map(draftFromCell));
  const [cellMeta, setCellMeta] = useState<Record<string, { schema: string[]; kinds: Record<string, FlowKind> }>>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const reportMeta = useMemo(
    () => (cellId: string, meta: { schema: string[]; kinds: Record<string, FlowKind> }) =>
      setCellMeta((m) => {
        const prev = m[cellId];
        if (prev && JSON.stringify(prev) === JSON.stringify(meta)) return m;
        return { ...m, [cellId]: meta };
      }),
    [],
  );

  const afterWrite = (a: Analysis) => {
    qc.setQueryData(["analysis", ana.name], a);
    qc.invalidateQueries({ queryKey: ["analyses"] });
    // The instructions changed; every viewer-style cached run is stale.
    qc.invalidateQueries({ queryKey: ["cell-run", ana.name] });
  };

  // Reconcile drafts with the server's record after a save: adopt ids and
  // server-filled titles for the saved cell, keep other drafts' edits.
  const reconcile = (a: Analysis, savedKey: string, savedId: string | null) => {
    setDrafts((ds) =>
      ds.map((d) => {
        if (d.key !== savedKey) return d;
        const server = savedId
          ? a.cells.find((c) => c.id === savedId)
          : a.cells[a.cells.length - 1];
        if (!server) return d;
        return { ...d, id: server.id, title: server.title, raw: server, dirty: false };
      }),
    );
  };

  const saveCell = async (d: DraftCell) => {
    setError(null);
    setSavingKey(d.key);
    try {
      // Literal kinds for synthesis: dataset schema or upstream cell schema.
      const kinds: Record<string, FlowKind> = await (async () => {
        if (!d.shaping) return {};
        const ds = sourceDataset(d.shaping.source);
        if (ds) {
          const s = await api.get<FlowSchemaResult>(`${API}/flows/schema?dataset=${encodeURIComponent(ds)}`);
          return s.kinds;
        }
        const up = sourceCell(d.shaping.source);
        return (up && cellMeta[up]?.kinds) || {};
      })();
      const payload = cellPayload(d, kinds);
      if (!payload) throw new Error("This cell isn't finished yet.");
      const base = `${API}/analyses/${encodeURIComponent(ana.name)}/cells`;
      const result = d.id
        ? await api.put<Analysis>(`${base}/${encodeURIComponent(d.id)}`, payload)
        : await api.post<Analysis>(base, payload);
      afterWrite(result);
      reconcile(result, d.key, d.id);
    } catch (e) {
      setError(translated(e));
    } finally {
      setSavingKey(null);
    }
  };

  const deleteCell = async (d: DraftCell) => {
    setError(null);
    if (!d.id) {
      setDrafts((ds) => ds.filter((x) => x.key !== d.key));
      return;
    }
    try {
      const result = await api.del<Analysis>(
        `${API}/analyses/${encodeURIComponent(ana.name)}/cells/${encodeURIComponent(d.id)}`,
      );
      afterWrite(result);
      setDrafts((ds) => ds.filter((x) => x.key !== d.key));
    } catch (e) {
      setError(translated(e));
    }
  };

  // Reorder: display order only — a whole-record PUT carrying every saved
  // cell WHOLE (the editor holds the full records), never a projection.
  const moveCell = async (d: DraftCell, dir: -1 | 1) => {
    setError(null);
    const i = drafts.findIndex((x) => x.key === d.key);
    const j = i + dir;
    if (j < 0 || j >= drafts.length) return;
    const next = [...drafts];
    [next[i], next[j]] = [next[j], next[i]];
    setDrafts(next);
    if (next.every((x) => x.id && x.raw)) {
      try {
        const result = await api.put<Analysis>(`${API}/analyses/${encodeURIComponent(ana.name)}`, {
          title: ana.title,
          description: ana.description,
          cells: next.map((x) => x.raw),
        });
        afterWrite(result);
      } catch (e) {
        setError(translated(e));
        setDrafts(drafts); // roll the visual order back
      }
    }
  };

  const del = useMutation({
    mutationFn: () => api.del(`${API}/analyses/${ana.name}`),
    onSuccess: onDeleted,
  });

  // The server now rewrites compiler refusals into cell + card vocabulary
  // itself ("Cell 2's Summarise card refers to…"), so this client-side
  // rewrite is normally a no-op. It stays as the backstop for any message
  // shape the server-side map has not met — the same net the preview hangs
  // under its errors.
  const translated = (e: unknown): unknown => {
    if (!(e instanceof ApiError)) return e;
    const position: Record<string, number> = {};
    const fragments: RefusalContext["fragments"] = {};
    drafts.forEach((d, i) => {
      if (!d.id) return;
      position[d.id] = i + 1;
      const frag = d.raw?.flow ?? (d.shaping ? cellFragment(d.shaping, {})?.flow : undefined);
      if (frag?.nodes) {
        fragments[d.id] = (frag.nodes as any[]).map((n) => ({ id: n.id, kind: n.kind }));
      }
    });
    const detail = explainCellRefusal(e.detail, { position, fragments });
    return detail === e.detail ? e : new ApiError(e.status, detail);
  };

  const addCell = (kind: "sql" | "shaping") => {
    setDrafts((ds) => [
      ...ds,
      {
        key: nextKey(),
        id: null,
        title: "",
        kind,
        sql: "",
        shaping: kind === "shaping" ? emptyShaping() : null,
        raw: null,
        bindings: { ...AUTO_BINDINGS },
        width: 12,
        dirty: true,
      },
    ]);
  };

  return (
    <div>
      <div style={{ marginBottom: 12 }}>
        <Link to="/analyses">← Analyses</Link>
      </div>
      <PageHeader
        title={ana.title || ana.name}
        subtitle={ana.description || "Viewers of this URL see results and charts, computed with their own data access — never the queries."}
        actions={
          <span style={{ display: "inline-flex", gap: 8 }}>
            <button className="small" onClick={() => addCell("shaping")}>+ Shaping cell</button>
            <button className="small" onClick={() => addCell("sql")}>+ SQL cell</button>
            <button
              className="small danger"
              onClick={() => {
                if (window.confirm(`Delete analysis "${ana.name}"?`)) del.mutate();
              }}
            >
              Delete
            </button>
          </span>
        }
      />
      {error != null &&
        (error instanceof ApiError && error.status === 400 ? (
          // A 400 here is a first-party refusal already written for the
          // analyst ("Cell 2's Summarise card refers to…") — the HTTP status
          // is not part of the sentence, so no "Error 400:" prefix.
          <div className="an-note an-note-bad">{error.detail}</div>
        ) : (
          <ErrorBox error={error} />
        ))}
      {del.isError && <ErrorBox error={del.error} />}

      {drafts.length === 0 ? (
        <EmptyState>
          No cells yet — add a shaping cell (point-and-click, chainable) or a
          SQL cell.
        </EmptyState>
      ) : (
        drafts.map((d, i) => (
          <EditorCell
            key={d.key}
            analysis={ana.name}
            draft={d}
            position={i + 1}
            drafts={drafts}
            update={(fn) => setDrafts((ds) => ds.map((x) => (x.key === d.key ? fn(x) : x)))}
            onSave={() => void saveCell(d)}
            onDelete={() => void deleteCell(d)}
            onMove={(dir) => void moveCell(d, dir)}
            reportMeta={reportMeta}
            cellMeta={cellMeta}
            saving={savingKey === d.key}
          />
        ))
      )}

      {/* Can happen to an editor only in an odd state (a demoted session, a
          stale tab): a cell that arrived WITHOUT its operational half must
          not be edited — a save would write the hole back as if it were
          data. */}
      {drafts.some((d) => d.raw !== null && !cellIsWhole(d.raw)) && (
        <Withheld
          what="Some cells' instructions"
          role="editor"
          label="not editable"
          why="Reload the page; if it persists, your session's role changed."
        />
      )}

      <style>{ANALYSES_STYLES}</style>
    </div>
  );
}

const ANALYSES_STYLES = `
.an-card { border: 1px solid var(--border, #2a2f3a55); border-radius: 8px; padding: 10px 12px; margin-top: 10px; }
.an-card-title { font-size: 11.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; opacity: 0.65; margin-bottom: 6px; }
.an-row { display: flex; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; align-items: center; }
.an-row select, .an-row input { min-width: 0; }
.an-add { background: none; border: 1px dashed var(--border, #2a2f3a88); border-radius: 6px; padding: 4px 10px; font-size: 12px; cursor: pointer; opacity: 0.8; }
.an-add:hover { opacity: 1; }
.an-x { background: none; border: none; cursor: pointer; font-size: 15px; opacity: 0.6; padding: 0 4px; }
.an-x:hover { opacity: 1; }
.an-note { font-size: 12.5px; padding: 8px 10px; border-radius: 6px; background: rgba(140, 150, 170, 0.12); margin-top: 8px; }
.an-note-bad { background: rgba(220, 90, 90, 0.12); }
`;
