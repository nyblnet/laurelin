// Zero-dependency SVG charts for query results.
//
// Colors come from the design-system CSS variables — tokens only, never hex —
// so charts match the app theme without a chart library, and a future light
// theme is one :root away. Data model: {columns, rows} exactly as the /query
// endpoint returns. X = a chosen (or inferred first non-numeric) column;
// Y = chosen (or all numeric) columns; `series` = a categorical column whose
// *values* become the series (the long result is pivoted wide before drawing).
//
// The zero-CDN single-file bundle constraint is why everything here is
// hand-rolled: no chart library can be added, ever.
//
// One honesty rule governs everything below: a mark on the chart is a claim
// about a measured value. A NULL is not a zero, an empty bin is not adjacent
// to its neighbour, and a value the formatter rounds to "0" is a wrong
// answer, not a style choice. Where something cannot be drawn, it is skipped
// *and counted*, the way the pie's "n rows ≤ 0 not drawn" note always has.

import { useMemo } from "react";
import type { ChartKind } from "./types";

export type { ChartKind };

type Row = Record<string, unknown>;
/** A drawable value: a number, or null for "no measurement here". */
type Cell = number | null;

export interface ChartData {
  columns: string[];
  rows: Row[];
}

const SERIES_COLORS = [
  "var(--gold)",
  "var(--blue)",
  "var(--green)",
  "var(--red)",
  "var(--gold-dim)",
  "var(--series-6)",
];

function isNumeric(v: unknown): boolean {
  return typeof v === "number" && Number.isFinite(v);
}

/** How many rows classification scans. Sampling only the first row once made
 * a fully-numeric column with a NULL in row 0 render as "No numeric columns
 * to plot" — a false "no data" claim that depended on the result's order. */
const CLASSIFY_SAMPLE = 100;

/** A column is numeric when at least one sampled non-null value is a number
 * and no sampled non-null value is anything else. */
function columnIsNumeric(data: ChartData, c: string): boolean {
  let sawNumber = false;
  const n = Math.min(data.rows.length, CLASSIFY_SAMPLE);
  for (let i = 0; i < n; i++) {
    const v = data.rows[i][c];
    if (v == null) continue;
    if (!isNumeric(v)) return false;
    sawNumber = true;
  }
  return sawNumber;
}

/** Infer bindings: x = given or first column whose values aren't numeric
 * (fall back to row index); y = given or every numeric column. */
export function inferBindings(
  data: ChartData,
  x: string,
  y: string[],
): { xCol: string | null; yCols: string[] } {
  const numericCols = data.columns.filter((c) => columnIsNumeric(data, c));
  const yCols = (y.length > 0 ? y : numericCols).filter((c) =>
    data.columns.includes(c),
  );
  const xCol =
    x && data.columns.includes(x)
      ? x
      : data.columns.find((c) => !yCols.includes(c)) ?? null;
  return { xCol, yCols };
}

/** Merge each series' x-label sequence into one order that preserves every
 * series' own relative order (a topological merge). First-appearance order —
 * the old rule — turned rows sorted by (series, x) into a shuffled axis: a
 * sparse series contributed "Jan, Mar" before another added "Feb", and a
 * strictly rising trend rendered as a peak-and-decline. On a conflict (two
 * series disagree about the order) it falls back to first appearance, which
 * cannot invent an ordering the data doesn't have. */
function mergeLabelOrders(sequences: string[][]): string[] {
  const firstSeen: string[] = [];
  const seen = new Set<string>();
  for (const seq of sequences)
    for (const l of seq)
      if (!seen.has(l)) {
        seen.add(l);
        firstSeen.push(l);
      }
  const indeg = new Map<string, number>(firstSeen.map((l) => [l, 0]));
  const succ = new Map<string, Set<string>>();
  for (const seq of sequences) {
    for (let i = 0; i + 1 < seq.length; i++) {
      const a = seq[i];
      const b = seq[i + 1];
      if (a === b) continue;
      let s = succ.get(a);
      if (!s) {
        s = new Set();
        succ.set(a, s);
      }
      if (!s.has(b)) {
        s.add(b);
        indeg.set(b, (indeg.get(b) ?? 0) + 1);
      }
    }
  }
  const out: string[] = [];
  const remaining = new Set(firstSeen);
  while (remaining.size > 0) {
    // Among the labels with no unmet predecessor, take the earliest-seen one,
    // so ties keep the data's own order and the result is deterministic.
    let pick: string | null = null;
    for (const l of firstSeen) {
      if (remaining.has(l) && (indeg.get(l) ?? 0) === 0) {
        pick = l;
        break;
      }
    }
    if (pick === null) return firstSeen; // cycle: the series disagree
    remaining.delete(pick);
    out.push(pick);
    for (const b of succ.get(pick) ?? []) {
      if (remaining.has(b)) indeg.set(b, (indeg.get(b) ?? 0) - 1);
    }
  }
  return out;
}

/** Pivot a long result wide: one row per distinct `x` value, one column per
 * distinct `series` value, cells from the first y column. This is what makes
 * "amount by month, split by region" one dropdown instead of a self-join.
 * A missing (x, series) cell stays missing — it is a gap, not a zero. */
function pivotBySeries(
  data: ChartData,
  xCol: string,
  seriesCol: string,
  valueCol: string,
): { data: ChartData; yCols: string[] } {
  const cats: string[] = [];
  const perSeries = new Map<string, string[]>();
  const cells = new Map<string, Map<string, number>>();
  for (const r of data.rows) {
    const label = String(r[xCol] ?? "");
    const cat = String(r[seriesCol] ?? "");
    if (!cats.includes(cat)) {
      cats.push(cat);
      perSeries.set(cat, []);
    }
    const seq = perSeries.get(cat)!;
    if (seq[seq.length - 1] !== label) seq.push(label);
    const v = r[valueCol];
    if (isNumeric(v)) {
      let m = cells.get(label);
      if (!m) {
        m = new Map();
        cells.set(label, m);
      }
      m.set(cat, v as number);
    } else if (!cells.has(label)) {
      cells.set(label, new Map());
    }
  }
  const labels = mergeLabelOrders(cats.map((c) => perSeries.get(c)!));
  // A series *value* spelled like the x column's name would overwrite the
  // label cell in the wide row and destroy the axis; suffix it out of the way.
  const catKey = new Map<string, string>();
  for (const cat of cats) {
    let key = cat;
    while (key === xCol || [...catKey.values()].includes(key)) key = `${key} (split)`;
    catKey.set(cat, key);
  }
  const rows: Row[] = labels.map((label) => {
    const out: Row = { [xCol]: label };
    for (const cat of cats) {
      const v = cells.get(label)?.get(cat);
      if (v !== undefined) out[catKey.get(cat)!] = v;
    }
    return out;
  });
  const yCols = cats.map((c) => catKey.get(c)!);
  return { data: { columns: [xCol, ...yCols], rows }, yCols };
}

function niceStep(raw: number): number {
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
}

/** Ticks spanning [min, max]. Bars anchor at zero, so their axis always
 * includes it — a negative value used to be clamped to the baseline and drawn
 * as a sliver of nothing, which for a P&L chart is not a rendering nit but a
 * wrong answer. A scatter passes `includeZero: false`: its whole purpose is
 * the relationship inside the data's own range, and forcing zero once
 * collapsed five distinct points into 0.8 of one pixel. */
function niceTicks(
  min: number,
  max: number,
  count = 4,
  includeZero = true,
): number[] {
  let lo = includeZero ? Math.min(0, min) : min;
  let hi = includeZero ? Math.max(0, max) : max;
  if (hi - lo <= 0) {
    // Degenerate domain (all values equal): widen so the marks sit mid-range.
    hi = hi + (includeZero ? 1 : Math.max(1, Math.abs(hi) * 0.05));
    if (!includeZero) lo = lo - Math.max(1, Math.abs(lo) * 0.05);
  }
  const step = niceStep((hi - lo) / count);
  lo = Math.floor(lo / step + 1e-9) * step;
  hi = Math.ceil(hi / step - 1e-9) * step;
  const ticks: number[] = [];
  for (let v = lo; v <= hi + step * 1e-6; v += step) ticks.push(v);
  return ticks;
}

/** Decimal places needed to render `step` exactly (capped at 6). Tick labels
 * used toFixed(2) unconditionally, so a 0.001-stepped axis printed four
 * distinct nonzero gridlines all as "0.00" — each label literally false. */
function decimalsFor(step: number): number {
  let d = 0;
  while (d < 6) {
    const scaled = step * Math.pow(10, d);
    if (Math.abs(scaled - Math.round(scaled)) < 1e-9) break;
    d++;
  }
  return d;
}

function fmtTick(v: number, step: number): string {
  const tier = (t: number, suffix: string) =>
    `${(v / t).toFixed(Math.min(2, decimalsFor(step / t)))}${suffix}`;
  if (Math.abs(v) < step * 1e-6) return "0";
  // A billions tier: 3.9e9 used to label as "3900.0M".
  if (Math.abs(v) >= 1_000_000_000) return tier(1_000_000_000, "B");
  if (Math.abs(v) >= 1_000_000) return tier(1_000_000, "M");
  if (Math.abs(v) >= 1_000) return tier(1_000, "k");
  return v.toFixed(decimalsFor(step));
}

/** Value formatting for tooltips. A nonzero value must never print as "0":
 * toLocaleString with a fixed fraction cap rounded 0.0004 to exactly that. */
function fmtVal(v: unknown): string {
  if (isNumeric(v)) {
    const n = v as number;
    if (n !== 0 && Math.abs(n) < 0.001) return n.toPrecision(3);
    return n.toLocaleString("en-US", { maximumFractionDigits: 3 });
  }
  return String(v ?? "");
}

/** A midnight timestamp is a *bucket*, not a moment: date_trunc(month) comes
 * back as "2026-01-01T00:00:00", which the 10-char label cut rendered as
 * "2026-01-01…" — a truncated timestamp with a misleading ellipsis. Show the
 * date alone. */
function displayLabel(lb: string): string {
  const m = /^(\d{4}-\d{2}-\d{2})[T ]00:00:00(?:\.0+)?$/.exec(lb);
  return m ? m[1] : lb;
}

/** Head-truncate labels; if that collapses distinct labels into identical
 * ones (two long timestamps differing only at the end), tail-truncate
 * instead so the distinguishing part survives. */
function truncateLabels(labels: string[]): string[] {
  const shown = labels.map(displayLabel);
  const head = shown.map((l) => (l.length > 11 ? l.slice(0, 10) + "…" : l));
  if (new Set(head).size >= new Set(shown).size) return head;
  return shown.map((l) => (l.length > 11 ? "…" + l.slice(-10) : l));
}

const W = 640;
const H = 260;
const PAD = { top: 14, right: 12, bottom: 34, left: 52 };

function Legend({ names }: { names: string[] }) {
  if (names.length <= 1) return null;
  return (
    <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginTop: 6 }}>
      {names.map((c, s) => (
        <span key={c} style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11.5 }} className="dim">
          <span
            style={{
              width: 10,
              height: 10,
              borderRadius: 2,
              background: SERIES_COLORS[s % SERIES_COLORS.length],
              display: "inline-block",
            }}
          />
          {c}
        </span>
      ))}
    </div>
  );
}

function FootNote({ children }: { children: React.ReactNode }) {
  return (
    <div className="faint" style={{ fontSize: 11, marginTop: 4 }}>
      {children}
    </div>
  );
}

/** When x values are all numbers (a histogram's bins, hours, years), treat
 * the axis as numeric: sort ascending, and if the values sit on a regular
 * grid, materialize the missing positions as gaps. Uniform index spacing
 * drew bins 0, 30, 60, 300 equally spaced — eight empty bins' worth of gap
 * rendered identically to one bin's width, so a bimodal distribution with an
 * outlier cluster read as a smooth decay. The filled positions hold NULL,
 * not zero: "no rows landed here" is drawn as visible empty width, never as
 * a measured 0 for an average nobody computed. */
const MAX_FILLED_POSITIONS = 240;
function numericAxis(
  labels: string[],
  values: Cell[][],
  xVals: number[],
): { labels: string[]; values: Cell[][] } {
  const order = xVals.map((_, i) => i).sort((a, b) => xVals[a] - xVals[b]);
  let outLabels = order.map((i) => labels[i]);
  let outValues = values.map((s) => order.map((i) => s[i]));
  const sorted = order.map((i) => xVals[i]);

  let width = Infinity;
  for (let i = 1; i < sorted.length; i++) {
    const d = sorted[i] - sorted[i - 1];
    if (d > 0 && d < width) width = d;
  }
  if (!Number.isFinite(width) || width <= 0) return { labels: outLabels, values: outValues };
  const span = sorted[sorted.length - 1] - sorted[0];
  const positions = Math.round(span / width) + 1;
  const onGrid = sorted.every(
    (v) => Math.abs((v - sorted[0]) / width - Math.round((v - sorted[0]) / width)) < 1e-6,
  );
  if (!onGrid || positions <= sorted.length || positions > MAX_FILLED_POSITIONS)
    return { labels: outLabels, values: outValues };

  const dec = decimalsFor(width);
  const byPos = new Map(sorted.map((v, i) => [Math.round((v - sorted[0]) / width), i]));
  const fLabels: string[] = [];
  const fValues: Cell[][] = values.map(() => []);
  for (let p = 0; p < positions; p++) {
    const i = byPos.get(p);
    if (i !== undefined) {
      fLabels.push(outLabels[i]);
      outValues.forEach((s, si) => fValues[si].push(s[i]));
    } else {
      fLabels.push(String(Number((sorted[0] + p * width).toFixed(dec))));
      outValues.forEach((_, si) => fValues[si].push(null));
    }
  }
  return { labels: fLabels, values: fValues };
}

export function Chart({
  data: raw,
  kind,
  x = "",
  y = [],
  series = "",
  stacked = false,
}: {
  data: ChartData;
  kind: ChartKind;
  x?: string;
  y?: string[];
  series?: string;
  stacked?: boolean;
}) {
  // Bindings first, then the series pivot: the pivot needs to know which
  // column is x and which holds the values.
  const { data, xCol, yCols } = useMemo(() => {
    const base = inferBindings(raw, x, y);
    if (
      series &&
      raw.columns.includes(series) &&
      base.xCol &&
      series !== base.xCol &&
      kind !== "scatter" &&
      kind !== "stat" &&
      kind !== "table"
    ) {
      const valueCol = base.yCols.find((c) => c !== series);
      if (valueCol) {
        const p = pivotBySeries(raw, base.xCol, series, valueCol);
        return { data: p.data, xCol: base.xCol, yCols: p.yCols };
      }
    }
    return { data: raw, xCol: base.xCol, yCols: base.yCols };
  }, [raw, x, y, series, kind]);

  if (kind === "stat") return <StatChart data={data} yCols={yCols} />;
  if (data.rows.length === 0) {
    return <div className="faint" style={{ padding: "16px 0", fontSize: 13 }}>No rows.</div>;
  }
  if (yCols.length === 0) {
    return (
      <div className="faint" style={{ padding: "16px 0", fontSize: 13 }}>
        No numeric columns to plot.
      </div>
    );
  }

  if (kind === "pie") return <PieChart data={data} xCol={xCol} yCol={yCols[0]} />;
  if (kind === "scatter") return <ScatterChart data={data} x={x} yCols={yCols} />;

  const rows = data.rows;
  // NULL is "no measurement", never coerced to 0: a revenue line used to
  // plunge to the baseline for a month with no data, hover reading "0".
  let labels = rows.map((r, i) => (xCol ? String(r[xCol] ?? "") : String(i)));
  let values: Cell[][] = yCols.map((c) =>
    rows.map((r) => (isNumeric(r[c]) ? (r[c] as number) : null)),
  );

  // Count the data's own gaps before the numeric-axis fill below adds
  // synthetic empty positions — an empty histogram bin is not a "missing
  // value" and must not inflate the note.
  const missing = values.reduce(
    (acc, s) => acc + s.reduce((a: number, v) => a + (v === null ? 1 : 0), 0),
    0,
  );

  if (xCol && rows.length > 1 && rows.every((r) => isNumeric(r[xCol]))) {
    const na = numericAxis(labels, values, rows.map((r) => r[xCol] as number));
    labels = na.labels;
    values = na.values;
  }

  // Domain over the values that exist. A stacked bar's extent is the
  // per-label sum of its positive parts (and, separately, its negative
  // parts) rather than any single value.
  let dataMin: number;
  let dataMax: number;
  const isStacked = kind === "bar" && stacked && yCols.length > 1;
  if (isStacked) {
    const posSums = labels.map((_, i) =>
      values.reduce((acc, s) => acc + Math.max(0, s[i] ?? 0), 0),
    );
    const negSums = labels.map((_, i) =>
      values.reduce((acc, s) => acc + Math.min(0, s[i] ?? 0), 0),
    );
    dataMax = Math.max(...posSums);
    dataMin = Math.min(...negSums);
  } else {
    const flat = values.flat().filter((v): v is number => v !== null);
    if (flat.length === 0) {
      return (
        <div className="faint" style={{ padding: "16px 0", fontSize: 13 }}>
          No values to plot — every cell is empty.
        </div>
      );
    }
    dataMax = Math.max(...flat);
    dataMin = Math.min(...flat);
  }
  const ticks = niceTicks(dataMin, dataMax);
  const yMin = ticks[0];
  const yMax = ticks[ticks.length - 1];
  const step = ticks.length > 1 ? ticks[1] - ticks[0] : 1;

  const iw = W - PAD.left - PAD.right;
  const ih = H - PAD.top - PAD.bottom;
  const yPix = (v: number) => PAD.top + ih - ((v - yMin) / (yMax - yMin)) * ih;

  // Thin x labels so they never collide.
  const every = Math.max(1, Math.ceil(labels.length / 12));
  const shownLabels = truncateLabels(labels);

  // Two measures whose scales differ by orders of magnitude share one axis
  // here (dual axes are deliberately not offered), so the smaller one renders
  // sub-pixel. Say so instead of letting a measure silently vanish.
  let scaleNote: string | null = null;
  if (yCols.length > 1 && !isStacked) {
    const maxes = values.map((s) =>
      s.reduce((a: number, v) => Math.max(a, v === null ? 0 : Math.abs(v)), 0),
    );
    const big = Math.max(...maxes);
    const small = Math.min(...maxes.filter((m) => m > 0));
    if (Number.isFinite(small) && small > 0 && big / small > 50) {
      const smallCol = yCols[maxes.indexOf(small)];
      scaleNote =
        `“${smallCol}” is over ${Math.round(big / small)}× smaller than the largest ` +
        `measure and is nearly invisible on this shared axis — consider a separate panel.`;
    }
  }

  const grid = (
    <g>
      {ticks.map((t) => (
        <g key={t}>
          <line
            x1={PAD.left}
            x2={W - PAD.right}
            y1={yPix(t)}
            y2={yPix(t)}
            stroke="var(--border)"
            strokeWidth={Math.abs(t) < step * 1e-6 ? 1.4 : 0.6}
          />
          <text
            x={PAD.left - 8}
            y={yPix(t) + 3.5}
            textAnchor="end"
            fontSize={10.5}
            fill="var(--text-faint)"
            style={{ fontVariantNumeric: "tabular-nums" }}
          >
            {fmtTick(t, step)}
          </text>
        </g>
      ))}
    </g>
  );

  const xLabels = (xs: (i: number) => number) => (
    <g>
      {labels.map((_, i) =>
        i % every === 0 ? (
          <text
            key={i}
            x={xs(i)}
            y={H - PAD.bottom + 16}
            textAnchor="middle"
            fontSize={10.5}
            fill="var(--text-faint)"
          >
            {shownLabels[i]}
          </text>
        ) : null,
      )}
    </g>
  );

  let body: JSX.Element;
  if (kind === "bar") {
    const groupW = iw / labels.length;
    const center = (i: number) => PAD.left + groupW * i + groupW / 2;
    if (isStacked) {
      // Cumulative offsets: positives stack up from zero, negatives down.
      const barW = Math.max(2, groupW * 0.72);
      const posBase = labels.map(() => 0);
      const negBase = labels.map(() => 0);
      body = (
        <>
          {grid}
          {values.map((seriesVals, s) => (
            <g key={s} fill={SERIES_COLORS[s % SERIES_COLORS.length]}>
              {seriesVals.map((v, i) => {
                if (v === null || v === 0) return null;
                const base = v > 0 ? posBase[i] : negBase[i];
                const top = base + v;
                if (v > 0) posBase[i] = top;
                else negBase[i] = top;
                return (
                  <rect
                    key={i}
                    x={center(i) - barW / 2}
                    y={Math.min(yPix(base), yPix(top))}
                    width={barW - 1}
                    height={Math.max(0.5, Math.abs(yPix(base) - yPix(top)))}
                    rx={1.5}
                    opacity={0.92}
                  >
                    <title>{`${labels[i]} · ${yCols[s]}: ${fmtVal(v)}`}</title>
                  </rect>
                );
              })}
            </g>
          ))}
          {xLabels(center)}
        </>
      );
    } else {
      // Clamp the group's total width to its band: the old 2px floor let
      // 8 series × 2px overflow a 14px band, physically interleaving bars
      // from adjacent x categories.
      const barW = Math.min(
        Math.max(2, (groupW * 0.72) / yCols.length),
        groupW / yCols.length,
      );
      body = (
        <>
          {grid}
          {values.map((seriesVals, s) => (
            <g key={s} fill={SERIES_COLORS[s % SERIES_COLORS.length]}>
              {seriesVals.map((v, i) =>
                v === null ? null : (
                  // Anchored at the zero line and extending either way — a
                  // negative bar hangs below it rather than being clamped away.
                  <rect
                    key={i}
                    x={center(i) - (barW * yCols.length) / 2 + s * barW}
                    y={Math.min(yPix(v), yPix(0))}
                    width={Math.max(0.5, barW - 1)}
                    height={Math.max(0.5, Math.abs(yPix(0) - yPix(v)))}
                    rx={1.5}
                    opacity={0.92}
                  >
                    <title>{`${labels[i]} · ${yCols[s]}: ${fmtVal(v)}`}</title>
                  </rect>
                ),
              )}
            </g>
          ))}
          {xLabels(center)}
        </>
      );
    }
  } else {
    // line / area share geometry. A NULL breaks the line into segments — a
    // gap the eye can see — instead of being drawn as a measured zero.
    const xs = (i: number) =>
      PAD.left + (labels.length === 1 ? iw / 2 : (iw * i) / (labels.length - 1));
    body = (
      <>
        {grid}
        {values.map((seriesVals, s) => {
          const color = SERIES_COLORS[s % SERIES_COLORS.length];
          const segments: { i: number; v: number }[][] = [];
          let run: { i: number; v: number }[] = [];
          seriesVals.forEach((v, i) => {
            if (v === null) {
              if (run.length > 0) segments.push(run);
              run = [];
            } else {
              run.push({ i, v });
            }
          });
          if (run.length > 0) segments.push(run);
          const last = segments.length > 0 ? segments[segments.length - 1] : null;
          const lastPt = last ? last[last.length - 1] : null;
          return (
            <g key={s}>
              {segments.map((seg, gi) => {
                if (seg.length === 1) {
                  // An isolated point has no line to ride on; draw it so it
                  // is not silently invisible.
                  return (
                    <circle
                      key={gi}
                      cx={xs(seg[0].i)}
                      cy={yPix(seg[0].v)}
                      r={2.5}
                      fill={color}
                    />
                  );
                }
                const pts = seg.map((p) => `${xs(p.i)},${yPix(p.v)}`).join(" ");
                const areaPath = `M ${xs(seg[0].i)},${yPix(0)} L ${seg
                  .map((p) => `${xs(p.i)},${yPix(p.v)}`)
                  .join(" L ")} L ${xs(seg[seg.length - 1].i)},${yPix(0)} Z`;
                return (
                  <g key={gi}>
                    {kind === "area" && <path d={areaPath} fill={color} opacity={0.16} />}
                    <polyline
                      points={pts}
                      fill="none"
                      stroke={color}
                      strokeWidth={2}
                      strokeLinejoin="round"
                      strokeLinecap="round"
                    />
                  </g>
                );
              })}
              {/* emphasized endpoint */}
              {lastPt && <circle cx={xs(lastPt.i)} cy={yPix(lastPt.v)} r={3} fill={color} />}
              {/* invisible hover targets so every measured point reads out its
                  value, the way every bar always has — but never for a gap,
                  which has no value to assert */}
              {seriesVals.map((v, i) =>
                v === null ? null : (
                  <circle key={i} cx={xs(i)} cy={yPix(v)} r={8} fill="transparent">
                    <title>{`${labels[i]} · ${yCols[s]}: ${fmtVal(v)}`}</title>
                  </circle>
                ),
              )}
            </g>
          );
        })}
        {xLabels(xs)}
      </>
    );
  }

  return (
    <div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        style={{ width: "100%", height: "auto", display: "block" }}
        role="img"
        aria-label={`${kind} chart of ${yCols.join(", ")} by ${xCol ?? "row"}`}
      >
        {body}
      </svg>
      <Legend names={yCols} />
      {missing > 0 && (
        <FootNote>
          {missing} missing value{missing === 1 ? "" : "s"} shown as gap
          {missing === 1 ? "" : "s"}, not zero
        </FootNote>
      )}
      {scaleNote && <FootNote>{scaleNote}</FootNote>}
    </div>
  );
}

// ---------------------------------------------------------------------- pie

/** Above this, slices stop being readable: fold the tail into "other". A
 * 154-slice pie once rendered with a 150-entry legend of sub-1% arcs. */
const MAX_PIE_SLICES = 12;

function PieChart({
  data,
  xCol,
  yCol,
}: {
  data: ChartData;
  xCol: string | null;
  yCol: string;
}) {
  // Slices from rows with a positive value; a pie of negatives is not a chart.
  let slices = data.rows
    .map((r, i) => ({
      label: xCol ? String(r[xCol] ?? "") : String(i),
      value: isNumeric(r[yCol]) ? (r[yCol] as number) : 0,
    }))
    .filter((s) => s.value > 0);
  const skipped = data.rows.length - slices.length;
  const total = slices.reduce((a, s) => a + s.value, 0);
  if (total <= 0) {
    return (
      <div className="faint" style={{ padding: "16px 0", fontSize: 13 }}>
        Nothing positive to slice — a pie shows shares of a positive total.
      </div>
    );
  }

  // Largest first, tail folded: shares read clockwise from 12 o'clock in
  // decreasing order, and "other" says how many categories it absorbed.
  slices = [...slices].sort((a, b) => b.value - a.value);
  if (slices.length > MAX_PIE_SLICES) {
    const tail = slices.slice(MAX_PIE_SLICES - 1);
    slices = slices.slice(0, MAX_PIE_SLICES - 1);
    slices.push({
      label: `other (${tail.length} categories)`,
      value: tail.reduce((a, s) => a + s.value, 0),
    });
  }

  // The palette cycles at 6, so slice 7 would repeat slice 1's color while
  // physically touching it at 12 o'clock — two categories reading as one
  // merged wedge. Give the closing slice a color distinct from both
  // neighbours.
  const colorOf = (i: number): string => {
    let idx = i % SERIES_COLORS.length;
    if (i === slices.length - 1 && i >= SERIES_COLORS.length) {
      const firstIdx = 0;
      const prevIdx = (i - 1) % SERIES_COLORS.length;
      while (idx === firstIdx || idx === prevIdx) idx = (idx + 1) % SERIES_COLORS.length;
    }
    return SERIES_COLORS[idx];
  };

  const cx = W / 2;
  const cy = H / 2;
  const r = Math.min(W, H) / 2 - 16;
  let angle = -Math.PI / 2; // start at 12 o'clock
  const paths = slices.map((s, i) => {
    const frac = s.value / total;
    const a0 = angle;
    const a1 = angle + frac * 2 * Math.PI;
    angle = a1;
    const pct = (frac * 100).toFixed(1);
    const title = `${s.label}: ${fmtVal(s.value)} (${pct}%)`;
    const color = colorOf(i);
    if (frac > 0.99999) {
      // A single slice: an arc from a point to itself renders nothing.
      return (
        <circle key={i} cx={cx} cy={cy} r={r} fill={color} opacity={0.92}>
          <title>{title}</title>
        </circle>
      );
    }
    const x0 = cx + r * Math.cos(a0);
    const y0 = cy + r * Math.sin(a0);
    const x1 = cx + r * Math.cos(a1);
    const y1 = cy + r * Math.sin(a1);
    const large = frac > 0.5 ? 1 : 0;
    return (
      <path
        key={i}
        d={`M ${cx},${cy} L ${x0},${y0} A ${r},${r} 0 ${large} 1 ${x1},${y1} Z`}
        fill={color}
        opacity={0.92}
        stroke="var(--bg-1)"
        strokeWidth={1}
      >
        <title>{title}</title>
      </path>
    );
  });

  return (
    <div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        style={{ width: "100%", height: "auto", display: "block" }}
        role="img"
        aria-label={`pie chart of ${yCol} by ${xCol ?? "row"}`}
      >
        {paths}
      </svg>
      {/* Labels live in the HTML legend, not the SVG: slice-fitted text is a
          layout problem a legend simply does not have. */}
      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginTop: 6 }}>
        {slices.map((s, i) => (
          <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11.5 }} className="dim">
            <span
              style={{
                width: 10,
                height: 10,
                borderRadius: 2,
                background: colorOf(i),
                display: "inline-block",
              }}
            />
            {s.label} · {((s.value / total) * 100).toFixed(1)}%
          </span>
        ))}
        {skipped > 0 && (
          <span className="faint" style={{ fontSize: 11.5 }}>
            {skipped} row{skipped === 1 ? "" : "s"} ≤ 0 not drawn
          </span>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ scatter

function ScatterChart({
  data,
  x,
  yCols: yGiven,
}: {
  data: ChartData;
  x: string;
  yCols: string[];
}) {
  // Scatter is the one kind whose x axis is a value, not a category: both
  // bindings must be numeric. Infer x as the first numeric column when the
  // given one isn't, and drop it from y.
  const numericCols = data.columns.filter((c) => columnIsNumeric(data, c));
  const xCol = x && numericCols.includes(x) ? x : numericCols[0] ?? null;
  const yCols = yGiven.filter((c) => c !== xCol && numericCols.includes(c));
  if (!xCol || yCols.length === 0) {
    return (
      <div className="faint" style={{ padding: "16px 0", fontSize: 13 }}>
        A scatter plot needs two number columns — one for each axis.
      </div>
    );
  }

  // A categorical column that is neither axis names the points: without it a
  // four-point scatter of regions had tooltips that could not say which
  // region a point was.
  const labelCol =
    data.columns.find((c) => c !== xCol && !yCols.includes(c) && !columnIsNumeric(data, c)) ??
    null;

  const xVals: number[] = [];
  const yVals: number[] = [];
  let skippedRows = 0;
  for (const r of data.rows) {
    const xOk = isNumeric(r[xCol]);
    const anyY = yCols.some((c) => isNumeric(r[c]));
    if (!xOk || !anyY) skippedRows++;
    if (xOk && anyY) {
      xVals.push(r[xCol] as number);
      for (const c of yCols) if (isNumeric(r[c])) yVals.push(r[c] as number);
    }
  }
  if (xVals.length === 0) {
    return (
      <div className="faint" style={{ padding: "16px 0", fontSize: 13 }}>
        No rows with values on both axes.
      </div>
    );
  }
  // Axes fit the data — no forced zero. Zero-anchoring is right for bars
  // (length encodes the value) and wrong here (position encodes it): a tight
  // cluster far from zero once rendered as a single sub-pixel blob.
  const xTicks = niceTicks(Math.min(...xVals), Math.max(...xVals), 4, false);
  const yTicks = niceTicks(Math.min(...yVals), Math.max(...yVals), 4, false);
  const xMin = xTicks[0];
  const xMax = xTicks[xTicks.length - 1];
  const yMin = yTicks[0];
  const yMax = yTicks[yTicks.length - 1];
  const xStep = xTicks.length > 1 ? xTicks[1] - xTicks[0] : 1;
  const yStep = yTicks.length > 1 ? yTicks[1] - yTicks[0] : 1;

  const iw = W - PAD.left - PAD.right;
  const ih = H - PAD.top - PAD.bottom;
  const xPix = (v: number) => PAD.left + ((v - xMin) / (xMax - xMin)) * iw;
  const yPix = (v: number) => PAD.top + ih - ((v - yMin) / (yMax - yMin)) * ih;

  return (
    <div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        style={{ width: "100%", height: "auto", display: "block" }}
        role="img"
        aria-label={`scatter chart of ${yCols.join(", ")} by ${xCol}`}
      >
        <g>
          {yTicks.map((t) => (
            <g key={`y${t}`}>
              <line
                x1={PAD.left}
                x2={W - PAD.right}
                y1={yPix(t)}
                y2={yPix(t)}
                stroke="var(--border)"
                strokeWidth={Math.abs(t) < yStep * 1e-6 ? 1.4 : 0.6}
              />
              <text
                x={PAD.left - 8}
                y={yPix(t) + 3.5}
                textAnchor="end"
                fontSize={10.5}
                fill="var(--text-faint)"
                style={{ fontVariantNumeric: "tabular-nums" }}
              >
                {fmtTick(t, yStep)}
              </text>
            </g>
          ))}
          {xTicks.map((t) => (
            <g key={`x${t}`}>
              <line
                x1={xPix(t)}
                x2={xPix(t)}
                y1={PAD.top}
                y2={H - PAD.bottom}
                stroke="var(--border)"
                strokeWidth={Math.abs(t) < xStep * 1e-6 ? 1.4 : 0.6}
              />
              <text
                x={xPix(t)}
                y={H - PAD.bottom + 16}
                textAnchor="middle"
                fontSize={10.5}
                fill="var(--text-faint)"
                style={{ fontVariantNumeric: "tabular-nums" }}
              >
                {fmtTick(t, xStep)}
              </text>
            </g>
          ))}
        </g>
        {yCols.map((c, s) => (
          <g key={c} fill={SERIES_COLORS[s % SERIES_COLORS.length]}>
            {data.rows.map((r, i) =>
              isNumeric(r[xCol]) && isNumeric(r[c]) ? (
                <circle
                  key={i}
                  cx={xPix(r[xCol] as number)}
                  cy={yPix(r[c] as number)}
                  r={3.5}
                  opacity={0.8}
                >
                  <title>
                    {(labelCol ? `${fmtVal(r[labelCol])} · ` : "") +
                      `${xCol}: ${fmtVal(r[xCol])} · ${c}: ${fmtVal(r[c])}`}
                  </title>
                </circle>
              ) : null,
            )}
          </g>
        ))}
      </svg>
      <Legend names={yCols} />
      <FootNote>
        {xCol} →
        {skippedRows > 0 && (
          <>
            {" · "}
            {skippedRows} row{skippedRows === 1 ? "" : "s"} with a missing value not drawn
          </>
        )}
      </FootNote>
    </div>
  );
}

// --------------------------------------------------------------------- stat

function StatChart({ data, yCols }: { data: ChartData; yCols: string[] }) {
  const row = data.rows[0];
  const col = yCols[0] ?? data.columns[0];
  const value = row?.[col];
  // A KPI that answers "0" for a true 0.004 is a wrong answer: small nonzero
  // values switch to significant digits instead of a fraction cap.
  const display = isNumeric(value)
    ? (value as number) !== 0 && Math.abs(value as number) < 0.01
      ? (value as number).toPrecision(3)
      : (value as number).toLocaleString("en-US", { maximumFractionDigits: 2 })
    : value == null
      ? "—"
      : String(value);
  return (
    <div style={{ padding: "10px 0 4px" }}>
      <div
        style={{
          fontSize: 38,
          fontWeight: 650,
          color: "var(--gold)",
          fontVariantNumeric: "tabular-nums",
          lineHeight: 1.1,
        }}
      >
        {display}
      </div>
      {col && <div className="faint" style={{ fontSize: 11.5, marginTop: 4 }}>{col}</div>}
    </div>
  );
}
