// Small shared UI primitives used across views. Keep this dependency-light —
// views import from here so the look stays consistent.

import type { ReactNode } from "react";
import { ApiError } from "./api";

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return <div className="loading">{label}</div>;
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorBox({ error }: { error: unknown }) {
  let msg: string;
  if (error instanceof ApiError) {
    msg =
      error.status === 403
        ? `Insufficient permissions (403)${error.detail ? " — " + error.detail : ""}`
        : `Error ${error.status || ""}: ${error.detail}`;
  } else {
    msg = `Error: ${String((error as Error)?.message ?? error)}`;
  }
  return <div className="error-box">{msg}</div>;
}

// The marker the API sends instead of a value it could not redact safely — an
// ODBC keyword string, a URL carrying a query, anything nested in a config.
// Kept byte-identical to `laurelin/core/redaction.py:WITHHELD`.
export const WITHHELD = "***** (withheld)";

/**
 * A config value that may have been withheld.
 *
 * Rendering the marker raw would be readable but wrong-shaped in a table, and
 * rendering it as an empty cell would be worse: "nothing configured" is what an
 * operator reads from a blank field, and their next move is to type the
 * credential in again. So a withheld value says so, and says why.
 */
export function RedactedValue({ value }: { value: string }) {
  if (value !== WITHHELD) return <>{value}</>;
  return (
    <span
      className="faint"
      title="Withheld by the server: this value could not be redacted safely, so none of it was sent. Laurelin masks a credential only where it can locate it exactly — in a scheme://user:password@host URL. An ODBC keyword string, or a URL with a query, could hide a secret anywhere in it."
    >
      withheld
    </span>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "gold" | "green" | "red" | "blue";
}) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle && <div className="subtitle">{subtitle}</div>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}

export interface Column<T> {
  label: string;
  render: (row: T) => ReactNode;
  className?: string;
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  isSelected,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T, i: number) => string;
  onRowClick?: (row: T) => void;
  isSelected?: (row: T) => boolean;
}) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((c, i) => (
              <th key={i} className={c.className}>
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr
              key={rowKey(row, i)}
              className={[
                onRowClick ? "clickable" : "",
                isSelected?.(row) ? "selected" : "",
              ]
                .join(" ")
                .trim()}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
            >
              {columns.map((c, j) => (
                <td key={j} className={c.className}>
                  {c.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function fmtNum(n: number | null | undefined): string {
  return n == null ? "—" : n.toLocaleString("en-US");
}

/**
 * A result count that may be a floor rather than an exact number. A broad
 * search stops counting at a cap, so rendering the bare number would state
 * "10,000" when the truth is "at least 10,000".
 */
export function fmtCount(r: { total: number; total_capped?: boolean } | null | undefined): string {
  if (r == null) return "—";
  return r.total_capped ? `${fmtNum(r.total)}+` : fmtNum(r.total);
}

export function fmtTime(ts: string | null | undefined): string {
  if (!ts) return "—";
  return ts.replace("T", " ").slice(0, 19);
}

export function fmtValue(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
