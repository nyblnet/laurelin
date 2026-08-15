// The right-hand pane: one form per step kind.
//
// Every control here is a `<select>` over a closed vocabulary, a column picker
// fed by the live schema, or a typed value input. Nothing takes free text that
// becomes SQL. The two text inputs that exist — a new column's name and a
// rename's target — are *invented identifiers*, which the server restricts to
// letters, digits, underscore and space precisely so they can be typed safely.

import { useState } from "react";
import type {
  Dataset,
  FlowAggFn,
  FlowCastType,
  FlowDef,
  FlowKind,
  FlowNode,
  FlowSortDir,
} from "../../types";
import { ConditionEditor, ValueEditor, type TypeHints } from "./ExprEditor";
import {
  AGG_FNS,
  AGG_FN_ORDER,
  CAST_TYPES,
  CAST_TYPE_ORDER,
  JOIN_HOWS,
  KINDS,
  NULLS,
  SORT_DIRS,
} from "./vocab";
import { nodeById } from "./model";

export interface StepFormProps {
  flow: FlowDef;
  node: FlowNode;
  /** Columns available to this step — its INPUT's schema, not its output. */
  schema: string[];
  /** For a `join`, the right-hand chain's schema. */
  rightSchema: string[] | null;
  hints: TypeHints;
  /** Column -> what it holds, from the server. Narrows the "Total of" /
   *  "Average of" pickers to columns those summaries can actually work on. */
  kinds: Record<string, FlowKind>;
  datasets: Dataset[];
  onParams: (params: Record<string, any>) => void;
  onRemove: () => void;
}

function ColumnSelect({
  value,
  schema,
  onChange,
  placeholder = "Pick a column…",
  note,
}: {
  value: string;
  schema: string[];
  onChange: (v: string) => void;
  placeholder?: string;
  /** Shown under the picker when the list has been narrowed, so a column
   *  missing from it reads as an explanation rather than as a bug. */
  note?: string;
}) {
  const missing = !!value && !schema.includes(value);
  return (
    <>
      <select
        className={`fx-in${missing ? " fx-missing" : ""}`}
        value={value ? `col:${value}` : ""}
        onChange={(e) => onChange(e.target.value ? e.target.value.slice(4) : "")}
      >
        <option value="">{placeholder}</option>
        {schema.map((c) => (
          <option key={c} value={`col:${c}`}>
            {c}
          </option>
        ))}
        {missing && <option value={`col:${value}`}>{value} — not in this data</option>}
      </select>
      {note && <span className="faint fx-kw">{note}</span>}
    </>
  );
}

/** A multi-select over the schema, rendered as checkboxes: a native
 *  <select multiple> is a well-known usability trap (ctrl-click to keep a
 *  selection) and this list is the core gesture of "Choose columns". */
function ColumnChecklist({
  selected,
  schema,
  onChange,
}: {
  selected: string[];
  schema: string[];
  onChange: (v: string[]) => void;
}) {
  const set = new Set(selected);
  return (
    <div className="fx-checklist">
      {schema.length === 0 && <div className="faint">No columns yet — finish the steps above.</div>}
      {schema.map((c) => (
        <label key={c} className="check-inline">
          <input
            type="checkbox"
            checked={set.has(c)}
            onChange={(e) =>
              onChange(
                e.target.checked
                  ? [...selected.filter((x) => x !== c), c]
                  : selected.filter((x) => x !== c),
              )
            }
          />
          <span>{c}</span>
        </label>
      ))}
      {selected.filter((c) => !schema.includes(c)).map((c) => (
        <label key={c} className="check-inline fx-missing-row">
          <input type="checkbox" checked onChange={() => onChange(selected.filter((x) => x !== c))} />
          <span>{c} — not in this data</span>
        </label>
      ))}
    </div>
  );
}

/** Summaries that need a column of numbers. `min`/`max`/`any_value` and the
 *  three counts work on anything, so narrowing their picker would refuse
 *  something the engine happily runs. */
const NUMERIC_AGG_FNS = new Set(["sum", "avg"]);

export function StepForm(props: StepFormProps) {
  const { flow, node, schema, rightSchema, hints, kinds, datasets, onParams, onRemove } = props;
  const p = node.params;
  const set = (patch: Record<string, any>) => onParams({ ...p, ...patch });
  // Unknown kind ⇒ still offered. The server has no opinion about that column
  // either, so it will not refuse it, and hiding it would be the UI inventing
  // a rule the compiler does not have.
  const numericColumns = schema.filter((c) => !kinds[c] || kinds[c] === "number");

  return (
    <div className="fx-form">
      <div className="fx-form-head">
        <div className="fx-form-title">{KINDS[node.kind].label}</div>
        {/* No Remove on a step with no input: that is where a chain starts, and
            removing it would leave whatever consumes it pointing at nothing.
            The affordance is simply absent rather than present-and-refused. */}
        {node.inputs.length > 0 && (
          <button type="button" className="fx-x" title="Remove this step" onClick={onRemove}>
            Remove
          </button>
        )}
      </div>
      <p className="fx-form-blurb">{KINDS[node.kind].blurb}</p>

      {node.kind === "source" && (
        <div className="field">
          <label>Dataset</label>
          <select
            className="fx-in fx-wide"
            value={p.dataset ?? ""}
            onChange={(e) => set({ dataset: e.target.value })}
          >
            <option value="">Pick a dataset…</option>
            {datasets.map((d) => (
              <option key={d.name} value={d.name}>
                {d.name}
              </option>
            ))}
            {p.dataset && !datasets.some((d) => d.name === p.dataset) && (
              <option value={p.dataset}>{p.dataset} — not available to you</option>
            )}
          </select>
          {/* The picker is fed by the datasets *this* account can view, so a
              source the author cannot read is not offered at all rather than
              refused after they pick it. */}
          <div className="hint">Only datasets you can read are listed.</div>
        </div>
      )}

      {node.kind === "filter" && (
        <div className="field">
          <label>Keep rows where…</label>
          <ConditionEditor
            expr={p.predicate}
            schema={schema}
            hints={hints}
            onChange={(predicate) => set({ predicate })}
          />
        </div>
      )}

      {node.kind === "select" && (
        <>
          <div className="field">
            <label>What to do with the columns you tick</label>
            <select className="fx-in fx-wide" value={p.mode} onChange={(e) => set({ mode: e.target.value })}>
              <option value="keep">Keep only these</option>
              <option value="drop">Drop these, keep the rest</option>
            </select>
          </div>
          <div className="field">
            <label>Columns</label>
            <ColumnChecklist
              selected={p.columns ?? []}
              schema={schema}
              onChange={(columns) => set({ columns })}
            />
          </div>
        </>
      )}

      {node.kind === "rename" && (
        <div className="field">
          <label>Renames</label>
          {(p.pairs ?? []).map((pair: any, i: number) => (
            <div key={i} className="fx-row">
              <ColumnSelect
                value={pair.from ?? ""}
                schema={schema}
                onChange={(from) => set({ pairs: p.pairs.map((x: any, j: number) => (j === i ? { ...x, from } : x)) })}
              />
              <span className="fx-kw">→</span>
              <input
                className="fx-in"
                type="text"
                placeholder="new name"
                value={pair.to ?? ""}
                onChange={(e) =>
                  set({ pairs: p.pairs.map((x: any, j: number) => (j === i ? { ...x, to: e.target.value } : x)) })
                }
              />
              {p.pairs.length > 1 && (
                <button
                  type="button"
                  className="fx-x"
                  onClick={() => set({ pairs: p.pairs.filter((_: any, j: number) => j !== i) })}
                >
                  ×
                </button>
              )}
            </div>
          ))}
          <button type="button" className="fx-add" onClick={() => set({ pairs: [...(p.pairs ?? []), { from: "", to: "" }] })}>
            + rename another
          </button>
          <div className="hint">
            New names may use letters, digits, underscores and spaces.
          </div>
        </div>
      )}

      {node.kind === "derive" && (
        <>
          <div className="field">
            <label>Name of the new column</label>
            <input
              className="fx-in fx-wide"
              type="text"
              placeholder="e.g. days late"
              value={p.name ?? ""}
              onChange={(e) => set({ name: e.target.value })}
            />
          </div>
          <div className="field">
            <label>Worked out from</label>
            <ValueEditor expr={p.expr} schema={schema} hints={hints} onChange={(expr) => set({ expr })} />
          </div>
        </>
      )}

      {node.kind === "cast" && (
        <>
          <div className="field">
            <label>Column</label>
            <ColumnSelect value={p.column ?? ""} schema={schema} onChange={(column) => set({ column })} />
          </div>
          <div className="field">
            <label>Read it as</label>
            <select className="fx-in fx-wide" value={p.to} onChange={(e) => set({ to: e.target.value as FlowCastType })}>
              {CAST_TYPE_ORDER.map((t) => (
                <option key={t} value={t}>
                  {CAST_TYPES[t]}
                </option>
              ))}
            </select>
          </div>
        </>
      )}

      {node.kind === "join" && (
        <JoinForm
          flow={flow}
          node={node}
          schema={schema}
          rightSchema={rightSchema ?? []}
          onParams={onParams}
        />
      )}

      {node.kind === "aggregate" && (
        <>
          <div className="field">
            <label>One row per… (leave all unticked for a single total row)</label>
            <ColumnChecklist
              selected={p.group_by ?? []}
              schema={schema}
              onChange={(group_by) => set({ group_by })}
            />
          </div>
          <div className="field">
            <label>Work out</label>
            {(p.aggs ?? []).map((a: any, i: number) => (
              <div key={i} className="fx-aggrow">
                <select
                  className="fx-in"
                  value={a.fn}
                  onChange={(e) => {
                    const fn = e.target.value as FlowAggFn;
                    set({
                      aggs: p.aggs.map((x: any, j: number) =>
                        j === i
                          ? { ...x, fn, column: fn === "count_star" ? null : x.column || "" }
                          : x,
                      ),
                    });
                  }}
                >
                  {AGG_FN_ORDER.map((fn) => (
                    <option key={fn} value={fn}>
                      {AGG_FNS[fn]}
                    </option>
                  ))}
                </select>
                {a.fn !== "count_star" && (
                  <>
                    <span className="fx-kw">of</span>
                    {/* Total and Average offer only columns that hold numbers.
                        The form used to offer every column regardless of type,
                        and "sum" is the seeded default for a new summary — so
                        "Total of" + a column of names was one dropdown away,
                        the flow saved cleanly, and the only feedback anywhere
                        was a preview error naming nothing and a build failure
                        saying "Laurelin's own code raised". The kinds needed to
                        grey it out were already being fetched for the value
                        boxes; this branch simply had not been given them. */}
                    <ColumnSelect
                      value={a.column ?? ""}
                      schema={NUMERIC_AGG_FNS.has(a.fn) ? numericColumns : schema}
                      note={
                        NUMERIC_AGG_FNS.has(a.fn) && numericColumns.length < schema.length
                          ? "number columns only"
                          : undefined
                      }
                      onChange={(column) =>
                        set({ aggs: p.aggs.map((x: any, j: number) => (j === i ? { ...x, column } : x)) })
                      }
                    />
                  </>
                )}
                <span className="fx-kw">called</span>
                <input
                  className="fx-in"
                  type="text"
                  placeholder="name"
                  value={a.as ?? ""}
                  onChange={(e) =>
                    set({ aggs: p.aggs.map((x: any, j: number) => (j === i ? { ...x, as: e.target.value } : x)) })
                  }
                />
                {p.aggs.length > 1 && (
                  <button
                    type="button"
                    className="fx-x"
                    onClick={() => set({ aggs: p.aggs.filter((_: any, j: number) => j !== i) })}
                  >
                    ×
                  </button>
                )}
              </div>
            ))}
            <button
              type="button"
              className="fx-add"
              onClick={() => set({ aggs: [...(p.aggs ?? []), { fn: "sum", column: "", as: "" }] })}
            >
              + another summary
            </button>
          </div>
          <div className="hint">
            The result has exactly these columns: the ones you grouped by, plus
            each summary. Everything else is gone — that is what summarising
            means.
          </div>
        </>
      )}

      {node.kind === "dedupe" && (
        <>
          <div className="field">
            <label>A row is a duplicate when these match</label>
            <ColumnChecklist selected={p.keys ?? []} schema={schema} onChange={(keys) => set({ keys })} />
          </div>
          <div className="field">
            <label>Keep which one</label>
            <div className="fx-row">
              <select className="fx-in" value={p.keep} onChange={(e) => set({ keep: e.target.value })}>
                <option value="first">The first</option>
                <option value="last">The last</option>
              </select>
              <span className="fx-kw">by</span>
              <ColumnSelect
                value={(p.order_by ?? [])[0]?.column ?? ""}
                schema={schema}
                onChange={(column) =>
                  set({ order_by: [{ ...(p.order_by?.[0] ?? { dir: "desc" }), column }] })
                }
              />
              <select
                className="fx-in"
                value={(p.order_by ?? [])[0]?.dir ?? "desc"}
                onChange={(e) =>
                  set({ order_by: [{ ...(p.order_by?.[0] ?? { column: "" }), dir: e.target.value as FlowSortDir }] })
                }
              >
                <option value="desc">largest first</option>
                <option value="asc">smallest first</option>
              </select>
            </div>
            {/* The ordering column is mandatory on the server, and this is why:
                "the first" with nothing deciding the order is a coin flip that
                stays stable until the data is compacted underneath it. */}
            <div className="hint">
              An order is required. “The first” means nothing until you say
              first by <em>what</em> — otherwise the winner can change on its
              own the next time the data is rewritten.
            </div>
          </div>
        </>
      )}

      {node.kind === "sort" && (
        <div className="field">
          <label>Sort by</label>
          {(p.by ?? []).map((entry: any, i: number) => (
            <div key={i} className="fx-row">
              <ColumnSelect
                value={entry.column ?? ""}
                schema={schema}
                onChange={(column) => set({ by: p.by.map((x: any, j: number) => (j === i ? { ...x, column } : x)) })}
              />
              <select
                className="fx-in"
                value={entry.dir}
                onChange={(e) => set({ by: p.by.map((x: any, j: number) => (j === i ? { ...x, dir: e.target.value } : x)) })}
              >
                {Object.entries(SORT_DIRS).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </select>
              <select
                className="fx-in"
                value={entry.nulls ?? "last"}
                onChange={(e) => set({ by: p.by.map((x: any, j: number) => (j === i ? { ...x, nulls: e.target.value } : x)) })}
              >
                {Object.entries(NULLS).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </select>
              {p.by.length > 1 && (
                <button type="button" className="fx-x" onClick={() => set({ by: p.by.filter((_: any, j: number) => j !== i) })}>
                  ×
                </button>
              )}
            </div>
          ))}
          <button
            type="button"
            className="fx-add"
            onClick={() => set({ by: [...(p.by ?? []), { column: "", dir: "asc", nulls: "last" }] })}
          >
            + then by
          </button>
        </div>
      )}
    </div>
  );
}

function JoinForm({
  flow,
  node,
  schema,
  rightSchema,
  onParams,
}: {
  flow: FlowDef;
  node: FlowNode;
  schema: string[];
  rightSchema: string[];
  onParams: (p: Record<string, any>) => void;
}) {
  const p = node.params;
  const right = nodeById(flow, node.inputs[1]);
  const rightName = right?.kind === "source" ? right.params.dataset : "";
  const set = (patch: Record<string, any>) => onParams({ ...p, ...patch });

  return (
    <>
      <div className="field">
        <label>Which rows to keep</label>
        <select className="fx-in fx-wide" value={p.how} onChange={(e) => set({ how: e.target.value })}>
          {Object.entries(JOIN_HOWS).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label>Rows match when</label>
        {(p.keys ?? []).map((k: any, i: number) => (
          <div key={i} className="fx-row">
            <ColumnSelect
              value={k.left ?? ""}
              schema={schema}
              onChange={(left) => set({ keys: p.keys.map((x: any, j: number) => (j === i ? { ...x, left } : x)) })}
              placeholder="this side…"
            />
            <span className="fx-kw">=</span>
            <ColumnSelect
              value={k.right ?? ""}
              schema={rightSchema}
              onChange={(right2) => set({ keys: p.keys.map((x: any, j: number) => (j === i ? { ...x, right: right2 } : x)) })}
              placeholder={rightName ? `${rightName}…` : "the other side…"}
            />
            {p.keys.length > 1 && (
              <button type="button" className="fx-x" onClick={() => set({ keys: p.keys.filter((_: any, j: number) => j !== i) })}>
                ×
              </button>
            )}
          </div>
        ))}
        <button type="button" className="fx-add" onClick={() => set({ keys: [...(p.keys ?? []), { left: "", right: "" }] })}>
          + and also match on
        </button>
        <div className="hint">
          The matched columns from {rightName || "the other dataset"} are not
          repeated in the result. If both sides have another column with the
          same name, add a <strong>Rename columns</strong> step to one side
          first.
        </div>
      </div>
    </>
  );
}

/** Data expectations: a closed set of four, authored as dropdowns.
 *
 * `expectations.expression()` — a raw SQL predicate — is deliberately NOT
 * reachable from here at any level. It stays available to Python authors. */
export function ExpectationsForm({
  flow,
  outputSchema,
  onChange,
}: {
  flow: FlowDef;
  outputSchema: string[];
  onChange: (e: FlowDef["expectations"]) => void;
}) {
  const [open, setOpen] = useState(false);
  const list = flow.expectations ?? [];

  return (
    <div className="fx-expect">
      <button type="button" className="fx-disclose" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} Checks before publishing ({list.length})
      </button>
      {open && (
        <div className="fx-expect-body">
          <p className="faint">
            If a check fails the build stops and the dataset is not replaced, so
            nothing downstream sees the bad version.
          </p>
          {list.map((x, i) => (
            <div key={i} className="fx-row">
              <select
                className="fx-in"
                value={x.kind}
                onChange={(e) => {
                  const kind = e.target.value as typeof x.kind;
                  const next = { ...x, kind } as typeof x;
                  if (kind === "row_count_between") {
                    delete next.column;
                    delete next.values;
                    next.min = next.min ?? 1;
                  } else {
                    next.column = next.column ?? "";
                    if (kind !== "accepted_values") delete next.values;
                    else next.values = next.values ?? [""];
                  }
                  onChange(list.map((y, j) => (j === i ? next : y)));
                }}
              >
                <option value="not_null">Every row has a value in</option>
                <option value="unique">No two rows share a value in</option>
                <option value="accepted_values">Only these values appear in</option>
                <option value="row_count_between">The row count is between</option>
              </select>

              {x.kind === "row_count_between" ? (
                <>
                  <input
                    className="fx-in fx-narrow"
                    type="number"
                    value={x.min ?? ""}
                    placeholder="min"
                    onChange={(e) =>
                      onChange(list.map((y, j) => (j === i ? { ...y, min: e.target.value === "" ? null : Math.trunc(Number(e.target.value)) } : y)))
                    }
                  />
                  <span className="fx-kw">and</span>
                  <input
                    className="fx-in fx-narrow"
                    type="number"
                    value={x.max ?? ""}
                    placeholder="max"
                    onChange={(e) =>
                      onChange(list.map((y, j) => (j === i ? { ...y, max: e.target.value === "" ? null : Math.trunc(Number(e.target.value)) } : y)))
                    }
                  />
                </>
              ) : (
                <ColumnSelect
                  value={x.column ?? ""}
                  schema={outputSchema}
                  onChange={(column) => onChange(list.map((y, j) => (j === i ? { ...y, column } : y)))}
                />
              )}

              {x.kind === "accepted_values" && (
                <input
                  className="fx-in"
                  type="text"
                  placeholder="value, value, value"
                  value={(x.values ?? []).join(", ")}
                  onChange={(e) =>
                    onChange(
                      list.map((y, j) =>
                        j === i
                          ? { ...y, values: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) }
                          : y,
                      ),
                    )
                  }
                />
              )}

              <button type="button" className="fx-x" onClick={() => onChange(list.filter((_, j) => j !== i))}>
                ×
              </button>
            </div>
          ))}
          <button
            type="button"
            className="fx-add"
            onClick={() => onChange([...list, { kind: "not_null", column: "", severity: "error" }])}
          >
            + add a check
          </button>
        </div>
      )}
    </div>
  );
}
