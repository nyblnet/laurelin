// The words an analyst uses, mapped onto the words the engine uses.
//
// This file is the product. The compiler's vocabulary is `aggregate`,
// `count_distinct`, `varchar`, `NULLS LAST`; the audience for this screen is a
// person who does not write SQL and for whom every one of those is a small
// wall. Every closed enum in `laurelin/transforms/flow_ir.py` gets a label
// here, and nothing in the builder renders a raw enum key.
//
// The keys are the wire values and must stay byte-identical to the server's
// vocabularies — the server refuses anything outside them, so a typo here is a
// 400 the author cannot act on rather than a silent widening.

import type {
  FlowAggFn,
  FlowCastType,
  FlowLitType,
  FlowNodeKind,
  FlowOp,
} from "../../types";

export interface KindMeta {
  /** What the step *is*, in a card header. A noun phrase. */
  label: string;
  /** What adding it *does*, in the "add a step" menu. An imperative. */
  action: string;
  /** One line under the action in the menu, for someone who has never done it. */
  blurb: string;
}

// Ordered as the "add a step" menu offers them: the four an analyst reaches
// for first, then the reshaping ones, then the two that are really data
// hygiene. Not alphabetical, and not the order the IR declares them in.
export const KIND_ORDER: FlowNodeKind[] = [
  "filter",
  "aggregate",
  "join",
  "derive",
  "select",
  "rename",
  "sort",
  "dedupe",
  "cast",
];

export const KINDS: Record<FlowNodeKind, KindMeta> = {
  source: {
    label: "Start from a dataset",
    action: "Start from a dataset",
    blurb: "Every pipeline begins with data you can already read.",
  },
  filter: {
    label: "Filter rows",
    action: "Filter rows",
    blurb: "Keep only the rows that match a condition you set.",
  },
  select: {
    label: "Choose columns",
    action: "Choose columns",
    blurb: "Keep the columns you need, or drop the ones you don't.",
  },
  rename: {
    label: "Rename columns",
    action: "Rename columns",
    blurb: "Give a column a clearer name in the result.",
  },
  derive: {
    label: "Add a column",
    action: "Add a column",
    blurb: "Work out a new value from the columns you already have.",
  },
  cast: {
    label: "Change a column's type",
    action: "Change a column's type",
    blurb: "Read a column as text, a number, a date, or true/false.",
  },
  join: {
    label: "Combine with another dataset",
    action: "Combine with another dataset",
    blurb: "Match rows from a second dataset on a shared column.",
  },
  aggregate: {
    label: "Group and summarise",
    action: "Group and summarise",
    blurb: "One row per group, with totals, averages or counts.",
  },
  dedupe: {
    label: "Remove duplicates",
    action: "Remove duplicates",
    blurb: "Keep one row per key — the newest, or the oldest.",
  },
  sort: {
    label: "Sort rows",
    action: "Sort rows",
    blurb: "Put the result in a definite order.",
  },
};

// ------------------------------------------------------------------ operators
//
// Split into three menus rather than one list of 28. An author choosing a
// filter condition is asking "is this row's value …?"; an author building a
// value is asking "what do I compute?". Offering both in one dropdown makes
// each one twice as long and neither easier to read.

export interface OpMeta {
  label: string;
  /** Number of *editable* operands after the first, for the inline renderer. */
  form: "compare" | "unary" | "list" | "nary" | "if_else" | "call";
}

export const OPS: Record<FlowOp, OpMeta> = {
  eq: { label: "is", form: "compare" },
  // "is not" and "is not one of" KEEP empty values, which is not what SQL's
  // `<>` and `NOT IN` do. Measured over a column holding [null, null, null, 5],
  // "keep rows where v is not 5" returned 0 rows — correct SQL, and the exact
  // opposite of what this label says to somebody who does not write SQL, with
  // no error anywhere. The compiler renders these as `IS DISTINCT FROM` and
  // `NOT coalesce(… IN …, false)` so the operator matches its own words; the
  // hint under the picker says so.
  ne: { label: "is not", form: "compare" },
  lt: { label: "is less than", form: "compare" },
  lte: { label: "is at most", form: "compare" },
  gt: { label: "is greater than", form: "compare" },
  gte: { label: "is at least", form: "compare" },
  is_null: { label: "is empty", form: "unary" },
  is_not_null: { label: "is not empty", form: "unary" },
  in: { label: "is one of", form: "list" },
  not_in: { label: "is not one of", form: "list" },  // keeps empty values, as above
  like: { label: "matches the pattern", form: "compare" },
  and: { label: "all of these are true", form: "nary" },
  or: { label: "any of these is true", form: "nary" },
  not: { label: "this is not true", form: "unary" },
  add: { label: "plus", form: "compare" },
  sub: { label: "minus", form: "compare" },
  mul: { label: "times", form: "compare" },
  div: { label: "divided by", form: "compare" },
  concat: { label: "joined onto", form: "nary" },
  coalesce: { label: "first value that isn't empty", form: "nary" },
  if_else: { label: "if … then … otherwise", form: "if_else" },
  upper: { label: "in CAPITALS", form: "call" },
  lower: { label: "in lower case", form: "call" },
  trim: { label: "with spaces trimmed off", form: "call" },
  length: { label: "how many characters", form: "call" },
  abs: { label: "without the minus sign", form: "call" },
  round: { label: "rounded", form: "call" },
  floor: { label: "rounded down to a whole number", form: "call" },
  date_trunc: { label: "rounded down to a whole", form: "call" },
};

/** Conditions — anything whose answer is yes or no. A filter needs one of these. */
export const CONDITION_OPS: FlowOp[] = [
  "eq", "ne", "gt", "gte", "lt", "lte",
  "is_null", "is_not_null", "in", "not_in", "like",
];

/** Combinators, offered separately: they take conditions, not values. */
export const COMBINE_OPS: FlowOp[] = ["and", "or", "not"];

/** Value-producing operations, for "Add a column". */
export const VALUE_OPS: FlowOp[] = [
  "add", "sub", "mul", "div", "round", "floor", "abs",
  "concat", "upper", "lower", "trim", "length",
  "coalesce", "if_else", "date_trunc",
];

/** Ops the server accepts as a filter predicate root / an if_else condition. */
export const BOOLEAN_OPS: ReadonlySet<FlowOp> = new Set<FlowOp>([
  ...CONDITION_OPS,
  ...COMBINE_OPS,
]);

// ------------------------------------------------------------------ values

export const LIT_TYPES: Record<FlowLitType, string> = {
  string: "Text",
  bigint: "Whole number",
  double: "Decimal number",
  boolean: "True / false",
  date: "Date",
  timestamp: "Date and time",
  null: "Empty",
};

/** Offered in the value-type picker. `null` is reached by the "is empty"
 *  condition instead — a magic empty string in a text box is exactly the
 *  ambiguity this builder exists to remove. */
export const LIT_TYPE_ORDER: FlowLitType[] = [
  "string", "bigint", "double", "boolean", "date", "timestamp",
];

export const CAST_TYPES: Record<FlowCastType, string> = {
  varchar: "Text",
  bigint: "Whole number",
  double: "Decimal number",
  boolean: "True / false",
  date: "Date",
  timestamp: "Date and time",
};

export const CAST_TYPE_ORDER: FlowCastType[] = [
  "varchar", "bigint", "double", "boolean", "date", "timestamp",
];

export const AGG_FNS: Record<FlowAggFn, string> = {
  // `count(*)` and `count(c)` differ on NULLs, and an analyst who cannot see
  // that difference in the menu picks the wrong one. So they are two entries
  // with two sentences, not one entry with a footnote.
  count_star: "Number of rows",
  count: "Number of rows with a value",
  count_distinct: "Number of different values",
  sum: "Total",
  avg: "Average",
  // "Middle value", not "median": half the audience for this menu knows the
  // word, and the half that doesn't is exactly who the menu is for.
  median: "Middle value (median)",
  min: "Smallest",
  max: "Largest",
  any_value: "Any one value",
};

export const AGG_FN_ORDER: FlowAggFn[] = [
  "count_star", "sum", "avg", "median", "min", "max",
  "count", "count_distinct", "any_value",
];

export const JOIN_HOWS: Record<string, string> = {
  inner: "Only rows that match in both",
  left: "Every row on the left, matched where possible",
};

export const SORT_DIRS: Record<string, string> = {
  asc: "Smallest first (A→Z)",
  desc: "Largest first (Z→A)",
};

export const NULLS: Record<string, string> = {
  last: "Empty values last",
  first: "Empty values first",
};

export const DATE_UNITS: Record<string, string> = {
  year: "year",
  quarter: "quarter",
  month: "month",
  week: "week",
  day: "day",
  hour: "hour",
};

export const DATE_UNIT_ORDER = ["year", "quarter", "month", "week", "day", "hour"];

export const EXPECTATIONS: Record<string, { label: string; blurb: string }> = {
  not_null: {
    label: "Every row has a value in",
    blurb: "Stop the build if this column is ever empty.",
  },
  unique: {
    label: "No two rows share a value in",
    blurb: "Stop the build if this column repeats. Required if this dataset backs an object type.",
  },
  accepted_values: {
    label: "Only these values appear in",
    blurb: "Stop the build if anything else shows up.",
  },
  row_count_between: {
    label: "The result has between",
    blurb: "Stop the build if the row count leaves this range.",
  },
};
