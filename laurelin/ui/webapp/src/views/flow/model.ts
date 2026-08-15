// Client-side model over the flow IR: the graph edits the builder performs, a
// schema simulation for the column pickers, a one-line summary per step, and
// the "this step is not finished yet" check.
//
// NONE of this is a validator. `laurelin/transforms/flow_ir.py` (structure) and
// `flow_compile.py` (identifier membership against the live schema) are the
// only authorities, and both run again on every PUT, preview and build. What is
// here exists so the builder can offer the *right* dropdown options and say
// "pick a dataset" instead of firing a request whose only possible answer is a
// refusal.

import type {
  FlowDef,
  FlowExpr,
  FlowNode,
  FlowNodeKind,
  FlowOp,
} from "../../types";
import { KINDS, OPS } from "./vocab";

// ------------------------------------------------------------------ graph

/** The steps feeding `nodeId`, bottom-up, following the FIRST input only.
 *
 * The builder draws a flow as a vertical list because the measured corpus is
 * near-linear (one join in 22 transforms). A `join`'s second input is a chain
 * of its own and is drawn nested inside the join's card — so "the spine" is
 * exactly the first-input walk. */
export function spine(flow: FlowDef, nodeId: string): FlowNode[] {
  const byId = new Map(flow.nodes.map((n) => [n.id, n]));
  const out: FlowNode[] = [];
  const seen = new Set<string>();
  let cur = byId.get(nodeId);
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id);
    out.unshift(cur);
    cur = cur.inputs.length > 0 ? byId.get(cur.inputs[0]) : undefined;
  }
  return out;
}

export function nodeById(flow: FlowDef, id: string): FlowNode | undefined {
  return flow.nodes.find((n) => n.id === id);
}

/** The step that consumes `id` (there is at most one in anything the builder
 *  builds), and which input slot it consumes it in. */
export function consumerOf(
  flow: FlowDef,
  id: string,
): { node: FlowNode; slot: number } | null {
  for (const n of flow.nodes) {
    const slot = n.inputs.indexOf(id);
    if (slot >= 0) return { node: n, slot };
  }
  return null;
}

export function nextNodeId(flow: FlowDef): string {
  const used = new Set(flow.nodes.map((n) => n.id));
  for (let i = 1; ; i++) {
    const id = `s${i}`;
    if (!used.has(id)) return id;
  }
}

/** Default params for a fresh step, so a new card is never a blank object the
 *  server would refuse with a message about a missing key. */
export function defaultParams(kind: FlowNodeKind): Record<string, any> {
  switch (kind) {
    case "source":
      return { dataset: "" };
    case "filter":
      return {
        predicate: { t: "op", op: "eq", args: [{ t: "col", name: "" }, { t: "lit", type: "string", value: "" }] },
      };
    case "select":
      return { mode: "keep", columns: [] };
    case "rename":
      return { pairs: [{ from: "", to: "" }] };
    case "derive":
      return { name: "", expr: { t: "lit", type: "string", value: "" } };
    case "cast":
      return { column: "", to: "varchar" };
    case "join":
      return { how: "inner", keys: [{ left: "", right: "" }] };
    case "aggregate":
      return { group_by: [], aggs: [{ fn: "count_star", column: null, as: "row count" }] };
    case "dedupe":
      return { keys: [], order_by: [{ column: "", dir: "desc" }], keep: "first" };
    case "sort":
      return { by: [{ column: "", dir: "asc", nulls: "last" }] };
  }
}

export function emptyFlow(name: string, author: string): FlowDef {
  return {
    name,
    output: name,
    author,
    description: "",
    terminal: "s1",
    nodes: [{ id: "s1", kind: "source", inputs: [], params: { dataset: "" } }],
    expectations: [],
  };
}

/**
 * Insert a new step immediately after `afterId`, rewiring whoever consumed it.
 *
 * A `join` is the one kind that needs a second chain, so inserting one also
 * creates the `source` step it will read — otherwise the author is handed a
 * card with an input slot and no way to fill it.
 */
export function insertAfter(
  flow: FlowDef,
  afterId: string,
  kind: FlowNodeKind,
): { flow: FlowDef; selected: string } {
  const id = nextNodeId(flow);
  const nodes = flow.nodes.map((n) => ({ ...n, inputs: [...n.inputs] }));
  let extra: FlowNode[] = [];
  let inputs = [afterId];
  let selected = id;

  if (kind === "join") {
    const rightId = `${id}r`;
    extra = [{ id: rightId, kind: "source", inputs: [], params: { dataset: "" } }];
    inputs = [afterId, rightId];
    // Select the *source* of the new right-hand chain: picking the dataset is
    // the author's next move, and the join keys cannot be chosen before it.
    selected = rightId;
  }

  const step: FlowNode = { id, kind, inputs, params: defaultParams(kind) };
  const consumer = consumerOf(flow, afterId);
  const rewired = nodes.map((n) =>
    consumer && n.id === consumer.node.id
      ? { ...n, inputs: n.inputs.map((i, idx) => (idx === consumer.slot ? id : i)) }
      : n,
  );

  return {
    flow: {
      ...flow,
      nodes: [...rewired, ...extra, step],
      terminal: flow.terminal === afterId && !consumer ? id : flow.terminal,
    },
    selected,
  };
}

/**
 * Remove a step, reconnecting its consumer to its first input.
 *
 * Deleting a `join` also deletes the whole right-hand chain: those steps exist
 * only to feed it, and leaving them behind makes the flow unreachable-invalid
 * ("these steps are not connected to the flow's last step"), which the server
 * refuses on save.
 */
export function removeStep(flow: FlowDef, id: string): FlowDef {
  const node = nodeById(flow, id);
  if (!node) return flow;
  const doomed = new Set<string>([id]);

  if (node.kind === "join") {
    for (const s of spine(flow, node.inputs[1])) doomed.add(s.id);
  }

  const replacement = node.inputs[0];
  const consumer = consumerOf(flow, id);
  let nodes = flow.nodes.filter((n) => !doomed.has(n.id));
  if (consumer && replacement) {
    nodes = nodes.map((n) =>
      n.id === consumer.node.id
        ? { ...n, inputs: n.inputs.map((i, idx) => (idx === consumer.slot ? replacement : i)) }
        : n,
    );
  }
  const terminal = doomed.has(flow.terminal) ? (replacement ?? flow.terminal) : flow.terminal;
  return { ...flow, nodes, terminal };
}

export function updateParams(
  flow: FlowDef,
  id: string,
  params: Record<string, any>,
): FlowDef {
  return {
    ...flow,
    nodes: flow.nodes.map((n) => (n.id === id ? { ...n, params } : n)),
  };
}

// ------------------------------------------------------------------ schema
//
// A *local* simulation of what each step's output columns are, used only to
// populate column pickers. The server recomputes all of it and is the
// authority; if these two ever disagree the author gets a refusal naming the
// step, which is the failure mode we want rather than a silent wrong answer.

export type SourceSchemas = Record<string, string[] | undefined>;

export function schemaAt(
  flow: FlowDef,
  nodeId: string,
  sources: SourceSchemas,
): string[] | null {
  const node = nodeById(flow, nodeId);
  if (!node) return null;

  if (node.kind === "source") {
    const ds = node.params.dataset as string;
    return ds ? (sources[ds] ?? null) : null;
  }

  const input = schemaAt(flow, node.inputs[0], sources);
  if (input === null) return null;
  const p = node.params;

  switch (node.kind) {
    case "filter":
    case "cast":
    case "dedupe":
    case "sort":
      return input;
    case "select": {
      const wanted = new Set<string>(p.columns ?? []);
      return p.mode === "drop"
        ? input.filter((c) => !wanted.has(c))
        : input.filter((c) => wanted.has(c));
    }
    case "rename": {
      const map = new Map<string, string>(
        (p.pairs ?? []).filter((x: any) => x.from && x.to).map((x: any) => [x.from, x.to]),
      );
      return input.map((c) => map.get(c) ?? c);
    }
    case "derive":
      return p.name ? [...input, p.name] : input;
    case "aggregate":
      return [...(p.group_by ?? []), ...(p.aggs ?? []).map((a: any) => a.as).filter(Boolean)];
    case "join": {
      const right = schemaAt(flow, node.inputs[1], sources);
      if (right === null) return null;
      // Matches `flow_compile._compile_node`: the right side's key columns are
      // dropped, because on an inner join they equal the left's by
      // construction and carrying both would collide.
      const keyCols = new Set<string>((p.keys ?? []).map((k: any) => k.right).filter(Boolean));
      return [...input, ...right.filter((c) => !keyCols.has(c))];
    }
    default:
      return input;
  }
}

/** The columns a step's form should offer: its INPUT schema, not its output. */
export function inputSchema(
  flow: FlowDef,
  nodeId: string,
  sources: SourceSchemas,
): string[] | null {
  const node = nodeById(flow, nodeId);
  if (!node || node.inputs.length === 0) return null;
  return schemaAt(flow, node.inputs[0], sources);
}

export function rightSchema(
  flow: FlowDef,
  nodeId: string,
  sources: SourceSchemas,
): string[] | null {
  const node = nodeById(flow, nodeId);
  if (!node || node.inputs.length < 2) return null;
  return schemaAt(flow, node.inputs[1], sources);
}

/** Every dataset the flow reads — what the schema fetcher needs to load. */
export function sourceDatasets(flow: FlowDef): string[] {
  return Array.from(
    new Set(
      flow.nodes
        .filter((n) => n.kind === "source" && n.params.dataset)
        .map((n) => n.params.dataset as string),
    ),
  ).sort();
}

// ------------------------------------------------------------------ summaries

function exprText(e: FlowExpr | undefined, depth = 0): string {
  if (!e) return "…";
  if (e.t === "col") return e.name || "…";
  if (e.t === "lit") {
    if (e.type === "null") return "empty";
    if (e.type === "string") return `“${String(e.value)}”`;
    if (e.type === "boolean") return e.value ? "true" : "false";
    return e.value === "" || e.value == null ? "…" : String(e.value);
  }
  const label = OPS[e.op as FlowOp]?.label ?? e.op;
  const a = e.args ?? [];
  if (e.op === "and" || e.op === "or") {
    const sep = e.op === "and" ? " and " : " or ";
    const inner = a.map((x) => exprText(x, depth + 1)).join(sep);
    return depth > 0 ? `(${inner})` : inner;
  }
  if (e.op === "not") return `not ${exprText(a[0], depth + 1)}`;
  if (e.op === "is_null") return `${exprText(a[0], depth + 1)} is empty`;
  if (e.op === "is_not_null") return `${exprText(a[0], depth + 1)} is not empty`;
  if (e.op === "in" || e.op === "not_in") {
    const values = a.slice(1).map((x) => exprText(x, depth + 1)).join(", ");
    return `${exprText(a[0], depth + 1)} ${label} ${values || "…"}`;
  }
  if (e.op === "if_else") {
    return `if ${exprText(a[0], depth + 1)} then ${exprText(a[1], depth + 1)} otherwise ${exprText(a[2], depth + 1)}`;
  }
  if (e.op === "date_trunc") {
    return `${exprText(a[1], depth + 1)} rounded down to a whole ${exprText(a[0], depth + 1).replace(/[“”]/g, "")}`;
  }
  if (OPS[e.op as FlowOp]?.form === "call") {
    return `${exprText(a[0], depth + 1)} ${label}`;
  }
  return a.map((x) => exprText(x, depth + 1)).join(` ${label} `);
}

export { exprText };

/** The human sentence on a step card. Never shows a node id. */
export function summarise(flow: FlowDef, node: FlowNode): string {
  const p = node.params;
  switch (node.kind) {
    case "source":
      return p.dataset ? p.dataset : "No dataset picked yet";
    case "filter":
      return `Keep rows where ${exprText(p.predicate)}`;
    case "select": {
      const cols: string[] = p.columns ?? [];
      if (cols.length === 0) return "No columns picked yet";
      const verb = p.mode === "drop" ? "Drop" : "Keep only";
      return `${verb} ${cols.length === 1 ? cols[0] : `${cols.length} columns`}`;
    }
    case "rename": {
      const pairs = (p.pairs ?? []).filter((x: any) => x.from && x.to);
      if (pairs.length === 0) return "Nothing renamed yet";
      return pairs.map((x: any) => `${x.from} → ${x.to}`).join(", ");
    }
    case "derive":
      return `${p.name || "New column"} = ${exprText(p.expr)}`;
    case "cast":
      return p.column
        ? `Read ${p.column} as ${p.to === "varchar" ? "text" : p.to === "bigint" ? "a whole number" : p.to === "double" ? "a decimal number" : p.to === "boolean" ? "true/false" : p.to}`
        : "No column picked yet";
    case "join": {
      // The branch's ROOT, not its last step: a side chain that ends in a
      // rename still "brings in" the dataset it started from, and naming the
      // rename step instead would tell the author nothing.
      const root = spine(flow, node.inputs[1])[0];
      const rightName = root && root.kind === "source" ? root.params.dataset : "another dataset";
      const keys = (p.keys ?? []).filter((k: any) => k.left && k.right);
      const on = keys.length
        ? keys.map((k: any) => (k.left === k.right ? k.left : `${k.left} = ${k.right}`)).join(" and ")
        : "…";
      return `${rightName || "another dataset"}, matching on ${on}`;
    }
    case "aggregate": {
      const groups: string[] = p.group_by ?? [];
      const aggs = (p.aggs ?? []).map((a: any) => a.as).filter(Boolean);
      const what = aggs.length ? aggs.join(", ") : "…";
      return groups.length
        ? `${what}, one row per ${groups.join(" + ")}`
        : `${what}, over the whole dataset`;
    }
    case "dedupe": {
      const keys: string[] = p.keys ?? [];
      const order = (p.order_by ?? [])[0];
      const which = p.keep === "last" ? "last" : "first";
      return keys.length
        ? `One row per ${keys.join(" + ")} — the ${which} by ${order?.column || "…"}`
        : "No key picked yet";
    }
    case "sort": {
      const by = (p.by ?? []).filter((x: any) => x.column);
      if (!by.length) return "No column picked yet";
      return by.map((x: any) => `${x.column} ${x.dir === "desc" ? "high → low" : "low → high"}`).join(", ");
    }
  }
}

// ------------------------------------------------------------------ issues
//
// "This step is not finished." Deliberately NOT a validator — a step that
// passes this can still be refused by the server, and that refusal is shown on
// the same card. What this catches is the half-filled form, where sending it
// would buy a message about an empty string that names a key the author has
// never seen.

function exprIssue(e: FlowExpr | undefined): string | null {
  if (!e) return "This condition is not finished.";
  if (e.t === "col") return e.name ? null : "Pick a column.";
  if (e.t === "lit") {
    if (e.type === "null") return null;
    if (e.type === "boolean") return null;
    return e.value === "" || e.value == null ? "Fill in a value." : null;
  }
  for (const a of e.args ?? []) {
    const issue = exprIssue(a);
    if (issue) return issue;
  }
  return null;
}

// `_flow` is unused today and kept deliberately: every other function in this
// module takes the flow, and a caller should not have to remember which one
// is the exception. A future kind (a join whose two sides disagree) needs it.
export function stepIssue(_flow: FlowDef, node: FlowNode): string | null {
  const p = node.params;
  switch (node.kind) {
    case "source":
      return p.dataset ? null : "Pick a dataset to read.";
    case "filter":
      return exprIssue(p.predicate);
    case "select":
      return (p.columns ?? []).length ? null : "Pick at least one column.";
    case "rename":
      return (p.pairs ?? []).some((x: any) => !x.from || !x.to)
        ? "Every rename needs a column and a new name."
        : null;
    case "derive":
      if (!p.name) return "Give the new column a name.";
      return exprIssue(p.expr);
    case "cast":
      return p.column ? null : "Pick a column to convert.";
    case "join":
      return (p.keys ?? []).some((k: any) => !k.left || !k.right)
        ? "Every match needs a column from each side."
        : null;
    case "aggregate": {
      const aggs = p.aggs ?? [];
      if (!aggs.length) return "Add at least one summary.";
      for (const a of aggs) {
        if (!a.as) return "Give every summary a name.";
        if (a.fn !== "count_star" && !a.column) return "Pick a column to summarise.";
      }
      return null;
    }
    case "dedupe":
      if (!(p.keys ?? []).length) return "Pick the column(s) that identify a row.";
      if ((p.order_by ?? []).some((o: any) => !o.column))
        return "Pick which column decides who wins — “first” has no meaning without an order.";
      return null;
    case "sort":
      return (p.by ?? []).some((x: any) => !x.column) ? "Pick a column to sort by." : null;
  }
}

/** Every unfinished step in the flow, in spine order. */
export function flowIssues(flow: FlowDef): { node: FlowNode; issue: string }[] {
  return flow.nodes
    .map((n) => ({ node: n, issue: stepIssue(flow, n) }))
    .filter((x): x is { node: FlowNode; issue: string } => x.issue !== null);
}

// ------------------------------------------------------------------ refusals

/**
 * Turn a server refusal into something an analyst can act on.
 *
 * `FlowRefused`'s message is first-party and names the offending step — but it
 * names it by *id* (`step 's3'`), and a node id is an internal handle the
 * builder never otherwise shows. This rewrites each id into the step's position
 * and human label ("step 3, Filter rows") and reports which card to light up.
 */
export function readRefusal(
  flow: FlowDef,
  detail: string,
): { message: string; node: string | null } {
  const order = spine(flow, flow.terminal);
  const position = new Map(order.map((n, i) => [n.id, i + 1]));
  let hit: string | null = null;
  let message = detail;

  for (const n of flow.nodes) {
    // Only the `step '<id>'` form, so a *column* that happens to be spelled
    // like a step id is never rewritten out of the author's own message.
    const re = new RegExp(`\\b[Ss]tep '${n.id}'`, "g");
    if (!re.test(message)) continue;
    hit = hit ?? n.id;
    const where = position.has(n.id) ? `Step ${position.get(n.id)}` : "The step";
    message = message.replace(re, `${where} (${KINDS[n.kind].label})`);
  }
  // The server names a *remedy* by node kind — "add a 'rename' step" — and
  // "rename" is the compiler's word, not the menu's. Rewrite those too, so the
  // fix the message prescribes is spelled the way the button that performs it
  // is spelled.
  for (const [kind, meta] of Object.entries(KINDS)) {
    message = message.replace(
      new RegExp(`'${kind}' step`, "g"),
      `\u201c${meta.action}\u201d step`,
    );
  }
  return { message, node: hit };
}
