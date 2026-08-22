// Explore's client model: screen state and ONE pure function that turns it
// into a FlowDef.
//
// This file is the whole representation layer of Explore. There is no
// ExploreSpec on the server, on the wire, or in storage — the UI holds the
// state below and `exploreFlow` synthesizes a linear flow
// (source → filter → derive* → aggregate → sort) that goes through the exact
// stack every Flow goes through: Tier A validation, the one compiler with
// every value bound, the one SQL execution path. If you are tempted to add a
// "lighter" wire format here, that is a second compiler and a second
// injection surface — the mistake this seam exists to prevent.
//
// The reverse function `stateFromFlow` exists so a saved panel can be
// reopened in Explore. It parses ONLY the shapes `exploreFlow` emits; a flow
// authored in the Flow builder that wandered off that shape parses to null
// and the UI says so instead of silently flattening it.

import type {
  FlowAggFn,
  FlowDef,
  FlowExpr,
  FlowKind,
  FlowNode,
} from "../../types";

// ------------------------------------------------------------------- state

/** Filter operators Explore offers — the condition subset of the Flow vocab. */
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

export interface ExploreState {
  dataset: string;
  filters: ExploreFilter[];
  groups: ExploreGroup[];
  measures: ExploreMeasure[];
  sort: ExploreSort | null;
  /** Top-N, free-typed. "" = no limit. Travels as the panel's `top`, which
   *  the server binds as LIMIT ? at the terminal — never a mid-flow node. */
  top: string;
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

/** The name the bucketed column gets in the result — also what the chart's x
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

function filterExpr(f: ExploreFilter, kinds: Record<string, FlowKind>): FlowExpr | null {
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
 *  on step 's2'", about a step the analyst has never seen. */
const NEW_NAME_RE = /^[A-Za-z_][A-Za-z0-9_ ]{0,127}$/;

/** Why this state cannot preview yet, as sentences for the card. Empty = go.
 *  Everything the server would refuse in compiler vocabulary is caught here
 *  first, in analyst vocabulary — both states (duplicate groups, punctuation
 *  in a summary name) are reachable in two clicks, so "synthesized shapes
 *  make refusals rare" was wishful. */
export function exploreIssues(
  state: ExploreState,
  kinds: Record<string, FlowKind>,
): string[] {
  const issues: string[] = [];
  if (!state.dataset) issues.push("Pick a dataset.");
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
  if (state.measures.length === 0) issues.push("Add at least one summary.");
  const seenAliases = new Set<string>();
  for (const m of state.measures) {
    if (m.fn !== "count_star" && !m.column) issues.push("Pick a column to summarise.");
    const alias = m.alias.trim();
    if (!alias) {
      issues.push("Give every summary a name.");
    } else if (!NEW_NAME_RE.test(m.alias)) {
      issues.push(
        `Summary names can use letters, digits, underscores and spaces — ` +
          `rename “${m.alias}”.`,
      );
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

/** The columns the synthesized flow's result will have, in order. */
export function resultColumns(state: ExploreState): string[] {
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

/**
 * Synthesize the FlowDef. Call only when `exploreIssues` is empty; an
 * unfinished state returns null rather than a flow the server must refuse.
 *
 * Shape, always linear:
 * source → [filter] → [cast]* → [derive]* → aggregate → [sort].
 * A time bucket is derive(date_trunc(unit, col)); a group with `parse` casts
 * its text column to timestamp first (the compiler's own `cast` step); a
 * numeric bin is derive(mul(floor(div(col, w)), w)) — the binning runs inside
 * the governed, parameter-bound statement, never in the browser over raw rows.
 */
export function exploreFlow(
  state: ExploreState,
  kinds: Record<string, FlowKind>,
  author: string,
): FlowDef | null {
  if (exploreIssues(state, kinds).length > 0) return null;

  const nodes: FlowNode[] = [];
  let prev = "s1";
  nodes.push({ id: "s1", kind: "source", inputs: [], params: { dataset: state.dataset } });

  const predicates = state.filters
    .map((f) => filterExpr(f, kinds))
    .filter((e): e is FlowExpr => e !== null);
  if (predicates.length > 0) {
    const predicate: FlowExpr =
      predicates.length === 1
        ? predicates[0]
        : { t: "op", op: "and", args: predicates };
    const id = `s${nodes.length + 1}`;
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
    const id = `s${nodes.length + 1}`;
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
    const id = `s${nodes.length + 1}`;
    nodes.push({ id, kind: "derive", inputs: [prev], params: { name, expr } });
    prev = id;
  }

  // A sort that names a column the result no longer produces (the author
  // renamed the measure it pointed at) is silently dropped rather than
  // refused: the refusal would arrive in compiler vocabulary ("step 's3'")
  // about a control the author never sees as a step. `sortColumn` below and
  // the select in the Order card apply the same rule, so what previews is
  // what the card shows.
  const sortColumn =
    state.sort && resultColumns(state).includes(state.sort.column)
      ? state.sort.column
      : null;

  const aggId = `s${nodes.length + 1}`;
  nodes.push({
    id: aggId,
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
  prev = aggId;

  if (sortColumn) {
    const id = `s${nodes.length + 1}`;
    nodes.push({
      id,
      kind: "sort",
      inputs: [prev],
      params: { by: [{ column: sortColumn, dir: state.sort!.dir, nulls: "last" }] },
    });
    prev = id;
  }

  return {
    // A fixed, valid flow name. Nothing in the Explore path checks output-name
    // collisions because nothing is ever materialized under this name.
    name: "explore",
    output: "explore",
    author,
    description: "",
    terminal: prev,
    nodes,
    expectations: [],
  };
}

// ------------------------------------------------------------ reverse parse

function parseFilter(e: FlowExpr): ExploreFilter | null {
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

/** Reopen a saved panel's flow as Explore state. Returns null for any flow
 *  that is not exactly the shape `exploreFlow` writes — including the `{}` a
 *  raw-SQL panel stores in its `flow` field (the model's default), which once
 *  reached `.nodes.map` and took the whole app down with it. */
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
  const derived = new Map<string, ExploreGroup>();
  const parsed = new Set<string>();
  let seen: "source" | "filter" | "cast" | "derive" | "aggregate" | "sort" = "source";

  for (const node of spine.slice(1)) {
    if (node.kind === "filter" && seen === "source") {
      const p = node.params.predicate as FlowExpr;
      const parts = p.t === "op" && p.op === "and" ? p.args : [p];
      for (const part of parts) {
        const f = parseFilter(part);
        if (!f) return null;
        state.filters.push(f);
      }
      seen = "filter";
    } else if (
      node.kind === "cast" &&
      (seen === "source" || seen === "filter" || seen === "cast")
    ) {
      // Only the cast `exploreFlow` writes: text read as timestamps for a
      // date bucket. Anything else is Flow-builder territory.
      if (node.params.to !== "timestamp" || typeof node.params.column !== "string") return null;
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
      if (!g) return null;
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
    } else if (node.kind === "sort" && seen === "aggregate") {
      const by = (node.params.by ?? [])[0];
      if (!by) return null;
      state.sort = { column: by.column, dir: by.dir === "desc" ? "desc" : "asc" };
      seen = "sort";
    } else {
      return null;
    }
  }
  if (seen !== "aggregate" && seen !== "sort") return null;
  // Every cast must be claimed by a parsed group; otherwise reopening would
  // silently drop the cast and saving would silently change the query.
  for (const column of parsed) {
    if (!state.groups.some((g) => g.column === column && g.parse)) return null;
  }
  return state;
}

// -------------------------------------------------------------- result kinds

/** The kind a *result* column will have, for defaulting the sort direction
 *  and labeling it: a date bucket is time however its source was typed, a bin
 *  is a number, a measure is almost always a number. */
export function resultColumnKind(
  state: ExploreState,
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
  return "";
}

// ---------------------------------------------------------- refusal rewrite

/** What each synthesized node is called on screen. The compiler's refusals
 *  name steps ("Step 's3' groups by the same column more than once") because
 *  Flow authors see steps; Explore analysts see cards. `exploreIssues`
 *  catches everything we know how to say first — this is the net under it,
 *  so a refusal that still gets through arrives in the card's vocabulary. */
const CARD_OF: Record<string, string> = {
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

/** Everything on the Explore screen, as one serializable draft. Kept in
 *  sessionStorage: an accidental reload used to wipe an eight-interaction
 *  shaping session with no warning, for an audience that takes minutes, not
 *  seconds, to rebuild it. */
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

export const DRAFT_KEY = "laurelin.explore.draft.v1";

export function serializeDraft(d: ExploreDraft): string {
  return JSON.stringify({ v: 1, ...d });
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
  const s = raw.state;
  const o = raw.obj;
  const b = raw.bindings;
  const strArray = (a: unknown) => Array.isArray(a) && a.every((x) => typeof x === "string");
  if (
    (raw.tab !== "datasets" && raw.tab !== "objects") ||
    !s || typeof s.dataset !== "string" ||
    !Array.isArray(s.filters) || !Array.isArray(s.groups) || !Array.isArray(s.measures) ||
    typeof s.top !== "string" ||
    !o || typeof o.typeName !== "string" || !strArray(o.groupBy) ||
    !Array.isArray(o.metrics) || !Array.isArray(o.filters) || typeof o.search !== "string" ||
    !b || typeof b.chart !== "string" || typeof b.x !== "string" || !strArray(b.y) ||
    typeof b.series !== "string" || typeof b.stacked !== "boolean"
  ) {
    return null;
  }
  const state: ExploreState = {
    dataset: s.dataset,
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
        ? { column: s.sort.column, dir: s.sort.dir === "asc" ? "asc" : "desc" }
        : null,
    top: s.top,
  };
  return {
    tab: raw.tab,
    state,
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
    bindings: { chart: b.chart, x: b.x, y: b.y, series: b.series, stacked: b.stacked },
  };
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
