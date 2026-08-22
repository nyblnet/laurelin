// The expression editor: conditions and calculations, built entirely from
// dropdowns and typed inputs.
//
// There is no text area here, and there is no code path that turns a keystroke
// into SQL text. A column is chosen from the live schema; an operation is
// chosen from a closed list; a value goes into a typed input and travels to the
// server as JSON, where the compiler BINDS it as a query parameter. That is the
// security premise of the whole feature, and it is only true if this file never
// grows a "raw expression" escape hatch.

import type { FlowExpr, FlowLitType, FlowOp } from "../../types";
import {
  COMBINE_OPS,
  CONDITION_OPS,
  DATE_UNITS,
  DATE_UNIT_ORDER,
  LIT_TYPES,
  LIT_TYPE_ORDER,
  OPS,
  VALUE_OPS,
} from "./vocab";

/** A guessed literal type per column, from the preview's own values. Only ever
 *  used to pick a sensible *default* for a new value input — the author can
 *  always change it, and the server coerces strictly either way. */
export type TypeHints = Record<string, FlowLitType>;

const COL = (name = ""): FlowExpr => ({ t: "col", name });
const LIT = (type: FlowLitType = "string", value: unknown = ""): FlowExpr => ({
  t: "lit",
  type,
  value,
});

/** A blank right-hand side appropriate to the operator and the left column. */
function defaultRhs(op: FlowOp, left: FlowExpr, hints: TypeHints): FlowExpr {
  if (op === "like") return LIT("string", "");
  const hinted = left.t === "col" ? hints[left.name] : undefined;
  const type: FlowLitType = hinted && hinted !== "null" ? hinted : "string";
  return LIT(type, type === "boolean" ? true : type === "bigint" || type === "double" ? 0 : "");
}

function isCondition(e: FlowExpr | undefined): boolean {
  return !!e && e.t === "op" && (CONDITION_OPS as string[]).concat(COMBINE_OPS).includes(e.op);
}

// ------------------------------------------------------------------ literals

function LiteralInput({
  expr,
  onChange,
  placeholder,
}: {
  expr: Extract<FlowExpr, { t: "lit" }>;
  onChange: (e: FlowExpr) => void;
  placeholder?: string;
}) {
  const set = (value: unknown) => onChange({ ...expr, value });

  if (expr.type === "null") return <span className="fx-null">empty</span>;
  if (expr.type === "boolean") {
    return (
      <select
        className="fx-in"
        value={expr.value ? "true" : "false"}
        onChange={(e) => set(e.target.value === "true")}
      >
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    );
  }
  if (expr.type === "bigint" || expr.type === "double") {
    return (
      <input
        className="fx-in"
        type="number"
        step={expr.type === "bigint" ? 1 : "any"}
        value={expr.value === null || expr.value === undefined ? "" : String(expr.value)}
        placeholder={placeholder ?? "0"}
        onChange={(e) => {
          const raw = e.target.value;
          if (raw === "") return set("");
          const n = Number(raw);
          // bigint is a *whole* number on the server: `isinstance(v, int)` and
          // a bool is not an int there either. Rounding here rather than
          // sending 1.5 means the author sees what will be used.
          set(Number.isFinite(n) ? (expr.type === "bigint" ? Math.trunc(n) : n) : "");
        }}
      />
    );
  }
  if (expr.type === "date" || expr.type === "timestamp") {
    return (
      <input
        className="fx-in"
        type={expr.type === "date" ? "date" : "datetime-local"}
        value={String(expr.value ?? "")}
        onChange={(e) => set(e.target.value)}
      />
    );
  }
  return (
    <input
      className="fx-in"
      type="text"
      value={String(expr.value ?? "")}
      placeholder={placeholder ?? "value"}
      onChange={(e) => set(e.target.value)}
    />
  );
}

function TypePicker({
  value,
  onChange,
}: {
  value: FlowLitType;
  onChange: (t: FlowLitType) => void;
}) {
  return (
    <select
      className="fx-in fx-type"
      value={value}
      title="How this value should be read"
      onChange={(e) => onChange(e.target.value as FlowLitType)}
    >
      {LIT_TYPE_ORDER.map((t) => (
        <option key={t} value={t}>
          {LIT_TYPES[t]}
        </option>
      ))}
    </select>
  );
}

/** Retype a literal, converting the value so the input doesn't go blank. */
function retype(expr: Extract<FlowExpr, { t: "lit" }>, type: FlowLitType): FlowExpr {
  if (type === "boolean") return LIT(type, expr.value === true || expr.value === "true");
  if (type === "bigint" || type === "double") {
    const n = Number(expr.value);
    return LIT(type, Number.isFinite(n) ? (type === "bigint" ? Math.trunc(n) : n) : 0);
  }
  return LIT(type, expr.value === null || expr.value === undefined ? "" : String(expr.value));
}

// ------------------------------------------------------------------ operands

// Sentinels for the operand dropdown's non-column entries. Prefixed, and
// columns are prefixed too, so a dataset with a column genuinely called
// "calculation" cannot be shadowed by a menu item.
const M_VALUE = "mode:value";
const M_CALC = "mode:calc";

/**
 * One operand: a column, a fixed value, or (optionally) a calculation.
 *
 * Rendered as a single dropdown in the common case. The column list *is* the
 * live schema; there is no way to type a column name that is not in it, which
 * is what makes the identifier position safe without a pattern match.
 */
function Operand({
  expr,
  schema,
  hints,
  onChange,
  mode,
}: {
  expr: FlowExpr;
  schema: string[];
  hints: TypeHints;
  onChange: (e: FlowExpr) => void;
  /** "column" offers columns first (a filter's left side); "value" offers a
   *  fixed value first (a filter's right side). */
  mode: "column" | "value";
}) {
  if (expr.t === "col") {
    return (
      <select
        className={`fx-in fx-col${expr.name && !schema.includes(expr.name) ? " fx-missing" : ""}`}
        value={expr.name ? `col:${expr.name}` : ""}
        onChange={(e) => {
          const v = e.target.value;
          if (v === M_CALC) return onChange({ t: "op", op: "concat", args: [COL(expr.name), LIT("string", "")] });
          if (v === M_VALUE) return onChange(LIT("string", ""));
          onChange(COL(v.slice(4)));
        }}
      >
        <option value="">Pick a column…</option>
        {schema.map((c) => (
          <option key={c} value={`col:${c}`}>
            {c}
          </option>
        ))}
        {expr.name && !schema.includes(expr.name) && (
          // A column this data no longer has. Kept in the list rather than
          // silently reset to blank: the author needs to see what broke, and
          // the server refuses it by name on save.
          <option value={`col:${expr.name}`}>{expr.name} — not in this data</option>
        )}
        <option disabled>──────────</option>
        <option value={M_VALUE}>a fixed value…</option>
        <option value={M_CALC}>a calculation…</option>
      </select>
    );
  }

  if (expr.t === "lit") {
    return (
      <span className="fx-operand">
        <LiteralInput expr={expr} onChange={onChange} />
        <TypePicker value={expr.type} onChange={(t) => onChange(retype(expr, t))} />
        <button
          type="button"
          className="fx-swap"
          title="Use a column instead"
          onClick={() => onChange(COL(""))}
        >
          column
        </button>
        <button
          type="button"
          className="fx-swap"
          title="Use a calculation instead"
          onClick={() => onChange({ t: "op", op: "concat", args: [COL(""), LIT("string", "")] })}
        >
          calc
        </button>
      </span>
    );
  }

  // A calculation. Nested, with its own operator dropdown.
  return (
    <span className="fx-operand fx-calc">
      <CalcEditor expr={expr} schema={schema} hints={hints} onChange={onChange} />
      <button
        type="button"
        className="fx-swap"
        title={mode === "column" ? "Use a plain column instead" : "Use a plain value instead"}
        onClick={() => onChange(mode === "column" ? COL("") : LIT("string", ""))}
      >
        ×
      </button>
    </span>
  );
}

/** A value-producing operation and its operands. */
function CalcEditor({
  expr,
  schema,
  hints,
  onChange,
}: {
  expr: Extract<FlowExpr, { t: "op" }>;
  schema: string[];
  hints: TypeHints;
  onChange: (e: FlowExpr) => void;
}) {
  const meta = OPS[expr.op];
  const setOp = (op: FlowOp) => {
    const [lo, hi] = ARITY[op];
    let args = [...expr.args];
    if (op === "date_trunc") {
      args = [LIT("string", "month"), args.find((a) => a.t !== "lit") ?? COL("")];
    } else if (op === "if_else") {
      args = [
        isCondition(args[0]) ? args[0] : { t: "op", op: "eq", args: [COL(""), LIT("string", "")] },
        args[1] ?? LIT("string", ""),
        args[2] ?? LIT("string", ""),
      ];
    } else {
      while (args.length < lo) args.push(LIT("string", ""));
      if (hi !== null && args.length > hi) args = args.slice(0, hi);
    }
    onChange({ t: "op", op, args });
  };
  const setArg = (i: number, v: FlowExpr) =>
    onChange({ ...expr, args: expr.args.map((a, j) => (j === i ? v : a)) });

  const opSelect = (
    <select className="fx-in fx-op" value={expr.op} onChange={(e) => setOp(e.target.value as FlowOp)}>
      {VALUE_OPS.map((o) => (
        <option key={o} value={o}>
          {OPS[o].label}
        </option>
      ))}
    </select>
  );

  if (expr.op === "date_trunc") {
    const unit = expr.args[0];
    return (
      <span className="fx-row-inline">
        <Operand expr={expr.args[1] ?? COL("")} schema={schema} hints={hints} mode="column" onChange={(v) => setArg(1, v)} />
        {opSelect}
        <select
          className="fx-in"
          value={unit && unit.t === "lit" ? String(unit.value) : "month"}
          onChange={(e) => setArg(0, LIT("string", e.target.value))}
        >
          {DATE_UNIT_ORDER.map((u) => (
            <option key={u} value={u}>
              {DATE_UNITS[u]}
            </option>
          ))}
        </select>
      </span>
    );
  }

  if (expr.op === "if_else") {
    return (
      <span className="fx-ifelse">
        {opSelect}
        <span className="fx-ifrow">
          <span className="fx-kw">if</span>
          <ConditionEditor
            expr={expr.args[0]}
            schema={schema}
            hints={hints}
            onChange={(v) => setArg(0, v)}
          />
        </span>
        <span className="fx-ifrow">
          <span className="fx-kw">then</span>
          <Operand expr={expr.args[1] ?? LIT("string", "")} schema={schema} hints={hints} mode="value" onChange={(v) => setArg(1, v)} />
        </span>
        <span className="fx-ifrow">
          <span className="fx-kw">otherwise</span>
          <Operand expr={expr.args[2] ?? LIT("string", "")} schema={schema} hints={hints} mode="value" onChange={(v) => setArg(2, v)} />
        </span>
      </span>
    );
  }

  if (meta.form === "call") {
    return (
      <span className="fx-row-inline">
        <Operand expr={expr.args[0] ?? COL("")} schema={schema} hints={hints} mode="column" onChange={(v) => setArg(0, v)} />
        {opSelect}
        {expr.op === "round" && (
          <>
            <span className="fx-kw">to</span>
            <input
              className="fx-in fx-narrow"
              type="number"
              min={0}
              value={expr.args[1] && expr.args[1].t === "lit" ? String(expr.args[1].value) : ""}
              placeholder="0"
              onChange={(e) => {
                const raw = e.target.value;
                const next = [...expr.args];
                if (raw === "") next.length = 1;
                else next[1] = LIT("bigint", Math.trunc(Number(raw) || 0));
                onChange({ ...expr, args: next });
              }}
            />
            <span className="fx-kw">decimal places</span>
          </>
        )}
      </span>
    );
  }

  // `coalesce` reads as a prefix ("first value that isn't empty: a, b, c");
  // every other n-ary op reads infix ("a plus b"). Rendering both the same way
  // makes one of them nonsense, so they are two layouts.
  const [, hi] = ARITY[expr.op];
  const prefix = expr.op === "coalesce";
  return (
    <span className="fx-row-inline">
      {prefix && opSelect}
      {expr.args.map((a, i) => (
        <span key={i} className="fx-row-inline">
          {!prefix && i === 1 && opSelect}
          {!prefix && i > 1 && <span className="fx-kw">{OPS[expr.op].label}</span>}
          {prefix && i > 0 && <span className="fx-kw">,</span>}
          <Operand
            expr={a}
            schema={schema}
            hints={hints}
            mode={i === 0 ? "column" : "value"}
            onChange={(v) => setArg(i, v)}
          />
        </span>
      ))}
      {hi === null && (
        <button
          type="button"
          className="fx-add"
          onClick={() => onChange({ ...expr, args: [...expr.args, LIT("string", "")] })}
        >
          + another
        </button>
      )}
      {expr.args.length > ARITY[expr.op][0] && (
        <button
          type="button"
          className="fx-x"
          title="Remove the last part"
          onClick={() => onChange({ ...expr, args: expr.args.slice(0, -1) })}
        >
          ×
        </button>
      )}
    </span>
  );
}

/** Mirrors `flow_ir.OPS`: (min, max|null). Arity is enforced server-side; this
 *  is what stops the builder from ever *offering* a shape that would be. */
const ARITY: Record<FlowOp, [number, number | null]> = {
  and: [2, null], or: [2, null], not: [1, 1],
  eq: [2, 2], ne: [2, 2], lt: [2, 2], lte: [2, 2], gt: [2, 2], gte: [2, 2],
  is_null: [1, 1], is_not_null: [1, 1],
  in: [2, null], not_in: [2, null], like: [2, 2],
  add: [2, 2], sub: [2, 2], mul: [2, 2], div: [2, 2],
  if_else: [3, 3], coalesce: [2, null],
  upper: [1, 1], lower: [1, 1], trim: [1, 1], length: [1, 1], abs: [1, 1],
  round: [1, 2], floor: [1, 1], concat: [2, null], date_trunc: [2, 2],
};

// ------------------------------------------------------------------ conditions

/**
 * A condition — the thing a filter keeps rows by, and an `if_else` branches on.
 *
 * The root is always a boolean operation, because the server requires it: a
 * bare column in a WHERE clause is a type error surfaced by DuckDB, and an
 * author would see a binder message instead of a sentence.
 */
export function ConditionEditor({
  expr,
  schema,
  hints,
  onChange,
  onRemove,
  depth = 0,
}: {
  expr: FlowExpr | undefined;
  schema: string[];
  hints: TypeHints;
  onChange: (e: FlowExpr) => void;
  onRemove?: () => void;
  depth?: number;
}) {
  // A stored predicate that is a bare column or value is invalid — the server
  // refuses it — but it is also what a hand-edited flow file looks like, and
  // the builder's job is to let that be repaired. Keep whatever the author had
  // as the left-hand side rather than blanking it: a card that silently loses
  // the column it named is worse than one that shows an incomplete comparison.
  const e =
    expr && expr.t === "op"
      ? expr
      : ({
          t: "op",
          op: "eq",
          args: [expr ?? COL(""), LIT("string", "")],
        } as Extract<FlowExpr, { t: "op" }>);

  // --- a group: all of / any of --------------------------------------------
  if (e.op === "and" || e.op === "or") {
    return (
      <div className={`fx-group depth-${Math.min(depth, 3)}`}>
        <div className="fx-group-head">
          <select
            className="fx-in"
            value={e.op}
            onChange={(ev) => onChange({ ...e, op: ev.target.value as FlowOp })}
          >
            <option value="and">{OPS.and.label}</option>
            <option value="or">{OPS.or.label}</option>
          </select>
          {onRemove && (
            <button type="button" className="fx-x" title="Remove this group" onClick={onRemove}>
              ×
            </button>
          )}
        </div>
        <div className="fx-group-body">
          {e.args.map((a, i) => (
            <ConditionEditor
              key={i}
              expr={a}
              schema={schema}
              hints={hints}
              depth={depth + 1}
              onChange={(v) => onChange({ ...e, args: e.args.map((x, j) => (j === i ? v : x)) })}
              onRemove={
                e.args.length > 2
                  ? () => onChange({ ...e, args: e.args.filter((_, j) => j !== i) })
                  : undefined
              }
            />
          ))}
          <div className="fx-group-actions">
            <button
              type="button"
              className="fx-add"
              onClick={() =>
                onChange({ ...e, args: [...e.args, { t: "op", op: "eq", args: [COL(""), LIT("string", "")] }] })
              }
            >
              + condition
            </button>
            {depth < 3 && (
              <button
                type="button"
                className="fx-add"
                onClick={() =>
                  onChange({
                    ...e,
                    args: [
                      ...e.args,
                      {
                        t: "op",
                        op: e.op === "and" ? "or" : "and",
                        args: [
                          { t: "op", op: "eq", args: [COL(""), LIT("string", "")] },
                          { t: "op", op: "eq", args: [COL(""), LIT("string", "")] },
                        ],
                      },
                    ],
                  })
                }
              >
                + group
              </button>
            )}
          </div>
        </div>
      </div>
    );
  }

  if (e.op === "not") {
    return (
      <div className="fx-cond">
        <span className="fx-kw">not</span>
        <ConditionEditor
          expr={e.args[0]}
          schema={schema}
          hints={hints}
          depth={depth + 1}
          onChange={(v) => onChange({ ...e, args: [v] })}
        />
        <button type="button" className="fx-x" title="Drop the “not”" onClick={() => onChange(e.args[0])}>
          ×
        </button>
      </div>
    );
  }

  // --- a single comparison -------------------------------------------------
  const left = e.args[0] ?? COL("");
  const setOp = (op: FlowOp) => {
    if (op === "and" || op === "or") {
      onChange({ t: "op", op, args: [e, { t: "op", op: "eq", args: [COL(""), LIT("string", "")] }] });
      return;
    }
    if (op === "is_null" || op === "is_not_null") {
      onChange({ t: "op", op, args: [left] });
      return;
    }
    if (op === "in" || op === "not_in") {
      const existing = e.args.slice(1).filter((a) => a.t === "lit");
      onChange({ t: "op", op, args: [left, ...(existing.length ? existing : [defaultRhs(op, left, hints)])] });
      return;
    }
    const rhs = e.args[1] && e.args[1].t !== "col" ? e.args[1] : defaultRhs(op, left, hints);
    onChange({ t: "op", op, args: [left, op === "like" ? retypeToString(rhs) : rhs] });
  };

  return (
    <div className="fx-cond">
      <Operand
        expr={left}
        schema={schema}
        hints={hints}
        mode="column"
        onChange={(v) => {
          // Retype the value side to follow the column. Picking `delay_minutes`
          // and being handed a text box is the single most common way to
          // author a comparison that means something other than what it looks
          // like — and the type is knowable, so the author should not have to
          // know it. Only a still-blank literal is retyped: a value they have
          // already typed is theirs.
          const rest = e.args.slice(1);
          const hinted = v.t === "col" ? hints[v.name] : undefined;
          const retyped =
            hinted && hinted !== "null" && e.op !== "like"
              ? rest.map((a) =>
                  a.t === "lit" && (a.value === "" || a.value === null) ? retype(a, hinted) : a,
                )
              : rest;
          onChange({ ...e, args: [v, ...retyped] });
        }}
      />
      <select className="fx-in fx-op" value={e.op} onChange={(ev) => setOp(ev.target.value as FlowOp)}>
        {CONDITION_OPS.map((o) => (
          <option key={o} value={o}>
            {OPS[o].label}
          </option>
        ))}
        <option disabled>──────────</option>
        <option value="and">…and something else</option>
        <option value="or">…or something else</option>
      </select>

      {e.op === "in" || e.op === "not_in" ? (
        <span className="fx-list">
          {e.args.slice(1).map((a, i) => (
            <span key={i} className="fx-listitem">
              {a.t === "lit" ? (
                <LiteralInput expr={a} onChange={(v) => onChange({ ...e, args: e.args.map((x, j) => (j === i + 1 ? v : x)) })} />
              ) : null}
              {e.args.length > 2 && (
                <button
                  type="button"
                  className="fx-x"
                  onClick={() => onChange({ ...e, args: e.args.filter((_, j) => j !== i + 1) })}
                >
                  ×
                </button>
              )}
            </span>
          ))}
          <button
            type="button"
            className="fx-add"
            onClick={() => onChange({ ...e, args: [...e.args, defaultRhs(e.op, left, hints)] })}
          >
            + value
          </button>
          {e.op === "not_in" && <span className="fx-hint">empty values are kept</span>}
          {e.args[1] && e.args[1].t === "lit" && (
            <TypePicker
              value={e.args[1].type}
              onChange={(t) =>
                onChange({
                  ...e,
                  args: [left, ...e.args.slice(1).map((a) => (a.t === "lit" ? retype(a, t) : a))],
                })
              }
            />
          )}
        </span>
      ) : e.op === "is_null" || e.op === "is_not_null" ? null : (
        <>
          <Operand
            expr={e.args[1] ?? defaultRhs(e.op, left, hints)}
            schema={schema}
            hints={hints}
            mode="value"
            onChange={(v) => onChange({ ...e, args: [left, v] })}
          />
          {e.op === "like" && (
            <span className="fx-hint">
              <span className="mono">%</span> matches anything, <span className="mono">_</span> matches
              one character
            </span>
          )}
          {/* Said on screen, not just in the compiler. "is not" keeps rows whose
              value is empty — which is what the words mean and NOT what SQL's
              `<>` does. Measured: without this rendering, "v is not 5" over
              three empty rows and one 5 returned nothing, silently. */}
          {e.op === "ne" && <span className="fx-hint">empty values are kept</span>}
        </>
      )}

      {onRemove && (
        <button type="button" className="fx-x" title="Remove this condition" onClick={onRemove}>
          ×
        </button>
      )}
    </div>
  );
}

function retypeToString(e: FlowExpr): FlowExpr {
  if (e.t === "lit") return LIT("string", e.value === null || e.value === undefined ? "" : String(e.value));
  return LIT("string", "");
}

/** A value — what "Add a column" computes. */
export function ValueEditor({
  expr,
  schema,
  hints,
  onChange,
}: {
  expr: FlowExpr | undefined;
  schema: string[];
  hints: TypeHints;
  onChange: (e: FlowExpr) => void;
}) {
  return (
    <div className="fx-value">
      <Operand expr={expr ?? LIT("string", "")} schema={schema} hints={hints} mode="value" onChange={onChange} />
    </div>
  );
}
