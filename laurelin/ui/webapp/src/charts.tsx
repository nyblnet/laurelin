// Zero-dependency SVG charts for query results (bar / line / area / stat).
//
// Colors come from the design-system CSS variables so charts match the app
// theme without a chart library. Data model: {columns, rows} exactly as the
// /query endpoint returns. X = a chosen (or inferred first non-numeric)
// column; Y = chosen (or all numeric) columns.

import { useMemo } from "react";

export type ChartKind = "table" | "bar" | "line" | "area" | "stat";

type Row = Record<string, unknown>;

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
  "#8a7fd6",
];

function isNumeric(v: unknown): boolean {
  return typeof v === "number" && Number.isFinite(v);
}

/** Infer bindings: x = given or first column whose values aren't numeric
 * (fall back to row index); y = given or every numeric column. */
export function inferBindings(
  data: ChartData,
  x: string,
  y: string[],
): { xCol: string | null; yCols: string[] } {
  const sample = data.rows[0] ?? {};
  const numericCols = data.columns.filter((c) => isNumeric(sample[c]));
  const yCols = (y.length > 0 ? y : numericCols).filter((c) =>
    data.columns.includes(c),
  );
  const xCol =
    x && data.columns.includes(x)
      ? x
      : data.columns.find((c) => !yCols.includes(c)) ?? null;
  return { xCol, yCols };
}

function niceTicks(max: number, count = 4): number[] {
  if (max <= 0) return [0];
  const raw = max / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const ticks: number[] = [];
  for (let v = 0; v <= max + 1e-9; v += step) ticks.push(v);
  return ticks;
}

function fmtTick(v: number): string {
  if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (Math.abs(v) >= 1_000) return `${(v / 1_000).toFixed(1)}k`;
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

const W = 640;
const H = 260;
const PAD = { top: 14, right: 12, bottom: 34, left: 52 };

export function Chart({
  data,
  kind,
  x = "",
  y = [],
}: {
  data: ChartData;
  kind: ChartKind;
  x?: string;
  y?: string[];
}) {
  const { xCol, yCols } = useMemo(
    () => inferBindings(data, x, y),
    [data, x, y],
  );

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

  const rows = data.rows;
  const labels = rows.map((r, i) => (xCol ? String(r[xCol] ?? "") : String(i)));
  const values = yCols.map((c) => rows.map((r) => (isNumeric(r[c]) ? (r[c] as number) : 0)));
  const maxVal = Math.max(1e-9, ...values.flat());
  const ticks = niceTicks(maxVal);
  const yMax = ticks[ticks.length - 1] || 1;

  const iw = W - PAD.left - PAD.right;
  const ih = H - PAD.top - PAD.bottom;
  const yPix = (v: number) => PAD.top + ih - (v / yMax) * ih;

  // Thin x labels so they never collide.
  const every = Math.max(1, Math.ceil(labels.length / 12));

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
            strokeWidth={t === 0 ? 1.4 : 0.6}
          />
          <text
            x={PAD.left - 8}
            y={yPix(t) + 3.5}
            textAnchor="end"
            fontSize={10.5}
            fill="var(--text-faint, #69707f)"
            style={{ fontVariantNumeric: "tabular-nums" }}
          >
            {fmtTick(t)}
          </text>
        </g>
      ))}
    </g>
  );

  const xLabels = (xs: (i: number) => number) => (
    <g>
      {labels.map((lb, i) =>
        i % every === 0 ? (
          <text
            key={i}
            x={xs(i)}
            y={H - PAD.bottom + 16}
            textAnchor="middle"
            fontSize={10.5}
            fill="var(--text-faint, #69707f)"
          >
            {lb.length > 11 ? lb.slice(0, 10) + "…" : lb}
          </text>
        ) : null,
      )}
    </g>
  );

  let body: JSX.Element;
  if (kind === "bar") {
    const groupW = iw / labels.length;
    const barW = Math.max(2, (groupW * 0.72) / yCols.length);
    const center = (i: number) => PAD.left + groupW * i + groupW / 2;
    body = (
      <>
        {grid}
        {values.map((series, s) => (
          <g key={s} fill={SERIES_COLORS[s % SERIES_COLORS.length]}>
            {series.map((v, i) => (
              <rect
                key={i}
                x={center(i) - (barW * yCols.length) / 2 + s * barW}
                y={yPix(Math.max(0, v))}
                width={barW - 1}
                height={Math.max(0.5, Math.abs(yPix(0) - yPix(v)))}
                rx={1.5}
                opacity={0.92}
              >
                <title>{`${labels[i]} · ${yCols[s]}: ${v}`}</title>
              </rect>
            ))}
          </g>
        ))}
        {xLabels(center)}
      </>
    );
  } else {
    // line / area share geometry
    const xs = (i: number) =>
      PAD.left + (labels.length === 1 ? iw / 2 : (iw * i) / (labels.length - 1));
    body = (
      <>
        {grid}
        {values.map((series, s) => {
          const color = SERIES_COLORS[s % SERIES_COLORS.length];
          const pts = series.map((v, i) => `${xs(i)},${yPix(v)}`).join(" ");
          const areaPath = `M ${xs(0)},${yPix(0)} L ${series
            .map((v, i) => `${xs(i)},${yPix(v)}`)
            .join(" L ")} L ${xs(series.length - 1)},${yPix(0)} Z`;
          return (
            <g key={s}>
              {kind === "area" && <path d={areaPath} fill={color} opacity={0.16} />}
              <polyline
                points={pts}
                fill="none"
                stroke={color}
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
              {/* emphasized endpoint */}
              <circle
                cx={xs(series.length - 1)}
                cy={yPix(series[series.length - 1])}
                r={3}
                fill={color}
              />
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
      >
        {body}
      </svg>
      {yCols.length > 1 && (
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginTop: 6 }}>
          {yCols.map((c, s) => (
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
      )}
    </div>
  );
}

function StatChart({ data, yCols }: { data: ChartData; yCols: string[] }) {
  const row = data.rows[0];
  const col = yCols[0] ?? data.columns[0];
  const value = row?.[col];
  const display = isNumeric(value)
    ? (value as number).toLocaleString("en-US", { maximumFractionDigits: 2 })
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
