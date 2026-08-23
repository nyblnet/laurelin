// Analyses' client model: per-cell shaping state and the pure functions that
// turn it into a Flow IR *fragment* — `{nodes, terminal}` plus the upstream
// cell ids — which is what an AnalysisCell stores.
//
// This deliberately reuses Explore's representation layer (types, labels,
// literal typing) rather than inventing a second shaping vocabulary: a cell
// is Explore's card stack with two differences, both about chaining.
//
//   1. The SOURCE may be an earlier shaping cell ("cell:c1") instead of a
//      dataset. That one picker is the entire chaining UX. In the fragment it
//      means the first shaping step's input is the upstream cell's output —
//      the server rewrites it to the upstream cell's terminal step when it
//      synthesizes the whole ancestor closure into ONE FlowDef per run, so
//      the chain executes as one compiled, parameter-bound statement under
//      the caller's own policy.
//
//   2. Summaries are OPTIONAL. A cell with none returns the shaped rows
//      themselves (filter → sort), which is what makes "filter cell feeding
//      an aggregate cell" — the natural two-cell analysis — expressible.
//
// Like Explore, there is no query language on the wire: the fragment goes
// through `FlowDef.from_json` (Tier A), the one compiler with every value
// bound, and the one SQL execution path. A "lighter" format here would be a
// second compiler, which is a second injection surface.

import type { FlowExpr, FlowKind } from "../../types";
import {
  groupResultName,
  literalFor,
  type ExploreFilter,
  type ExploreGroup,
  type ExploreMeasure,
  type ExploreSort,
} from "../explore/model";

// ------------------------------------------------------------------- state

/** "" = not picked; "ds:<dataset>" = a dataset; "cell:<id>" = an earlier
 *  shaping cell's output. */
export type CellSource = string;

export interface CellShaping {
  source: CellSource;
  filters: ExploreFilter[];
  groups: ExploreGroup[];
  /** Empty = no aggregation: the cell returns the shaped rows themselves. */
  measures: ExploreMeasure[];
  sort: ExploreSort | null;
  /** Top-N, free-typed; travels as the cell's `top` (bound LIMIT ? at the
   *  cell's terminal — never pushed upstream into the chain). */
  top: string;
}

export function emptyShaping(source: CellSource = ""): CellShaping {
  return { source, filters: [], groups: [], measures: [], sort: null, top: "" };
}

export function sourceDataset(s: CellSource): string | null {
  return s.startsWith("ds:") ? s.slice(3) : null;
}

export function sourceCell(s: CellSource): string | null {
  return s.startsWith("cell:") ? s.slice(5) : null;
}

// ------------------------------------------------------------------ issues

const NEW_NAME_RE = /^[A-Za-z_][A-Za-z0-9_ ]{0,127}$/;

/** Why this cell cannot preview yet, as sentences for the card. Empty = go.
 *  Mirrors `exploreIssues`, minus the parts a cell relaxes (summaries are
 *  optional) and plus the parts chaining adds. */
export function shapingIssues(
  state: CellShaping,
  kinds: Record<string, FlowKind>,
): string[] {
  const issues: string[] = [];
  if (!state.source) issues.push("Pick where this cell reads from.");
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
  if (state.measures.length === 0 && state.groups.length > 0) {
    issues.push("Grouping needs at least one summary — or remove the grouping to keep the rows.");
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
    if (seenGroupNames.has(name))
      issues.push(`You are already grouping by “${name}” — remove the duplicate row.`);
    seenGroupNames.add(name);
  }
  const seenAliases = new Set<string>();
  for (const m of state.measures) {
    if (m.fn !== "count_star" && !m.column) issues.push("Pick a column to summarise.");
    const alias = m.alias.trim();
    if (!alias) {
      issues.push("Give every summary a name.");
    } else if (!NEW_NAME_RE.test(m.alias)) {
      issues.push(
        `Summary names can use letters, digits, underscores and spaces — rename “${m.alias}”.`,
      );
    } else if (seenAliases.has(alias)) {
      issues.push(`Two summaries are both called “${alias}” — give one a different name.`);
    } else if (seenGroupNames.has(alias)) {
      issues.push(`“${alias}” is already the name of a grouping — call the summary something else.`);
    }
    seenAliases.add(alias);
  }
  if (state.top.trim() !== "") {
    const n = Number(state.top);
    if (!Number.isInteger(n) || n < 1) issues.push("Top N must be a whole number of rows.");
  }
  // A cell that reads another cell and does nothing to it has no step to
  // compile. (A dataset source compiles to its own step, so it may stand
  // alone as "the raw rows".)
  if (
    sourceCell(state.source) &&
    state.filters.length === 0 && state.measures.length === 0 && !state.sort
  ) {
    issues.push("Add a step — a filter, a summary or an order. As it stands this cell would just repeat the cell above.");
  }
  return issues;
}

/** The columns this cell's result will have. Aggregating: groups + aliases.
 *  Not aggregating: the source's own columns (the caller passes them from
 *  the schema/preview of whatever the source is). */
export function cellResultColumns(
  state: CellShaping,
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

// --------------------------------------------------------------- synthesis

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

export interface CellFragment {
  flow: { terminal: string; nodes: any[] };
  inputs: string[];
  top: number | null;
}

/**
 * Synthesize this cell's stored fragment. Call only when `shapingIssues` is
 * empty; an unfinished state returns null rather than a fragment the server
 * must refuse.
 *
 * Shape, always linear within the cell:
 *   [source] → [filter] → [cast]* → [derive]* → [aggregate] → [sort]
 * with step ids s1… local to the cell — the server namespaces them
 * (`{cell_id}_{step}`) when it synthesizes the closure. A cell-sourced
 * fragment has no source step: its first step's input is `cell:<id>`.
 */
export function cellFragment(
  state: CellShaping,
  kinds: Record<string, FlowKind>,
): CellFragment | null {
  if (shapingIssues(state, kinds).length > 0) return null;

  const nodes: any[] = [];
  const upstream = sourceCell(state.source);
  const dataset = sourceDataset(state.source);
  // The input of the next step: a local step id, or the cell reference for
  // the first step of a cell-sourced fragment.
  let prev = upstream ? `cell:${upstream}` : "";

  if (dataset) {
    nodes.push({ id: "s1", kind: "source", inputs: [], params: { dataset } });
    prev = "s1";
  }

  const predicates = state.filters
    .map((f) => filterExpr(f, kinds))
    .filter((e): e is FlowExpr => e !== null);
  if (predicates.length > 0) {
    const predicate: FlowExpr =
      predicates.length === 1 ? predicates[0] : { t: "op", op: "and", args: predicates };
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
            t: "op", op: "mul",
            args: [
              { t: "op", op: "floor", args: [
                { t: "op", op: "div", args: [col, { t: "lit", type: "double", value: Number(g.binWidth) }] },
              ] },
              { t: "lit", type: "double", value: Number(g.binWidth) },
            ],
          }
        : {
            t: "op", op: "date_trunc",
            args: [{ t: "lit", type: "string", value: g.bucket }, col],
          };
    const id = `s${nodes.length + 1}`;
    nodes.push({ id, kind: "derive", inputs: [prev], params: { name, expr } });
    prev = id;
  }

  if (state.measures.length > 0) {
    const id = `s${nodes.length + 1}`;
    nodes.push({
      id, kind: "aggregate", inputs: [prev],
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

  // A sort naming a column the result no longer produces is dropped, not
  // refused — same rule and reason as Explore's Order card.
  const sortColumn =
    state.sort && state.measures.length > 0
      ? (cellResultColumns(state, []).includes(state.sort.column) ? state.sort.column : null)
      : state.sort?.column ?? null;
  if (sortColumn && state.sort) {
    const id = `s${nodes.length + 1}`;
    nodes.push({
      id, kind: "sort", inputs: [prev],
      params: { by: [{ column: sortColumn, dir: state.sort.dir, nulls: "last" }] },
    });
    prev = id;
  }

  if (nodes.length === 0) return null; // cell-sourced with no steps
  const top = state.top.trim() === "" ? null : Math.trunc(Number(state.top));
  return {
    flow: { terminal: prev, nodes },
    inputs: upstream ? [upstream] : [],
    top,
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
    return { ...base, op: e.op as ExploreFilter["op"], value: v };
  }
  return null;
}

/** Reopen a saved cell's fragment as shaping state. Returns null for any
 *  fragment that is not exactly the shape `cellFragment` writes — the UI
 *  then says so instead of silently flattening someone's hand-written IR. */
export function shapingFromCell(
  flow: { terminal?: unknown; nodes?: unknown } | undefined,
  inputs: string[] | undefined,
  top: number | null | undefined,
): CellShaping | null {
  if (!flow || !Array.isArray(flow.nodes) || typeof flow.terminal !== "string") return null;
  const nodes = flow.nodes as any[];
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const upstream = inputs && inputs.length === 1 ? inputs[0] : inputs?.length ? null : "";
  if (upstream === null) return null; // multi-input (a join): not this editor's shape

  // Walk the spine terminal-up; it must be linear and stay inside the cell.
  const spine: any[] = [];
  let cur = byId.get(flow.terminal);
  let cellInput = "";
  while (cur) {
    spine.unshift(cur);
    const ins: string[] = cur.inputs ?? [];
    if (ins.length === 0) break;
    if (ins.length > 1) return null;
    if (ins[0].startsWith("cell:")) {
      cellInput = ins[0].slice(5);
      cur = undefined;
      break;
    }
    cur = byId.get(ins[0]);
  }
  if (spine.length === 0 || spine.length !== nodes.length) return null;

  let state: CellShaping;
  let rest: any[];
  if (spine[0].kind === "source") {
    if (cellInput || (inputs ?? []).length > 0) return null;
    state = emptyShaping(`ds:${spine[0].params?.dataset ?? ""}`);
    rest = spine.slice(1);
  } else {
    if (!cellInput || cellInput !== upstream) return null;
    state = emptyShaping(`cell:${cellInput}`);
    rest = spine;
  }
  state.top = top != null ? String(top) : "";

  const derived = new Map<string, ExploreGroup>();
  const parsed = new Set<string>();
  let seen: "source" | "filter" | "cast" | "derive" | "aggregate" | "sort" = "source";

  for (const node of rest) {
    if (node.kind === "filter" && seen === "source") {
      const p = node.params.predicate as FlowExpr;
      const parts = p.t === "op" && p.op === "and" ? p.args : [p];
      for (const part of parts) {
        const f = parseFilter(part);
        if (!f) return null;
        state.filters.push(f);
      }
      seen = "filter";
    } else if (node.kind === "cast" && (seen === "source" || seen === "filter" || seen === "cast")) {
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
        g = { column, bucket: String(e.args[0].value) as ExploreGroup["bucket"],
              binWidth: "", parse: parsed.has(column) };
      } else if (e?.t === "op" && e.op === "mul") {
        const [fl, w] = e.args;
        if (
          fl?.t === "op" && fl.op === "floor" && w?.t === "lit" &&
          fl.args[0]?.t === "op" && fl.args[0].op === "div" &&
          fl.args[0].args[0]?.t === "col"
        ) {
          g = { column: fl.args[0].args[0].name, bucket: "bin",
                binWidth: String(w.value), parse: false };
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
    } else if (node.kind === "sort" && seen !== "sort") {
      const by = (node.params.by ?? [])[0];
      if (!by) return null;
      state.sort = { column: by.column, dir: by.dir === "desc" ? "desc" : "asc" };
      seen = "sort";
    } else {
      return null;
    }
  }
  // Derives claimed by no aggregate, or casts claimed by no parsed group,
  // would be silently dropped on the next save — refuse to parse instead.
  if (derived.size > 0 && state.groups.length === 0) return null;
  for (const column of parsed) {
    if (!state.groups.some((g) => g.column === column && g.parse)) return null;
  }
  return state;
}

// ---------------------------------------------------------- refusal rewrite

/** What each synthesized step is called on screen, per step kind — the same
 *  net Explore hangs under its cards, hung under cells instead. The server
 *  rewrites its own refusals into this vocabulary (`_cell_vocabulary` in
 *  routes.py mirrors this map), so this is a backstop for any namespaced
 *  step id ("Step 'c1_s2'") that still reaches the client. */
const CARD_OF: Record<string, string> = {
  source: "data source",
  filter: "Filter card",
  cast: "“read as dates” setting",
  derive: "Group by card",
  aggregate: "Summarise card",
  sort: "Order card",
};

export interface RefusalContext {
  /** cell id -> its 1-based display position. */
  position: Record<string, number>;
  /** cell id -> its fragment's nodes (for step-kind lookup). */
  fragments: Record<string, { id: string; kind: string }[]>;
}

export function explainCellRefusal(detail: string, ctx: RefusalContext): string {
  let message = detail;
  for (const [cellId, nodes] of Object.entries(ctx.fragments)) {
    const pos = ctx.position[cellId];
    for (const node of nodes) {
      const card = CARD_OF[node.kind] ?? "shaping";
      const label = pos != null ? `Cell ${pos}'s ${card}` : card;
      message = message.replace(
        new RegExp(`(?:on |for )?\\b[Ss]tep '${cellId}_${node.id}'`, "g"),
        label,
      );
    }
  }
  return message
    .replace(/a 'cast' step converting/g, "the “read as dates” setting on")
    .replace(/'cast' step/g, "“read as dates” setting");
}
