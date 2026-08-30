// The ONE shaping card stack — Filter / Group by / Summarise / Order & top N
// — shared by the quick chart (Analyses' zero-commitment entry, formerly the
// Explore screen) and Analyses' document cells, plus the chart-binding row
// and the shared result renderers.
//
// Explore and Analyses each rendered a private copy of these cards, and every
// courtesy taught to one was silently absent from the other: auto-chronological
// sort on a date bucket, value suggestions in filter boxes, masked-column
// greying, duplicate-group prevention, the top-N-without-order warning. Each
// of those behaviors now renders from exactly one place. The state
// transitions with decisions in them live in views/shaping/model.ts as pure
// functions (withGroupBucket and friends) so tests can pin them without a
// browser.
//
// The audience is an analyst who does not write SQL. Every control is a
// dropdown over a closed vocabulary or a column picker fed by the live
// schema; the only free text is values (typed by column kind and *bound*,
// never spliced) and invented names (aliases, titles).

import { useQuery } from "@tanstack/react-query";
import { API, api } from "../../api";
import { useAuth } from "../../auth";
import { Chart } from "../../charts";
import type { ChartKind, FlowAggFn, FlowKind, QueryResult } from "../../types";
import { Column, DataTable, fmtValue, truncationNote } from "../../ui";
import {
  DATE_BUCKETS,
  FILTER_OPS,
  MEASURE_FNS,
  NUMERIC_FNS,
  defaultAlias,
  distinctValuesFlow,
  resultColumnKind,
  shapedResultColumns,
  withGroupBin,
  withGroupBucket,
  withGroupColumn,
  withSortColumn,
  type ExploreFilter,
  type ExploreFilterOp,
  type ShapingFields,
} from "./model";

export const CHART_KINDS: ChartKind[] = ["table", "bar", "line", "area", "stat", "pie", "scatter"];

// ------------------------------------------------------------ column pickers

export function ColumnOptions({
  columns,
  masked,
}: {
  columns: string[];
  /** Columns the caller's masks cover, greyed out of the picker: summarising
   *  one can only ever aggregate "***", which is governance holding but
   *  inexplicable unless the picker says so. */
  masked?: Set<string>;
}) {
  return (
    <>
      {columns.map((c) =>
        masked?.has(c) ? (
          <option key={c} value={c} disabled title="Masked for you — you would only ever summarise '***'.">
            {c} (masked for you)
          </option>
        ) : (
          <option key={c} value={c}>{c}</option>
        ),
      )}
    </>
  );
}

/** Distinct values of a text column, served by the same governed preview
 *  path as every other shaping query — so the suggestions are exactly the
 *  values the caller's own policy lets them see. */
export function ValueSuggestions({ dataset, column, id }: { dataset: string; column: string; id: string }) {
  const auth = useAuth();
  const q = useQuery({
    queryKey: ["explore-values", dataset, column],
    queryFn: ({ signal }) =>
      api.post<QueryResult>(
        `${API}/explore/preview`,
        { flow: distinctValuesFlow(dataset, column, auth.user?.username ?? "explore"), max_rows: 50 },
        signal,
      ),
    staleTime: 300_000,
    enabled: !!dataset && !!column,
  });
  return (
    <datalist id={id}>
      {(q.data?.rows ?? []).map((r, i) => (
        <option key={i} value={String(r[column] ?? "")} />
      ))}
    </datalist>
  );
}

// -------------------------------------------------------------- the cards

export function ShapingCards({
  state,
  set,
  columns,
  kinds,
  masked,
  measuresRequired,
  suggestDataset,
  idPrefix,
}: {
  state: ShapingFields;
  /** Callers whose state carries extra fields (a dataset, a cell source)
   *  merge the returned fields back over their own state. */
  set: (fn: (s: ShapingFields) => ShapingFields) => void;
  /** Columns + kinds of the source (dataset schema or upstream cell's
   *  preview schema). */
  columns: string[];
  kinds: Record<string, FlowKind>;
  /** Columns masked for this caller, greyed in the measure pickers. */
  masked?: Set<string>;
  /** Quick chart: a chart needs at least one summary. Document cells: a
   *  cell with none returns the shaped rows themselves. */
  measuresRequired: boolean;
  /** When the source is a dataset, its name — filter boxes then suggest the
   *  column's real values through the governed preview path. A cell-sourced
   *  shaping has no dataset to ask, so it gets no suggestions (yet). */
  suggestDataset?: string | null;
  /** Namespace for datalist/input ids — several card stacks share a page. */
  idPrefix: string;
}) {
  const numericColumns = columns.filter((c) => !kinds[c] || kinds[c] === "number");
  const aggregating = state.measures.length > 0;
  const resultCols = shapedResultColumns(state, columns);

  // Text columns whose filters could use value suggestions, one datalist each.
  const suggestColumns = suggestDataset
    ? [
        ...new Set(
          state.filters
            .filter((f) => f.column && (kinds[f.column] ?? "text") === "text")
            .map((f) => f.column),
        ),
      ]
    : [];

  return (
    <>
      {/* ------------------------------------------------------------ filter */}
      <div className="ex-card">
        <div className="ex-card-title">Filter</div>
        {state.filters.map((f, i) => {
          const kind = kinds[f.column] ?? "";
          const needsValue = f.op !== "is_null" && f.op !== "is_not_null";
          const isList = f.op === "in" || f.op === "not_in";
          return (
            <div key={i} className="ex-row">
              <select
                aria-label="Filter column"
                value={f.column}
                onChange={(e) =>
                  set((s) => ({
                    ...s,
                    filters: s.filters.map((x, j) => (j === i ? { ...x, column: e.target.value } : x)),
                  }))
                }
              >
                <option value="">Pick a column…</option>
                <ColumnOptions columns={columns} />
              </select>
              <select
                aria-label="Filter condition"
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
                  aria-label="Filter value"
                  value={f.value}
                  // Suggest the column's real values: without them the analyst
                  // must already know exact spelling and casing, and a typo
                  // silently matches nothing.
                  list={
                    suggestDataset && kind !== "number" && kind !== "time" && kind !== "boolean" && f.column
                      ? `${idPrefix}-vals-${f.column}`
                      : undefined
                  }
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
                  aria-label="Filter values, comma-separated"
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
                className="ex-x"
                aria-label="Remove this filter"
                title="Remove this filter"
                onClick={() => set((s) => ({ ...s, filters: s.filters.filter((_, j) => j !== i) }))}
              >
                ×
              </button>
            </div>
          );
        })}
        <button
          className="ex-add"
          onClick={() =>
            set((s) => ({
              ...s,
              filters: [...s.filters, { column: "", op: "eq", value: "", values: [] } as ExploreFilter],
            }))
          }
        >
          + keep only rows where…
        </button>
        {suggestColumns.map((c) => (
          <ValueSuggestions key={c} dataset={suggestDataset!} column={c} id={`${idPrefix}-vals-${c}`} />
        ))}
      </div>

      {/* ---------------------------------------------------------- group by */}
      {(measuresRequired || aggregating) && (
        <div className="ex-card">
          <div className="ex-card-title">Group by</div>
          {state.groups.map((g, i) => {
            const kind = kinds[g.column] ?? "";
            // A column already grouped plain must not be offered again: the
            // duplicate compiles to "GROUP BY x, x", which the server refuses
            // in step vocabulary the analyst has never seen.
            const usedPlain = new Set(
              state.groups
                .filter((x, j) => j !== i && x.column && !x.bucket)
                .map((x) => x.column),
            );
            return (
              <div key={i} className="ex-row">
                <select
                  aria-label="Group by column"
                  value={g.column}
                  onChange={(e) => set((s) => withGroupColumn(s, i, e.target.value))}
                >
                  <option value="">Pick a column…</option>
                  <ColumnOptions columns={columns.filter((c) => c === g.column || !usedPlain.has(c))} />
                </select>
                {(kind === "time" || kind === "text") && (
                  <select
                    aria-label="Date bucket"
                    value={g.bucket === "bin" ? "" : g.bucket}
                    onChange={(e) =>
                      set((s) => withGroupBucket(s, i, e.target.value as any, kind, columns))
                    }
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
                      aria-label="Exact values or ranges"
                      value={g.bucket === "bin" ? "bin" : ""}
                      onChange={(e) =>
                        set((s) => withGroupBin(s, i, e.target.value === "bin", columns))
                      }
                    >
                      <option value="">exact values</option>
                      <option value="bin">in ranges of…</option>
                    </select>
                    {g.bucket === "bin" && (
                      <input
                        aria-label="Range size"
                        style={{ width: 76 }}
                        value={g.binWidth}
                        placeholder="10"
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
                  className="ex-x"
                  aria-label="Remove this grouping"
                  title="Remove this grouping"
                  onClick={() => set((s) => ({ ...s, groups: s.groups.filter((_, j) => j !== i) }))}
                >
                  ×
                </button>
              </div>
            );
          })}
          <button
            className="ex-add"
            onClick={() =>
              set((s) => ({
                ...s,
                groups: [...s.groups, { column: "", bucket: "" as const, binWidth: "", parse: false }],
              }))
            }
          >
            + one row per…
          </button>
          {measuresRequired && state.groups.length === 0 && (
            <div className="hint">No grouping = one summary row over everything.</div>
          )}
        </div>
      )}

      {/* ---------------------------------------------------------- measures */}
      <div className="ex-card">
        <div className="ex-card-title">Summarise</div>
        {!measuresRequired && !aggregating && (
          <div className="hint" style={{ marginBottom: 6 }}>
            No summaries — this cell returns the rows themselves. Add one to aggregate.
          </div>
        )}
        {state.measures.map((m, i) => (
          <div key={i} className="ex-row">
            <select
              aria-label="Summary function"
              value={m.fn}
              onChange={(e) => {
                const fn = e.target.value as FlowAggFn;
                set((s) => ({
                  ...s,
                  measures: s.measures.map((x, j) =>
                    j === i
                      ? {
                          fn,
                          column: fn === "count_star" ? "" : x.column,
                          // Follow the default alias unless the author renamed
                          // it. An empty alias follows too: a summary that
                          // starts blank should self-name like the first one
                          // does, not block preview with "give it a name".
                          alias:
                            x.alias === defaultAlias(x.fn, x.column) || x.alias.trim() === ""
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
              <>
                <span className="ex-kw">of</span>
                <select
                  aria-label="Column to summarise"
                  value={m.column}
                  onChange={(e) => {
                    const column = e.target.value;
                    set((s) => ({
                      ...s,
                      measures: s.measures.map((x, j) =>
                        j === i
                          ? {
                              ...x,
                              column,
                              alias:
                                x.alias === defaultAlias(x.fn, x.column) || x.alias.trim() === ""
                                  ? defaultAlias(x.fn, column)
                                  : x.alias,
                            }
                          : x,
                      ),
                    }));
                  }}
                >
                  <option value="">Pick a column…</option>
                  <ColumnOptions
                    columns={NUMERIC_FNS.has(m.fn) ? numericColumns : columns}
                    masked={masked}
                  />
                </select>
              </>
            )}
            <span className="ex-kw">called</span>
            <input
              aria-label="Name in the result"
              style={{ width: 130 }}
              value={m.alias}
              onChange={(e) =>
                set((s) => ({
                  ...s,
                  measures: s.measures.map((x, j) => (j === i ? { ...x, alias: e.target.value } : x)),
                }))
              }
            />
            <button
              className="ex-x"
              disabled={measuresRequired && state.measures.length === 1}
              aria-label="Remove this summary"
              title={
                measuresRequired && state.measures.length === 1
                  ? "a chart needs at least one summary"
                  : "Remove this summary"
              }
              onClick={() => set((s) => ({ ...s, measures: s.measures.filter((_, j) => j !== i) }))}
            >
              ×
            </button>
          </div>
        ))}
        <button
          className="ex-add"
          onClick={() =>
            set((s) => ({
              ...s,
              // Born with the default alias so it self-names as the pickers
              // change, exactly like the first summary — a blank one used to
              // block preview until the analyst typed a name by hand. The
              // FIRST summary of a rows-cell is "number of rows" (the natural
              // first question); later additions default to a total.
              measures: [
                ...s.measures,
                s.measures.length === 0
                  ? { fn: "count_star" as FlowAggFn, column: "", alias: defaultAlias("count_star", "") }
                  : { fn: "sum" as FlowAggFn, column: "", alias: defaultAlias("sum", "") },
              ],
            }))
          }
        >
          + add a summary
        </button>
      </div>

      {/* ------------------------------------------------------- sort + top */}
      <div className="ex-card">
        <div className="ex-card-title">Order &amp; top N</div>
        <div className="ex-row">
          <select
            aria-label="Order by"
            // A sort whose column the result no longer produces reads as "no
            // particular order", matching the synthesis (which drops it).
            value={
              state.sort && resultCols.includes(state.sort.column)
                ? state.sort.column
                : ""
            }
            onChange={(e) => set((s) => withSortColumn(s, e.target.value))}
          >
            <option value="">No particular order</option>
            {resultCols.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
          {state.sort && (
            <select
              aria-label="Order direction"
              value={state.sort.dir}
              onChange={(e) =>
                set((s) => ({ ...s, sort: s.sort ? { ...s.sort, dir: e.target.value as "asc" | "desc" } : null }))
              }
            >
              {/* Time columns get time words: nobody maps "smallest first"
                  to "oldest first" without stopping to think. */}
              {resultColumnKind(state, kinds, state.sort.column) === "time" ? (
                <>
                  <option value="asc">oldest first</option>
                  <option value="desc">newest first</option>
                </>
              ) : (
                <>
                  <option value="asc">smallest first</option>
                  <option value="desc">largest first</option>
                </>
              )}
            </select>
          )}
        </div>
        <div className="ex-row">
          <span className="ex-kw">keep the top</span>
          <input
            aria-label="Top N"
            style={{ width: 76 }}
            value={state.top}
            placeholder="all"
            onChange={(e) => set((s) => ({ ...s, top: e.target.value }))}
          />
          <span className="ex-kw">rows</span>
        </div>
        {state.top.trim() !== "" && !state.sort && (
          <div className="hint">
            Top N without an order keeps an <em>arbitrary</em> N — pick an order above to
            make it “the biggest N”.
          </div>
        )}
      </div>
    </>
  );
}

// -------------------------------------------------------------- bindings

/** Presentation bindings, shared by every shaping surface. */
export interface Bindings {
  chart: ChartKind;
  x: string;
  y: string[];
  series: string;
  stacked: boolean;
}

export function ChartKindBar({
  value,
  onChange,
}: {
  value: ChartKind;
  onChange: (k: ChartKind) => void;
}) {
  return (
    <div className="ex-kinds">
      {CHART_KINDS.map((k) => (
        <button
          key={k}
          className={`small${value === k ? " primary" : ""}`}
          onClick={() => onChange(k)}
        >
          {k}
        </button>
      ))}
    </div>
  );
}

export function BindingsRow({
  bindings,
  setBindings,
  columns,
  numericCols,
  categoricalCols,
  idPrefix,
}: {
  bindings: Bindings;
  setBindings: (fn: (b: Bindings) => Bindings) => void;
  columns: string[];
  numericCols: string[];
  categoricalCols: string[];
  idPrefix: string;
}) {
  const xOptions = bindings.chart === "scatter" ? numericCols : columns;
  return (
    <div className="ex-bindings">
      <label htmlFor={`${idPrefix}-bind-x`}>
        x
        <select
          id={`${idPrefix}-bind-x`}
          value={bindings.x}
          onChange={(e) => setBindings((b) => ({ ...b, x: e.target.value }))}
        >
          <option value="">(auto)</option>
          {xOptions.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
      </label>
      <span className="ex-bind-group">
        y
        <span className="ex-ychecks">
          {numericCols.length === 0 && <span className="faint">no number columns</span>}
          {numericCols.map((c, i) => (
            <label key={c} className="check-inline" htmlFor={`${idPrefix}-bind-y-${i}`}>
              <input
                id={`${idPrefix}-bind-y-${i}`}
                type="checkbox"
                checked={bindings.y.includes(c)}
                onChange={(e) =>
                  setBindings((b) => ({
                    ...b,
                    y: e.target.checked
                      ? [...b.y.filter((x) => x !== c), c]
                      : b.y.filter((x) => x !== c),
                  }))
                }
              />
              <span className="mono" style={{ fontSize: 11.5 }}>{c}</span>
            </label>
          ))}
          {bindings.y.length === 0 && numericCols.length > 0 && (
            <span className="faint">(all)</span>
          )}
        </span>
      </span>
      {bindings.chart !== "pie" && bindings.chart !== "scatter" && (
        <label htmlFor={`${idPrefix}-bind-series`}>
          split by
          <select
            id={`${idPrefix}-bind-series`}
            value={bindings.series}
            onChange={(e) => setBindings((b) => ({ ...b, series: e.target.value }))}
          >
            <option value="">(none)</option>
            {categoricalCols.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
          {/* Split-by can only offer columns the result carries. That is a
              long-vs-wide fact no analyst should need to know, so say the
              road: the column must be grouped first. */}
          {!bindings.series && (
            <span className="faint" style={{ fontSize: 11 }}>
              — to split by a column, add it under Group&nbsp;by first
            </span>
          )}
        </label>
      )}
      {bindings.chart === "bar" && (
        <label className="check-inline" htmlFor={`${idPrefix}-bind-stacked`}>
          <input
            id={`${idPrefix}-bind-stacked`}
            type="checkbox"
            checked={bindings.stacked}
            onChange={(e) => setBindings((b) => ({ ...b, stacked: e.target.checked }))}
          />
          <span>stacked</span>
        </label>
      )}
    </div>
  );
}

// --------------------------------------------------------------- results

export function ResultTable({ result, maxHeight = 300 }: { result: QueryResult; maxHeight?: number }) {
  const columns: Column<Record<string, unknown>>[] = result.columns.map((c) => ({
    label: c,
    className: "mono",
    render: (row) => fmtValue(row[c]),
  }));
  return (
    <div>
      <div style={{ maxHeight, overflowY: "auto" }}>
        <DataTable columns={columns} rows={result.rows} rowKey={(_, i) => String(i)} />
      </div>
      <div className="faint" style={{ fontSize: 11, marginTop: 6, display: "flex", gap: 8 }}>
        {result.row_count.toLocaleString("en-US")} row{result.row_count === 1 ? "" : "s"}
        {result.truncated && (
          <span className="badge badge-gold">{truncationNote(result.row_count)}</span>
        )}
      </div>
    </div>
  );
}

export function CellResult({
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

// ------------------------------------------------------------------ styles

/** The card stack's styles, once. Class names keep the ex- prefix the
 *  original Explore screen used — the mounted guards key on them. */
export const SHAPING_STYLES = `
.ex-shape { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.ex-card {
  background: var(--bg-1); border: 1px solid var(--border);
  border-radius: 10px; padding: 12px 14px;
}
.ex-card-title {
  font-size: 10.5px; letter-spacing: 0.12em; text-transform: uppercase;
  color: var(--text-faint); margin-bottom: 8px;
}
.ex-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
.ex-row select, .ex-row input {
  background: var(--bg-2); color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; padding: 4px 7px; font-size: 12.5px; min-width: 0;
}
.ex-row input { flex: 1 1 90px; }
.ex-row select { max-width: 200px; }
.ex-kw { color: var(--text-faint); font-size: 12px; }
.ex-x {
  background: transparent; border: none; color: var(--text-faint);
  cursor: pointer; font-size: 14px; padding: 2px 6px; border-radius: 5px;
}
.ex-x:hover:not(:disabled) { color: var(--red); background: var(--bg-2); }
.ex-add {
  background: transparent; border: 1px dashed var(--border-2); color: var(--text-dim);
  border-radius: 6px; padding: 4px 10px; font-size: 12px; cursor: pointer; margin-top: 2px;
}
.ex-add:hover { color: var(--gold); border-color: var(--gold-dim); }
.ex-kinds { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 8px; }
.ex-bindings {
  display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
  font-size: 11.5px; color: var(--text-faint); margin-bottom: 4px;
}
.ex-bindings > label, .ex-bindings > .ex-bind-group { display: inline-flex; align-items: center; gap: 6px; }
.ex-bindings select {
  background: var(--bg-2); color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; padding: 3px 6px; font-size: 12px;
}
.ex-ychecks { display: inline-flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.ex-truncated {
  font-size: 11.5px; color: var(--gold); background: var(--gold-tint-bg);
  border: 1px solid var(--gold-dim); border-radius: 6px; padding: 5px 9px; margin-bottom: 8px;
}
`;
