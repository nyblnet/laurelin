// Renders the REAL charts.tsx and exercises the REAL explore model — both
// imported from the source tree, never copied — and prints one JSON object of
// results for tests/test_charts_render.py to assert against. Zero mocking of
// the renderer: every chart claim below is made about the exact SVG markup
// react-dom/server emits from the shipped component.
//
// Bundled at test time with the webapp's own esbuild (no vitest, no second
// test framework — see test_charts_render.py for why).
import { renderToStaticMarkup } from "react-dom/server";
import { Chart, inferBindings } from "../../laurelin/ui/webapp/src/charts";
import {
  emptyExplore,
  exploreFlow,
  exploreIssues,
  explainRefusal,
  parseDraft,
  serializeDraft,
  stateFromFlow,
  distinctValuesFlow,
} from "../../laurelin/ui/webapp/src/views/explore/model";

const out: Record<string, unknown> = {};

function render(el: JSX.Element): string {
  return renderToStaticMarkup(el);
}

// ---------------------------------------------------------------- charts

// A NULL mid-series must be a gap, not a zero.
out.line_null = render(
  <Chart
    kind="line"
    x="month"
    y={["revenue"]}
    data={{
      columns: ["month", "revenue"],
      rows: [
        { month: "Jan", revenue: 100 },
        { month: "Feb", revenue: 110 },
        { month: "Mar", revenue: null },
        { month: "Apr", revenue: 120 },
        { month: "May", revenue: 130 },
      ],
    }}
  />,
);

// Rows sorted by (series, x): the axis must keep each series' own order.
out.pivot_order = render(
  <Chart
    kind="line"
    x="month"
    y={["amount"]}
    series="region"
    data={{
      columns: ["month", "region", "amount"],
      rows: [
        { month: "Jan", region: "A", amount: 500 },
        { month: "Mar", region: "A", amount: 520 },
        { month: "Jan", region: "B", amount: 400 },
        { month: "Feb", region: "B", amount: 410 },
        { month: "Mar", region: "B", amount: 430 },
      ],
    }}
  />,
);

// A KPI of 0.004 must not read "0".
out.stat_small = render(
  <Chart kind="stat" y={["rate"]} data={{ columns: ["rate"], rows: [{ rate: 0.004 }] }} />,
);

// Sub-0.01 tick labels must be distinct and true.
out.ticks_small = render(
  <Chart
    kind="bar"
    x="k"
    y={["v"]}
    data={{
      columns: ["k", "v"],
      rows: [
        { k: "a", v: 0.001 },
        { k: "b", v: 0.002 },
        { k: "c", v: 0.004 },
      ],
    }}
  />,
);

// Numeric bins with a gap: the empty bins must occupy visible axis width.
out.histogram_gap = render(
  <Chart
    kind="bar"
    x="delay_range"
    y={["n"]}
    data={{
      columns: ["delay_range", "n"],
      rows: [
        { delay_range: 0, n: 40 },
        { delay_range: 30, n: 25 },
        { delay_range: 60, n: 10 },
        { delay_range: 300, n: 5 },
      ],
    }}
  />,
);

// A tight cluster far from zero must not collapse to a sub-pixel blob.
out.scatter_cluster = render(
  <Chart
    kind="scatter"
    x="year"
    y={["price"]}
    data={{
      columns: ["year", "price"],
      rows: [
        { year: 2020, price: 100200 },
        { year: 2021, price: 100400 },
        { year: 2022, price: 100600 },
        { year: 2023, price: 100800 },
        { year: 2024, price: 101000 },
      ],
    }}
  />,
);

// A NULL bar must not be drawn as a measured zero.
out.bar_null = render(
  <Chart
    kind="bar"
    x="dept"
    y={["headcount"]}
    data={{
      columns: ["dept", "headcount"],
      rows: [
        { dept: "eng", headcount: 40 },
        { dept: "ops", headcount: null },
        { dept: "sales", headcount: 25 },
      ],
    }}
  />,
);

// Binding inference must scan past a NULL in row 0.
out.infer_first_null = JSON.stringify(
  inferBindings(
    {
      columns: ["dept", "headcount"],
      rows: [
        { dept: "eng", headcount: null },
        { dept: "ops", headcount: 30 },
      ],
    },
    "",
    [],
  ),
);

// Scatter must count the rows it cannot draw.
out.scatter_skip = render(
  <Chart
    kind="scatter"
    x="age"
    y={["salary"]}
    data={{
      columns: ["age", "salary"],
      rows: [
        { age: 25, salary: 50000 },
        { age: 30, salary: null },
        { age: 35, salary: 70000 },
        { age: null, salary: 90000 },
        { age: 45, salary: 90000 },
      ],
    }}
  />,
);

// 8 series × 40 groups: bars must stay inside their group band.
out.grouped_band = render(
  <Chart
    kind="bar"
    x="day"
    y={["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"]}
    data={{
      columns: ["day", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"],
      rows: Array.from({ length: 40 }, (_, i) => ({
        day: `day_${i}`,
        s1: 1, s2: 2, s3: 3, s4: 4, s5: 5, s6: 6, s7: 7, s8: 8,
      })),
    }}
  />,
);

// 7 equal slices: the closing slice must not repeat the first slice's color.
out.pie_seven = render(
  <Chart
    kind="pie"
    x="cat"
    y={["n"]}
    data={{
      columns: ["cat", "n"],
      rows: [1, 2, 3, 4, 5, 6, 7].map((i) => ({ cat: `c${i}`, n: 10 })),
    }}
  />,
);

// A series value spelled like the x column must not clobber the axis.
out.series_collision = render(
  <Chart
    kind="bar"
    x="month"
    y={["value"]}
    series="metric"
    data={{
      columns: ["month", "metric", "value"],
      rows: [
        { month: "Jan", metric: "month", value: 7 },
        { month: "Jan", metric: "other", value: 3 },
        { month: "Feb", metric: "month", value: 9 },
        { month: "Feb", metric: "other", value: 4 },
      ],
    }}
  />,
);

// Truncated long labels must stay distinguishable.
out.trunc_labels = render(
  <Chart
    kind="bar"
    x="grp"
    y={["v"]}
    data={{
      columns: ["grp", "v"],
      rows: [
        { grp: "customer_group_alpha", v: 10 },
        { grp: "customer_group_beta", v: 90 },
      ],
    }}
  />,
);

// Billions need a B tier.
out.billions = render(
  <Chart
    kind="bar"
    x="k"
    y={["v"]}
    data={{
      columns: ["k", "v"],
      rows: [
        { k: "a", v: 2_100_000_000 },
        { k: "b", v: 3_900_000_000 },
      ],
    }}
  />,
);

// A month bucket comes back as a midnight timestamp; label it as its date.
out.midnight_labels = render(
  <Chart
    kind="bar"
    x="month"
    y={["n"]}
    data={{
      columns: ["month", "n"],
      rows: [
        { month: "2026-01-01T00:00:00", n: 10 },
        { month: "2026-02-01T00:00:00", n: 20 },
      ],
    }}
  />,
);

// 30 categories: the tail folds into "other".
out.pie_many = render(
  <Chart
    kind="pie"
    x="cat"
    y={["n"]}
    data={{
      columns: ["cat", "n"],
      rows: Array.from({ length: 30 }, (_, i) => ({ cat: `c${i}`, n: 30 - i })),
    }}
  />,
);

// Scatter tooltips must name the category of each point.
out.scatter_label = render(
  <Chart
    kind="scatter"
    x="count"
    y={["total"]}
    data={{
      columns: ["region", "count", "total"],
      rows: [
        { region: "north", count: 71, total: 20141.57 },
        { region: "south", count: 60, total: 15000.0 },
      ],
    }}
  />,
);

// Wildly mismatched measure scales get a visible note.
out.scale_note = render(
  <Chart
    kind="bar"
    x="region"
    y={["row count", "total amount"]}
    data={{
      columns: ["region", "row count", "total amount"],
      rows: [
        { region: "us", "row count": 75, "total amount": 40000 },
        { region: "eu", "row count": 60, "total amount": 35000 },
      ],
    }}
  />,
);

// ----------------------------------------------------------------- model

// A raw-SQL panel's `{}` flow must parse to null, never crash.
try {
  out.sql_panel_flow = JSON.stringify(stateFromFlow({} as any, null));
} catch (e) {
  out.sql_panel_flow = `CRASH: ${String(e)}`;
}

// A text column bucketed by month synthesizes the compiler's cast step and
// round-trips through stateFromFlow.
{
  const kinds = { when: "text", delay: "number" } as const;
  const state = emptyExplore("events");
  state.groups = [{ column: "when", bucket: "month", binWidth: "", parse: true }];
  state.measures = [{ fn: "avg", column: "delay", alias: "average delay" }];
  state.sort = { column: "when month", dir: "asc" };
  const flow = exploreFlow(state, kinds as any, "ana");
  out.cast_flow = JSON.stringify(flow);
  out.cast_roundtrip = JSON.stringify(flow ? stateFromFlow(flow, null) : null);
}

// Duplicate groups and punctuation in names refuse in plain English, before
// any preview fires.
{
  const kinds = { region: "text", amount: "number" } as const;
  const dup = emptyExplore("orders");
  dup.groups = [
    { column: "region", bucket: "", binWidth: "", parse: false },
    { column: "region", bucket: "", binWidth: "", parse: false },
  ];
  out.dup_group_issues = JSON.stringify(exploreIssues(dup, kinds as any));

  const paren = emptyExplore("orders");
  paren.measures = [{ fn: "avg", column: "amount", alias: "Avg delay (min)" }];
  out.paren_alias_issues = JSON.stringify(exploreIssues(paren, kinds as any));
}

// A refusal that still gets through is rewritten into card vocabulary.
{
  const kinds = { region: "text", amount: "number" } as const;
  const state = emptyExplore("orders");
  state.groups = [{ column: "region", bucket: "", binWidth: "", parse: false }];
  const flow = exploreFlow(state, kinds as any, "ana")!;
  const agg = flow.nodes.find((n) => n.kind === "aggregate")!;
  out.refusal_rewrite = explainRefusal(
    `Step '${agg.id}' groups by the same column more than once.`,
    flow,
  );
}

// Drafts round-trip, and garbage degrades to null.
{
  const state = emptyExplore("orders");
  state.filters = [{ column: "region", op: "eq", value: "eu", values: [] }];
  state.groups = [{ column: "amount", bucket: "bin", binWidth: "100", parse: false }];
  const draft = {
    tab: "datasets" as const,
    state,
    obj: { typeName: "", groupBy: [], metrics: [], filters: [], search: "" },
    bindings: { chart: "bar", x: "", y: [], series: "", stacked: false },
  };
  const back = parseDraft(serializeDraft(draft));
  out.draft_roundtrip = JSON.stringify(back);
  out.draft_original = JSON.stringify(draft);
  out.draft_garbage = JSON.stringify([
    parseDraft("not json"),
    parseDraft(JSON.stringify({ v: 99 })),
    parseDraft(null),
  ]);
}

// The value-suggestion flow is an ordinary FlowDef over the one route.
out.distinct_flow = JSON.stringify(distinctValuesFlow("orders", "region", "ana"));

process.stdout.write(JSON.stringify(out));
