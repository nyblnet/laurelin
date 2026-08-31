// The ONE shaping model: screen state and the pure functions that turn it
// into Flow IR, shared by the quick chart (Analyses' zero-commitment entry,
// formerly the Explore screen) and Analyses' document cells.
//
// Explore and Analyses each carried a private copy of this layer, and the
// copies drifted measurably: eight verified courtesy gaps (auto-chronological
// sort, value suggestions, masked-column greying, duplicate-group refusal,
// chart defaults, …) existed on one surface and not the other. This module is
// where each of those behaviors now lives exactly once. The two callers
// differ in precisely two parameters, so that is the parameterization:
//
//   * measures REQUIRED (a chart needs at least one summary — quick chart)
//     versus OPTIONAL (a cell with none returns the shaped rows — documents);
//   * the source is a dataset (quick chart) or a dataset-or-earlier-cell
//     (documents; the cell wiring stays in views/analyses/model.ts).
//
// There is still no query language on the wire and no ExploreSpec anywhere:
// the synthesized nodes go through `FlowDef.from_json` (Tier A), the one
// compiler with every value bound, and the one SQL execution path. A
// "lighter" wire format here would be a second compiler and a second
// injection surface — the mistake this seam exists to prevent.

import type {
  FlowAggFn,
  FlowDef,
  FlowExpr,
  FlowKind,
  FlowNode,
} from "../../types";
import { NAME_RULE_LABEL } from "../../ui";

// ------------------------------------------------------------------- state

/** Filter operators the cards offer — the condition subset of the Flow vocab. */
export type ExploreFilterOp =
  | "eq" | "ne" | "gt" | "gte" | "lt" | "lte"
  | "like" | "is_null" | "is_not_null" | "in" | "not_in";

export interface ExploreFilter {
  column: string;
  op: ExploreFilterOp;
  /** Free-typed, then *typed* by the column's kind before it becomes a bound
   *  literal — never text destined for SQL. */
  value: string;
  /** For "is one of" / "is not one of": one literal per entry. */
  values: string[];
}

/** "" = group by the exact values; a date unit = bucket a time column;
 *  "bin" = bucket a number column into ranges of `binWidth`. */
export type ExploreBucket =
  | "" | "year" | "quarter" | "month" | "week" | "day" | "hour" | "bin";

export interface ExploreGroup {
  column: string;
  bucket: ExploreBucket;
  binWidth: string; // free-typed number; validated before synthesis
  /** Read a *text* column as timestamps before bucketing. Real datasets
   *  constantly arrive with ISO dates typed as text, and without this the
   *  whole class of time-series questions was silently impossible: the
   *  bucket select just never appeared, with nothing saying why. Synthesized
   *  as the compiler's own `cast` step — no new IR, no second path. */
  parse: boolean;
}

export interface ExploreMeasure {
  fn: FlowAggFn;
  column: string; // ignored for count_star
  alias: string;
}

export interface ExploreSort {
  column: string;
  dir: "asc" | "desc";
}

/** The shaping fields every surface shares. The quick chart adds `dataset`;
 *  a document cell adds `source` (dataset or earlier cell). */
export interface ShapingFields {
  filters: ExploreFilter[];
  groups: ExploreGroup[];
  /** Optional on document cells: empty = the cell returns the shaped rows. */
  measures: ExploreMeasure[];
  sort: ExploreSort | null;
  /** Top-N, free-typed. "" = no limit. Travels as the panel's/cell's `top`,
   *  which the server binds as LIMIT ? at the terminal — never mid-flow. */
  top: string;
}

/** The quick chart's state (ex-Explore). Measures are always required here:
 *  a chart needs at least one summary. */
export interface ExploreState extends ShapingFields {
  dataset: string;
}

export function emptyExplore(dataset = ""): ExploreState {
  return {
    dataset,
    filters: [],
    groups: [],
    measures: [{ fn: "count_star", column: "", alias: "row count" }],
    sort: null,
    top: "",
  };
}

export const FILTER_OPS: Record<ExploreFilterOp, string> = {
  eq: "is",
  ne: "is not",
  gt: "is greater than",
  gte: "is at least",
  lt: "is less than",
  lte: "is at most",
  like: "matches the pattern",
  is_null: "is empty",
  is_not_null: "is not empty",
  in: "is one of",
  not_in: "is not one of",
};

export const MEASURE_FNS: Record<string, string> = {
  count_star: "Number of rows",
  sum: "Total",
  avg: "Average",
  median: "Middle value (median)",
  min: "Smallest",
  max: "Largest",
  count: "Number of rows with a value",
  count_distinct: "Number of different values",
};

/** Summaries that only make sense over numbers; the picker narrows to match
 *  the compiler's own refusal instead of letting the author walk into it. */
export const NUMERIC_FNS = new Set<FlowAggFn>(["sum", "avg", "median"]);

export const DATE_BUCKETS: Record<string, string> = {
  year: "year",
  quarter: "quarter",
  month: "month",
  week: "week",
  day: "day",
  hour: "hour",
};

/** A default alias an analyst would have typed anyway. */
export function defaultAlias(fn: FlowAggFn, column: string): string {
  if (fn === "count_star") return "row count";
  const noun: Record<string, string> = {
    sum: "total", avg: "average", median: "median", min: "smallest",
    max: "largest", count: "count", count_distinct: "distinct",
    any_value: "any",
  };
  return `${noun[fn] ?? fn} ${column}`.trim();
}

// ------------------------------------------------------------- typed values

/** Type a free-typed filter value by the column's kind, as a Flow literal.
 *  Returns null when the text does not parse as that kind — the card shows
 *  the problem instead of firing a preview whose only answer is a refusal. */
export function literalFor(kind: FlowKind, text: string): FlowExpr | null {
  const v = text.trim();
  if (v === "") return null;
  if (kind === "number") {
    const n = Number(v);
    if (!Number.isFinite(n)) return null;
    return Number.isInteger(n) && /^-?\d+$/.test(v)
      ? { t: "lit", type: "bigint", value: n }
      : { t: "lit", type: "double", value: n };
  }
  if (kind === "boolean") {
    if (v === "true" || v === "yes" || v === "1") return { t: "lit", type: "boolean", value: true };
    if (v === "false" || v === "no" || v === "0") return { t: "lit", type: "boolean", value: false };
    return null;
  }
  if (kind === "time") {
    // ISO date or timestamp; the server re-validates with fromisoformat.
    if (/^\d{4}-\d{2}-\d{2}$/.test(v)) return { t: "lit", type: "date", value: v };
    if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(v))
      return { t: "lit", type: "timestamp", value: v.replace(" ", "T") };
    return null;
  }
  // text, or a column the server has no opinion about
  return { t: "lit", type: "string", value: v };
}

// ---------------------------------------------------------------- synthesis

/** The name a bucketed column gets in the result — also what the chart's x
 *  binding and the aggregate's group_by use. Must satisfy the server's
 *  invented-identifier rule (letters, digits, underscores, spaces). */
export function groupResultName(g: ExploreGroup, taken: Set<string>): string {
  if (!g.bucket) return g.column;
  const suffix = g.bucket === "bin" ? "range" : g.bucket;
  let name = `${g.column} ${suffix}`.replace(/[^A-Za-z0-9_ ]/g, "_");
  if (!/^[A-Za-z_]/.test(name)) name = `b ${name}`;
  while (taken.has(name)) name = `${name} 2`;
  return name.slice(0, 128);
}

export function filterExpr(f: ExploreFilter, kinds: Record<string, FlowKind>): FlowExpr | null {
  if (!f.column) return null;
  const col: FlowExpr = { t: "col", name: f.column };
  if (f.op === "is_null" || f.op === "is_not_null") {
    return { t: "op", op: f.op, args: [col] };
  }
  const kind = f.op === "like" ? "text" : (kinds[f.column] ?? "");
  if (f.op === "in" || f.op === "not_in") {
    const lits = f.values
      .map((v) => literalFor(kind, v))
      .filter((l): l is FlowExpr => l !== null);
    if (lits.length === 0) return null;
    return { t: "op", op: f.op, args: [col, ...lits] };
  }
  const lit = literalFor(kind, f.value);
  if (!lit) return null;
  return { t: "op", op: f.op, args: [col, lit] };
}

/** The server's rule for names an author invents (`_NEW_IDENT_RE`), copied so
 *  the refusal can be a sentence on the card *before* preview, in the card's
 *  own words — the server's version arrives as "Invalid new column name ...
 *  on step 's2'", about a step the analyst has never seen. The SENTENCE is
 *  NAME_RULE_LABEL, shared with the flow builder's rename hint, so the same
 *  rule stops being worded two ways one screen apart. */
const NEW_NAME_RE = /^[A-Za-z_][A-Za-z0-9_ ]{0,127}$/;

/** Why these shaping fields cannot preview yet, as sentences for the cards.
 *  Empty = go. Everything the server would refuse in compiler vocabulary is
 *  caught here first, in analyst vocabulary — both surfaces reach the same
 *  states (duplicate groups, punctuation in a summary name) in two clicks. */
export function shapingFieldIssues(
  state: ShapingFields,
  kinds: Record<string, FlowKind>,
  opts: { measuresRequired: boolean },
): string[] {
  const issues: string[] = [];
  for (const f of state.filters) {
    if (!f.column) {
      issues.push("A filter needs a column.");
    } else if (f.op === "in" || f.op === "not_in") {
      if (!filterExpr(f, kinds)) issues.push(`List at least one value for “${f.column}”.`);
    } else if (f.op !== "is_null" && f.op !== "is_not_null" && !filterExpr(f, kinds)) {
      const kind = kinds[f.column];
      issues.push(
        f.value.trim() === ""
          ? `Fill in a value for “${f.column}”.`
          : `“${f.value}” is not a valid ${kind === "time" ? "date (YYYY-MM-DD)" : kind || "value"} for “${f.column}”.`,
      );
    }
  }
  if (state.measures.length === 0) {
    if (opts.measuresRequired) {
      issues.push("Add at least one summary.");
    } else if (state.groups.length > 0) {
      issues.push("Grouping needs at least one summary — or remove the grouping to keep the rows.");
    }
  }
  const seenGroupNames = new Set<string>();
  const taken = new Set<string>();
  for (const g of state.groups) {
    if (!g.column) issues.push("A group needs a column.");
    if (g.bucket === "bin") {
      const w = Number(g.binWidth);
      if (!Number.isFinite(w) || w <= 0)
        issues.push(`Give “${g.column || "the binned column"}” a range size greater than 0.`);
    }
    if (!g.column) continue;
    const name = g.bucket ? groupResultName(g, taken) : g.column;
    taken.add(name);
    if (seenGroupNames.has(name)) {
      issues.push(`You are already grouping by “${name}” — remove the duplicate row.`);
    }
    seenGroupNames.add(name);
  }
  const seenAliases = new Set<string>();
  for (const m of state.measures) {
    if (m.fn !== "count_star" && !m.column) issues.push("Pick a column to summarise.");
    const alias = m.alias.trim();
    if (!alias) {
      issues.push("Give every summary a name.");
    } else if (!NEW_NAME_RE.test(m.alias)) {
      issues.push(`${NAME_RULE_LABEL} Rename “${m.alias}”.`);
    } else if (seenAliases.has(alias)) {
      issues.push(`Two summaries are both called “${alias}” — give one a different name.`);
    } else if (seenGroupNames.has(alias)) {
      issues.push(
        `“${alias}” is already the name of a grouping — call the summary something else.`,
      );
    }
    seenAliases.add(alias);
  }
  if (state.top.trim() !== "") {
    const n = Number(state.top);
    if (!Number.isInteger(n) || n < 1) issues.push("Top N must be a whole number of rows.");
  }
  return issues;
}

/** The quick chart's issue list: a dataset and at least one summary. */
export function exploreIssues(
  state: ExploreState,
  kinds: Record<string, FlowKind>,
): string[] {
  const issues: string[] = [];
  if (!state.dataset) issues.push("Pick a dataset.");
  issues.push(...shapingFieldIssues(state, kinds, { measuresRequired: true }));
  return issues;
}

/** The columns a shaping's result will have, in order. Aggregating: group
 *  names then measure aliases. Not aggregating (documents only): the
 *  source's own columns, which the caller knows and this module does not. */
export function shapedResultColumns(
  state: ShapingFields,
  sourceColumns: string[],
): string[] {
  if (state.measures.length === 0) return sourceColumns;
  const taken = new Set<string>();
  const groups = state.groups
    .filter((g) => g.column)
    .map((g) => {
      const name = groupResultName(g, taken);
      taken.add(name);
      return name;
    });
  return [...groups, ...state.measures.map((m) => m.alias).filter(Boolean)];
}

/** The quick chart's result columns (measures always present there). */
export function resultColumns(state: ExploreState): string[] {
  return shapedResultColumns(state, []);
}

/**
 * Synthesize the shared spine of nodes after the source:
 * [filter] → [cast]* → [derive]* → [aggregate?] → [sort?].
 * `prev` is the input of the first synthesized step (a source step's id, or
 * a document cell's `cell:<id>` reference); ids continue s{offset+1}….
 * A time bucket is derive(date_trunc(unit, col)); a group with `parse` casts
 * its text column to timestamp first (the compiler's own `cast` step); a
 * numeric bin is derive(mul(floor(div(col, w)), w)) — the binning runs inside
 * the governed, parameter-bound statement, never in the browser over raw rows.
 */
function synthesizeSpine(
  state: ShapingFields,
  kinds: Record<string, FlowKind>,
  prev: string,
  offset: number,
): { nodes: FlowNode[]; terminal: string } {
  const nodes: FlowNode[] = [];
  const nextId = () => `s${offset + nodes.length + 1}`;

  const predicates = state.filters
    .map((f) => filterExpr(f, kinds))
    .filter((e): e is FlowExpr => e !== null);
  if (predicates.length > 0) {
    const predicate: FlowExpr =
      predicates.length === 1
        ? predicates[0]
        : { t: "op", op: "and", args: predicates };
    const id = nextId();
    nodes.push({ id, kind: "filter", inputs: [prev], params: { predicate } });
    prev = id;
  }

  // One cast per distinct parsed column, before any derive that reads it.
  const parseCols: string[] = [];
  for (const g of state.groups) {
    if (g.column && g.parse && g.bucket && g.bucket !== "bin" && !parseCols.includes(g.column))
      parseCols.push(g.column);
  }
  for (const column of parseCols) {
    const id = nextId();
    nodes.push({ id, kind: "cast", inputs: [prev], params: { column, to: "timestamp" } });
    prev = id;
  }

  const taken = new Set<string>();
  const groupCols: string[] = [];
  for (const g of state.groups) {
    if (!g.column) continue;
    if (!g.bucket) {
      groupCols.push(g.column);
      taken.add(g.column);
      continue;
    }
    const name = groupResultName(g, taken);
    taken.add(name);
    groupCols.push(name);
    const col: FlowExpr = { t: "col", name: g.column };
    const expr: FlowExpr =
      g.bucket === "bin"
        ? {
            t: "op",
            op: "mul",
            args: [
              {
                t: "op",
                op: "floor",
                args: [
                  {
                    t: "op",
                    op: "div",
                    args: [col, { t: "lit", type: "double", value: Number(g.binWidth) }],
                  },
                ],
              },
              { t: "lit", type: "double", value: Number(g.binWidth) },
            ],
          }
        : {
            t: "op",
            op: "date_trunc",
            args: [{ t: "lit", type: "string", value: g.bucket }, col],
          };
    const id = nextId();
    nodes.push({ id, kind: "derive", inputs: [prev], params: { name, expr } });
    prev = id;
  }

  if (state.measures.length > 0) {
    const id = nextId();
    nodes.push({
      id,
      kind: "aggregate",
      inputs: [prev],
      params: {
        group_by: groupCols,
        aggs: state.measures.map((m) => ({
          fn: m.fn,
          column: m.fn === "count_star" ? null : m.column,
          as: m.alias,
        })),
      },
    });
    prev = id;
  }

  // A sort that names a column the result no longer produces (the author
  // renamed the measure it pointed at) is silently dropped rather than
  // refused: the refusal would arrive in compiler vocabulary ("step 's3'")
  // about a control the author never sees as a step. The Order card's select
  // applies the same rule, so what previews is what the card shows. A
  // non-aggregated document cell passes rows through, so any sort column may
  // stand — its validity is the source schema's, which the server checks.
  const sortColumn =
    state.sort && state.measures.length > 0
      ? (shapedResultColumns(state, []).includes(state.sort.column) ? state.sort.column : null)
      : state.sort?.column ?? null;
  if (sortColumn && state.sort) {
    const id = nextId();
    nodes.push({
      id,
      kind: "sort",
      inputs: [prev],
      params: { by: [{ column: sortColumn, dir: state.sort.dir, nulls: "last" }] },
    });
    prev = id;
  }

  return { nodes, terminal: prev };
}

/** Synthesize the shared spine for a document cell: no source step when the
 *  cell reads an earlier cell (`prev` = "cell:<id>"), s1 source otherwise.
 *  Exposed for views/analyses/model.ts; quick-chart callers use exploreFlow. */
export function synthesizeShapingNodes(
  state: ShapingFields,
  kinds: Record<string, FlowKind>,
  start: { dataset: string } | { cellInput: string },
): { nodes: FlowNode[]; terminal: string } {
  if ("dataset" in start) {
    const source: FlowNode = {
      id: "s1", kind: "source", inputs: [], params: { dataset: start.dataset },
    };
    const spine = synthesizeSpine(state, kinds, "s1", 1);
    return { nodes: [source, ...spine.nodes], terminal: spine.nodes.length ? spine.terminal : "s1" };
  }
  return synthesizeSpine(state, kinds, start.cellInput, 0);
}

/**
 * Synthesize the quick chart's FlowDef. Call only when `exploreIssues` is
 * empty; an unfinished state returns null rather than a flow the server must
 * refuse. Shape, always linear:
 * source → [filter] → [cast]* → [derive]* → aggregate → [sort].
 */
export function exploreFlow(
  state: ExploreState,
  kinds: Record<string, FlowKind>,
  author: string,
): FlowDef | null {
  if (exploreIssues(state, kinds).length > 0) return null;
  const { nodes, terminal } = synthesizeShapingNodes(state, kinds, { dataset: state.dataset });
  return {
    // A fixed, valid flow name. Nothing in the quick-chart path checks
    // output-name collisions because nothing is materialized under this name.
    name: "explore",
    output: "explore",
    author,
    description: "",
    terminal,
    nodes,
    expectations: [],
  };
}

// -------------------------------------------------------- card transitions

/** Pick a group row's column: the bucket belongs to the old column's kind,
 *  so it resets — and a fresh grouping with no order yet defaults to its own
 *  ascending order, visibly, in the Order card where the author can change
 *  it. Grouped-and-unordered charts render in whatever order the engine
 *  returns — plausible-looking noise. */
export function withGroupColumn(s: ShapingFields, i: number, column: string): ShapingFields {
  return {
    ...s,
    groups: s.groups.map((x, j) =>
      j === i ? { column, bucket: "" as ExploreBucket, binWidth: "", parse: false } : x,
    ),
    sort: column && !s.sort ? { column, dir: "asc" as const } : s.sort,
  };
}

/** Choose a date bucket for a group row. A text column bucketed by date needs
 *  reading as one first — the compiler's own cast step, synthesized for the
 *  analyst. And a time series nobody ordered charts in whatever order the
 *  engine grouped it, so the sort defaults to chronological the moment a
 *  bucket is chosen — visibly, in the Order card — unless the author's own
 *  sort still names a real result column. */
export function withGroupBucket(
  s: ShapingFields,
  i: number,
  bucket: ExploreBucket,
  kind: FlowKind | "",
  sourceColumns: string[] = [],
): ShapingFields {
  const parse = kind === "text" && !!bucket;
  const groups = s.groups.map((x, j) => (j === i ? { ...x, bucket, parse } : x));
  const keep =
    s.sort && shapedResultColumns({ ...s, groups }, sourceColumns).includes(s.sort.column);
  const sort =
    bucket && !keep
      ? { column: groupResultName(groups[i], new Set<string>()), dir: "asc" as const }
      : s.sort;
  return { ...s, groups, sort };
}

/** Toggle a numeric group row between exact values and ranges. Same rule as
 *  the date buckets: a histogram whose bins render in arbitrary order is a
 *  shuffled distribution that looks like a valid chart. */
export function withGroupBin(
  s: ShapingFields,
  i: number,
  bin: boolean,
  sourceColumns: string[] = [],
): ShapingFields {
  const groups = s.groups.map((x, j) =>
    j === i
      ? {
          ...x,
          bucket: (bin ? "bin" : "") as ExploreBucket,
          binWidth: bin ? x.binWidth || "10" : "",
          parse: false,
        }
      : x,
  );
  const keep =
    s.sort && shapedResultColumns({ ...s, groups }, sourceColumns).includes(s.sort.column);
  const sort =
    bin && !keep
      ? { column: groupResultName(groups[i], new Set<string>()), dir: "asc" as const }
      : s.sort;
  return { ...s, groups, sort };
}

/** Pick a sort column. Direction defaults by what the column *is*: a
 *  grouping ascends (a time column sorted "largest first" is a time series
 *  running backwards), a measure descends (biggest first is what "sort by
 *  the count" means). */
export function withSortColumn(s: ShapingFields, column: string): ShapingFields {
  if (!column) return { ...s, sort: null };
  const isMeasure = s.measures.some((m) => m.alias === column);
  return { ...s, sort: { column, dir: isMeasure ? ("desc" as const) : ("asc" as const) } };
}

// ------------------------------------------------------------ reverse parse

export function parseFilter(e: FlowExpr): ExploreFilter | null {
  if (e.t !== "op") return null;
  const [head, ...rest] = e.args;
  if (!head || head.t !== "col") return null;
  const base: ExploreFilter = { column: head.name, op: "eq", value: "", values: [] };
  if (e.op === "is_null" || e.op === "is_not_null") return { ...base, op: e.op };
  const litText = (l: FlowExpr): string | null =>
    l.t === "lit" ? String(l.value ?? "") : null;
  if (e.op === "in" || e.op === "not_in") {
    const values = rest.map(litText);
    if (values.some((v) => v === null)) return null;
    return { ...base, op: e.op, values: values as string[] };
  }
  if (["eq", "ne", "gt", "gte", "lt", "lte", "like"].includes(e.op)) {
    const v = rest[0] && litText(rest[0]);
    if (v == null) return null;
    return { ...base, op: e.op as ExploreFilterOp, value: v };
  }
  return null;
}

/**
 * Walk the post-source spine of a synthesized shaping, filling `state`'s
 * fields. Parses ONLY the shapes `synthesizeShapingNodes` emits; anything a
 * Flow-builder author wandered onto returns false and the caller says so
 * instead of silently flattening it. When `requireAggregate` (quick chart),
 * the walk must end at an aggregate or its sort.
 */
export function parseShapingSpine(
  rest: FlowNode[],
  state: ShapingFields,
  opts: { requireAggregate: boolean },
): boolean {
  const derived = new Map<string, ExploreGroup>();
  const parsed = new Set<string>();
  let seen: "source" | "filter" | "cast" | "derive" | "aggregate" | "sort" = "source";

  for (const node of rest) {
    if (node.kind === "filter" && seen === "source") {
      const p = node.params.predicate as FlowExpr;
      const parts = p.t === "op" && p.op === "and" ? p.args : [p];
      for (const part of parts) {
        const f = parseFilter(part);
        if (!f) return false;
        state.filters.push(f);
      }
      seen = "filter";
    } else if (
      node.kind === "cast" &&
      (seen === "source" || seen === "filter" || seen === "cast")
    ) {
      // Only the cast the synthesis writes: text read as timestamps for a
      // date bucket. Anything else is Flow-builder territory.
      if (node.params.to !== "timestamp" || typeof node.params.column !== "string") return false;
      parsed.add(node.params.column);
      seen = "cast";
    } else if (
      node.kind === "derive" &&
      (seen === "source" || seen === "filter" || seen === "cast" || seen === "derive")
    ) {
      const e = node.params.expr as FlowExpr;
      let g: ExploreGroup | null = null;
      if (e?.t === "op" && e.op === "date_trunc" && e.args[0]?.t === "lit" && e.args[1]?.t === "col") {
        const column = e.args[1].name;
        g = {
          column,
          bucket: String(e.args[0].value) as ExploreBucket,
          binWidth: "",
          parse: parsed.has(column),
        };
      } else if (e?.t === "op" && e.op === "mul") {
        const [fl, w] = e.args;
        if (
          fl?.t === "op" && fl.op === "floor" && w?.t === "lit" &&
          fl.args[0]?.t === "op" && fl.args[0].op === "div" &&
          fl.args[0].args[0]?.t === "col"
        ) {
          g = {
            column: fl.args[0].args[0].name,
            bucket: "bin",
            binWidth: String(w.value),
            parse: false,
          };
        }
      }
      if (!g) return false;
      derived.set(node.params.name, g);
      seen = "derive";
    } else if (node.kind === "aggregate" && seen !== "aggregate" && seen !== "sort") {
      for (const c of node.params.group_by ?? []) {
        state.groups.push(derived.get(c) ?? { column: c, bucket: "", binWidth: "", parse: false });
      }
      for (const a of node.params.aggs ?? []) {
        state.measures.push({ fn: a.fn, column: a.column ?? "", alias: a.as ?? "" });
      }
      seen = "aggregate";
    } else if (
      node.kind === "sort" &&
      (opts.requireAggregate ? seen === "aggregate" : seen !== "sort")
    ) {
      const by = (node.params.by ?? [])[0];
      if (!by) return false;
      state.sort = { column: by.column, dir: by.dir === "desc" ? "desc" : "asc" };
      seen = "sort";
    } else {
      return false;
    }
  }
  if (opts.requireAggregate && seen !== "aggregate" && seen !== "sort") return false;
  // Derives claimed by no aggregate, or casts claimed by no parsed group,
  // would be silently dropped on the next save — refuse to parse instead.
  if (derived.size > 0 && state.groups.length === 0) return false;
  for (const column of parsed) {
    if (!state.groups.some((g) => g.column === column && g.parse)) return false;
  }
  return true;
}

/** Reopen a saved panel's flow as quick-chart state. Returns null for any
 *  flow that is not exactly the shape `exploreFlow` writes — including the
 *  `{}` a raw-SQL panel stores in its `flow` field (the model's default),
 *  which once reached `.nodes.map` and took the whole app down with it. */
export function stateFromFlow(flow: FlowDef, top: number | null | undefined): ExploreState | null {
  if (!flow || !Array.isArray(flow.nodes) || typeof flow.terminal !== "string") return null;
  const byId = new Map(flow.nodes.map((n) => [n.id, n]));
  // Walk the spine terminal-up; it must be linear.
  const spine: FlowNode[] = [];
  let cur = byId.get(flow.terminal);
  while (cur) {
    spine.unshift(cur);
    if (cur.inputs.length === 0) break;
    if (cur.inputs.length > 1) return null;
    cur = byId.get(cur.inputs[0]);
  }
  if (spine.length === 0 || spine.length !== flow.nodes.length) return null;
  if (spine[0].kind !== "source") return null;

  const state = emptyExplore(spine[0].params.dataset ?? "");
  state.measures = [];
  state.top = top != null ? String(top) : "";
  if (!parseShapingSpine(spine.slice(1), state, { requireAggregate: true })) return null;
  return state;
}

// -------------------------------------------------------------- result kinds

/** The kind a *result* column will have, for defaulting the sort direction
 *  and labeling it: a date bucket is time however its source was typed, a bin
 *  is a number, a measure is almost always a number. A pass-through column
 *  (a document cell with no summaries) keeps its source kind. */
export function resultColumnKind(
  state: ShapingFields,
  kinds: Record<string, FlowKind>,
  column: string,
): FlowKind | "" {
  const taken = new Set<string>();
  for (const g of state.groups) {
    if (!g.column) continue;
    const name = g.bucket ? groupResultName(g, taken) : g.column;
    taken.add(name);
    if (name !== column) continue;
    if (g.bucket === "bin") return "number";
    if (g.bucket) return "time";
    return kinds[g.column] ?? "";
  }
  const m = state.measures.find((x) => x.alias === column);
  if (m) {
    if (m.fn === "min" || m.fn === "max" || m.fn === "any_value")
      return kinds[m.column] ?? "number";
    return "number";
  }
  return kinds[column] ?? "";
}

// ---------------------------------------------------------- refusal rewrite

/** What each synthesized node is called on screen. The compiler's refusals
 *  name steps ("Step 's3' groups by the same column more than once") because
 *  Flow authors see steps; shaping analysts see cards. The issue functions
 *  catch everything we know how to say first — this is the net under them,
 *  so a refusal that still gets through arrives in the card's vocabulary. */
export const CARD_OF: Record<string, string> = {
  source: "the data source",
  filter: "the Filter card",
  cast: "the “read as dates” setting",
  derive: "the Group by card",
  aggregate: "the Summarise card",
  sort: "the Order card",
};

export function explainRefusal(detail: string, flow: FlowDef | null): string {
  let message = detail;
  for (const node of flow?.nodes ?? []) {
    const card = CARD_OF[node.kind];
    if (!card) continue;
    message = message.replace(
      new RegExp(`(?:on |for )?\\b[Ss]tep '${node.id}'`, "g"),
      card,
    );
  }
  // The compiler's remedies prescribe steps by their IR names; spell them the
  // way this screen spells them.
  message = message
    .replace(/a 'cast' step converting/g, "the “read as dates” setting on")
    .replace(/'cast' step/g, "“read as dates” setting");
  return message;
}

// ------------------------------------------------------------------- drafts

/** Everything on the quick chart, as one serializable draft. Kept in
 *  sessionStorage, not localStorage, deliberately: shaping state names
 *  datasets and filter values, which should die with the browser session
 *  rather than persist on a shared machine. An accidental reload used to
 *  wipe an eight-interaction shaping session with no warning, for an
 *  audience that takes minutes, not seconds, to rebuild it. */
export interface ExploreDraft {
  tab: "datasets" | "objects";
  state: ExploreState;
  obj: {
    typeName: string;
    groupBy: string[];
    metrics: { op: string; property: string; alias: string }[];
    filters: { property: string; value: string }[];
    search: string;
  };
  bindings: { chart: string; x: string; y: string[]; series: string; stacked: boolean };
}

/** The quick chart's scratch state, under the merged surface's own name. */
export const SCRATCH_KEY = "laurelin.analyses.scratch.v1";
/** The pre-merge Explore screen's draft key. Read once, on first mount with
 *  no scratch of its own, so an in-flight shaping session survives the
 *  release that merged the screens. Delete after one release. */
export const LEGACY_SCRATCH_KEY = "laurelin.explore.draft.v1";

export function serializeDraft(d: ExploreDraft): string {
  return JSON.stringify({ v: 1, ...d });
}

const strArray = (a: unknown): a is string[] =>
  Array.isArray(a) && a.every((x) => typeof x === "string");

/** Validate the shared shaping fields of a stored draft, tolerantly: any
 *  entry that is not the shape the serializers write is dropped or nulled,
 *  never crashed on. Both draft parsers (scratch and document) go through
 *  this one function. */
export function parseShapingFields(s: any): ShapingFields | null {
  if (
    !s ||
    !Array.isArray(s.filters) || !Array.isArray(s.groups) || !Array.isArray(s.measures) ||
    typeof s.top !== "string"
  ) {
    return null;
  }
  return {
    filters: s.filters
      .filter((f: any) => f && typeof f.column === "string" && typeof f.op === "string")
      .map((f: any) => ({
        column: f.column,
        op: f.op as ExploreFilterOp,
        value: typeof f.value === "string" ? f.value : "",
        values: strArray(f.values) ? f.values : [],
      })),
    groups: s.groups
      .filter((g: any) => g && typeof g.column === "string")
      .map((g: any) => ({
        column: g.column,
        bucket: (typeof g.bucket === "string" ? g.bucket : "") as ExploreBucket,
        binWidth: typeof g.binWidth === "string" ? g.binWidth : "",
        parse: g.parse === true,
      })),
    measures: s.measures
      .filter((m: any) => m && typeof m.fn === "string" && typeof m.alias === "string")
      .map((m: any) => ({
        fn: m.fn as FlowAggFn,
        column: typeof m.column === "string" ? m.column : "",
        alias: m.alias,
      })),
    sort:
      s.sort && typeof s.sort.column === "string"
        ? { column: s.sort.column, dir: s.sort.dir === "asc" ? ("asc" as const) : ("desc" as const) }
        : null,
    top: s.top,
  };
}

function parseBindings(b: any): ExploreDraft["bindings"] | null {
  if (
    !b || typeof b.chart !== "string" || typeof b.x !== "string" || !strArray(b.y) ||
    typeof b.series !== "string" || typeof b.stacked !== "boolean"
  ) {
    return null;
  }
  return { chart: b.chart, x: b.x, y: b.y, series: b.series, stacked: b.stacked };
}

/** Parse a stored draft, returning null for anything that is not exactly the
 *  shape `serializeDraft` writes — a stale or hand-edited draft must degrade
 *  to a fresh screen, never to a crash on load. */
export function parseDraft(text: string | null): ExploreDraft | null {
  if (!text) return null;
  let raw: any;
  try {
    raw = JSON.parse(text);
  } catch {
    return null;
  }
  if (!raw || raw.v !== 1) return null;
  const o = raw.obj;
  const fields =
    raw.state && typeof raw.state.dataset === "string"
      ? parseShapingFields(raw.state)
      : null;
  const bindings = parseBindings(raw.bindings);
  if (
    (raw.tab !== "datasets" && raw.tab !== "objects") ||
    !fields || !bindings ||
    !o || typeof o.typeName !== "string" || !strArray(o.groupBy) ||
    !Array.isArray(o.metrics) || !Array.isArray(o.filters) || typeof o.search !== "string"
  ) {
    return null;
  }
  return {
    tab: raw.tab,
    state: { dataset: raw.state.dataset, ...fields },
    obj: {
      typeName: o.typeName,
      groupBy: o.groupBy,
      metrics: o.metrics
        .filter((m: any) => m && typeof m.op === "string")
        .map((m: any) => ({
          op: m.op,
          property: typeof m.property === "string" ? m.property : "",
          alias: typeof m.alias === "string" ? m.alias : m.op,
        })),
      filters: o.filters
        .filter((f: any) => f && typeof f.property === "string")
        .map((f: any) => ({ property: f.property, value: typeof f.value === "string" ? f.value : "" })),
      search: o.search,
    },
    bindings,
  };
}

/** The quick chart's draft, wherever it lives: its own key first, then — for
 *  one release — the pre-merge Explore key, so nobody's in-flight shaping
 *  session evaporates on upgrade day. */
export function loadScratchDraft(storage: Pick<Storage, "getItem">): ExploreDraft | null {
  return (
    parseDraft(storage.getItem(SCRATCH_KEY)) ??
    parseDraft(storage.getItem(LEGACY_SCRATCH_KEY))
  );
}

// A document's unsaved cells, keyed per analysis. Same storage decision as
// the scratch draft (sessionStorage; filter values die with the session).
// AnalysisEditor state was useState-only, so one refresh destroyed every
// unsaved cell — the verified worst data-loss cliff on the documents side.

export function docDraftKey(name: string): string {
  return `laurelin.analyses.doc.${name}.v1`;
}

/** One stored draft cell: the editable fields only — the saved record stays
 *  the server's, and unparseable saved shapes ride as `shaping: null`. */
export interface StoredDraftCell {
  id: string | null;
  title: string;
  kind: "sql" | "shaping";
  sql: string;
  shaping: ({ source: string } & ShapingFields) | null;
  bindings: ExploreDraft["bindings"];
  width: number;
  dirty: boolean;
}

export function serializeDocDraft(cells: StoredDraftCell[]): string {
  return JSON.stringify({ v: 1, cells });
}

export function parseDocDraft(text: string | null): StoredDraftCell[] | null {
  if (!text) return null;
  let raw: any;
  try {
    raw = JSON.parse(text);
  } catch {
    return null;
  }
  if (!raw || raw.v !== 1 || !Array.isArray(raw.cells)) return null;
  const out: StoredDraftCell[] = [];
  for (const c of raw.cells) {
    if (!c || typeof c.title !== "string" || (c.kind !== "sql" && c.kind !== "shaping")) continue;
    const bindings = parseBindings(c.bindings);
    if (!bindings) continue;
    const fields =
      c.shaping && typeof c.shaping.source === "string"
        ? parseShapingFields(c.shaping)
        : null;
    out.push({
      id: typeof c.id === "string" ? c.id : null,
      title: c.title,
      kind: c.kind,
      sql: typeof c.sql === "string" ? c.sql : "",
      shaping: fields ? { source: c.shaping.source, ...fields } : null,
      bindings,
      width: typeof c.width === "number" ? c.width : 12,
      dirty: c.dirty === true,
    });
  }
  return out;
}

// -------------------------------------------------------- value suggestions

/** The distinct values of one column, as a flow: the same governed preview
 *  path answers "what can this filter equal", so the suggestions a caller
 *  sees are exactly the values *their* policy lets them see — a masked
 *  column suggests "***", which is the honest answer. Without suggestions a
 *  typo ("South" for "south") silently matched nothing. */
export function distinctValuesFlow(dataset: string, column: string, author: string): FlowDef {
  // The count's alias must not collide (even confusably) with the grouped
  // column's own name.
  let alias = "rows";
  while (alias.toLowerCase() === column.trim().toLowerCase()) alias += "_";
  return {
    name: "explore",
    output: "explore",
    author,
    description: "",
    terminal: "s3",
    nodes: [
      { id: "s1", kind: "source", inputs: [], params: { dataset } },
      {
        id: "s2",
        kind: "aggregate",
        inputs: ["s1"],
        params: {
          group_by: [column],
          aggs: [{ fn: "count_star", column: null, as: alias }],
        },
      },
      {
        id: "s3",
        kind: "sort",
        inputs: ["s2"],
        params: { by: [{ column: alias, dir: "desc", nulls: "last" }] },
      },
    ],
    expectations: [],
  };
}
