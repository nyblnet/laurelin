// Analyses' cell model: the thin layer that turns the SHARED shaping model
// (views/shaping/model.ts) into a Flow IR *fragment* — `{nodes, terminal}`
// plus the upstream cell ids — which is what an AnalysisCell stores.
//
// A cell is the shared card stack with two differences, both about chaining:
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
// Everything else — literal typing, issue sentences, node synthesis, the
// reverse-parse walk — is the one shared implementation; the private copy
// that used to live here drifted from Explore's eight separate times.

import type { FlowKind, FlowNode } from "../../types";
import {
  parseShapingSpine,
  shapedResultColumns,
  shapingFieldIssues,
  synthesizeShapingNodes,
  type ShapingFields,
} from "../shaping/model";

// ------------------------------------------------------------------- state

/** "" = not picked; "ds:<dataset>" = a dataset; "cell:<id>" = an earlier
 *  shaping cell's output. */
export type CellSource = string;

export interface CellShaping extends ShapingFields {
  source: CellSource;
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

/** Why this cell cannot preview yet, as sentences for the card. Empty = go.
 *  The shared issue list (summaries optional), plus the parts chaining adds. */
export function shapingIssues(
  state: CellShaping,
  kinds: Record<string, FlowKind>,
): string[] {
  const issues: string[] = [];
  if (!state.source) issues.push("Pick where this cell reads from.");
  issues.push(...shapingFieldIssues(state, kinds, { measuresRequired: false }));
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
  return shapedResultColumns(state, sourceColumns);
}

// --------------------------------------------------------------- synthesis

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

  const upstream = sourceCell(state.source);
  const dataset = sourceDataset(state.source);
  const { nodes, terminal } = synthesizeShapingNodes(
    state,
    kinds,
    dataset ? { dataset } : { cellInput: `cell:${upstream}` },
  );
  if (nodes.length === 0) return null; // cell-sourced with no steps
  const top = state.top.trim() === "" ? null : Math.trunc(Number(state.top));
  return {
    flow: { terminal, nodes },
    inputs: upstream ? [upstream] : [],
    top,
  };
}

// ------------------------------------------------------------ reverse parse

/** Reopen a saved cell's fragment as shaping state. Returns null for any
 *  fragment that is not exactly the shape `cellFragment` writes — the UI
 *  then says so instead of silently flattening someone's hand-written IR. */
export function shapingFromCell(
  flow: { terminal?: unknown; nodes?: unknown } | undefined,
  inputs: string[] | undefined,
  top: number | null | undefined,
): CellShaping | null {
  if (!flow || !Array.isArray(flow.nodes) || typeof flow.terminal !== "string") return null;
  const nodes = flow.nodes as FlowNode[];
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const upstream = inputs && inputs.length === 1 ? inputs[0] : inputs?.length ? null : "";
  if (upstream === null) return null; // multi-input (a join): not this editor's shape

  // Walk the spine terminal-up; it must be linear and stay inside the cell.
  const spine: FlowNode[] = [];
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
  let rest: FlowNode[];
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
  if (!parseShapingSpine(rest, state, { requireAggregate: false })) return null;
  return state;
}

// ---------------------------------------------------------- refusal rewrite

/** What each synthesized step is called on screen, per step kind — the same
 *  net the quick chart hangs under its cards, hung under cells instead. The
 *  server rewrites its own refusals into this vocabulary (`_cell_vocabulary`
 *  in routes.py mirrors this map), so this is a backstop for any namespaced
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
