// Small shared UI primitives used across views. Keep this dependency-light —
// views import from here so the look stays consistent.

import { useEffect, useRef, type CSSProperties, type KeyboardEvent, type ReactNode } from "react";
import { ApiError } from "./api";
import type { AuthoringWarning, Failure, FailureCode, Role } from "./types";

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return <div className="loading">{label}</div>;
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

/**
 * A failed surface's red box. `onRetry` matters for auto-run surfaces: the
 * app-wide query defaults are `retry: false, refetchOnWindowFocus: false`
 * (App.tsx), so without a Retry control a transient 503 on a dashboard panel
 * or an object list is terminal until a full reload. Screens whose queries
 * run without a user gesture pass their query's `refetch` here.
 */
export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  let msg: string;
  if (error instanceof ApiError) {
    msg =
      error.status === 403
        ? `Insufficient permissions (403)${error.detail ? " — " + error.detail : ""}`
        : `Error ${error.status || ""}: ${error.detail}`;
  } else {
    msg = `Error: ${String((error as Error)?.message ?? error)}`;
  }
  return (
    <div className="error-box">
      {msg}
      {onRetry && (
        <button type="button" className="small error-retry" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

/**
 * A polite live region for async outcomes — "1,204 rows", "Saved", a build's
 * terminal status. Screen-reader users otherwise learn that a result arrived
 * only by re-reading the page. `role="status"` announces content changes
 * without stealing focus; wrap the *changing* text, not whole sections.
 */
export function LiveStatus({
  children,
  className,
  style,
}: {
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <div role="status" className={className} style={style}>
      {children}
    </div>
  );
}

// ------------------------------------------------------------------- modal
//
// One dialog implementation for the whole app. The command palette proved the
// pattern (role=dialog, focus in on open, Escape closes, focus restored);
// every ad-hoc `.modal-backdrop` div in a view had none of it — an invisible
// wall to a keyboard user, whose Tab kept walking the page underneath.

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Keep Tab inside `container`: wraps from the last focusable to the first and
 * back. Exported for the command palette, which shares the dialog contract
 * but owns its own chrome.
 */
export function containFocusTab(
  e: { key: string; shiftKey: boolean; preventDefault: () => void },
  container: HTMLElement | null,
): void {
  if (e.key !== "Tab" || !container) return;
  const nodes = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE));
  if (nodes.length === 0) {
    e.preventDefault();
    return;
  }
  const first = nodes[0];
  const last = nodes[nodes.length - 1];
  const active = document.activeElement;
  if (e.shiftKey && (active === first || active === container)) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && active === last) {
    e.preventDefault();
    first.focus();
  }
}

/**
 * The app's one modal dialog.
 *
 * Renders the same `.modal-backdrop`/`.modal` chrome the hand-rolled call
 * sites used, so migrating a view is: replace the two outer divs with
 * `<Modal label onClose>` and delete the stopPropagation plumbing. What the
 * primitive adds and a view must not re-implement: role=dialog + aria-modal,
 * focus moves in on open (first focusable control, else the dialog), Escape
 * closes, Tab is contained, and focus returns to the opener on close.
 * Backdrop close is mousedown-on-backdrop, not click, so a drag that starts
 * in an input and ends outside cannot destroy the form.
 */
export function Modal({
  label,
  onClose,
  width,
  children,
}: {
  /** Accessible name for the dialog — what a screen reader announces. */
  label: string;
  /** Called on Escape, backdrop mousedown, and whatever close controls the
   *  content renders. The opener owns the open/closed state. */
  onClose: () => void;
  /** Content width (px or CSS length). Default is the sheet's 460px max. */
  width?: number | string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const dialog = ref.current;
    const first = dialog?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? dialog)?.focus();
    return () => {
      opener?.focus?.();
    };
  }, []);
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={ref}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        style={width !== undefined ? { width, maxWidth: "92vw" } : undefined}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.stopPropagation();
            onClose();
          } else {
            containFocusTab(e, ref.current);
          }
        }}
      >
        {children}
      </div>
    </div>
  );
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
  const why =
    "Withheld by the server: this value could not be redacted safely, so none of it was sent. Laurelin masks a credential only where it can locate it exactly — in a scheme://user:password@host URL. An ODBC keyword string, or a URL with a query, could hide a secret anywhere in it.";
  // tabIndex + aria-label: the explanation must be reachable without a mouse.
  // A title tooltip alone is invisible to keyboard and screen-reader users —
  // the *content* disclosed is unchanged, only the path to it.
  return (
    <span className="faint" tabIndex={0} title={why} aria-label={`withheld. ${why}`}>
      withheld
    </span>
  );
}

// ---------------------------------------------------------------- withheld
//
// R2 withholds a field from anyone below the level that could have written it.
// The server does that by *omitting the key*, which leaves the UI holding
// `undefined` — and `undefined` renders as nothing at all. Nothing is the one
// thing this must never be: an empty "From" cell on a connector reads as "no
// source configured", and an operator's next move is to type the connection
// string in again.
//
// So every withheld value renders as a visible statement of three facts: that
// something is there, that you are not being shown it, and who is.

const ROLE_ARTICLE: Record<Role, string> = {
  viewer: "a viewer",
  editor: "an editor",
  admin: "an admin",
};

function withheldTitle(what: string, role: Role, why?: string): string {
  const base =
    `${what} is not sent to your role. It reaches ${ROLE_ARTICLE[role]} and above` +
    ` — the level that can write it.`;
  return why ? `${base} ${why}` : base;
}

/**
 * An inline "you are not shown this" marker, for a table cell or a line of
 * metadata. Deliberately not styled like an error: nothing has gone wrong.
 */
export function Withheld({
  what,
  role,
  why,
  label,
}: {
  /** What is missing, as a noun phrase: "the connector's configuration". */
  what: string;
  /** The lowest role that does receive it. */
  role: Role;
  /** Optional extra sentence for the tooltip. */
  why?: string;
  /** Overrides the visible text; defaults to "<role> only". */
  label?: string;
}) {
  const title = withheldTitle(what, role, why);
  // Focusable, and the explanation rides aria-label: title-only prose is
  // unreachable by keyboard. Same statement, one more path to it.
  return (
    <span
      className="withheld"
      tabIndex={0}
      title={title}
      aria-label={`${label ?? `${role} only`}. ${title}`}
    >
      {label ?? `${role} only`}
    </span>
  );
}

/**
 * The block form, for a whole section a lower-privileged reader does not get —
 * a federated dataset's connection details, an app's filter scope. Says what is
 * missing and what the reader can still do, because "nothing here" and "not for
 * you" look identical otherwise.
 */
export function WithheldBox({
  what,
  role,
  children,
}: {
  what: string;
  role: Role;
  children?: ReactNode;
}) {
  return (
    <div className="withheld-box">
      <div className="withheld-head">{what} is not shown to your role</div>
      <p>
        It reaches {ROLE_ARTICLE[role]} and above — the level that can write it.
        If you cannot write it, you cannot read it.
      </p>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------- failures
//
// R1 replaced every persisted driver sentence with a structured record. The
// point of the code vocabulary is that each member maps to exactly one operator
// action, so this table is where that promise is kept: an operator has to be
// able to tell "the password is wrong" from "nothing is listening" without
// reading the driver's prose, because the driver's prose no longer exists here.

interface FailureText {
  label: string;
  /** What the operator should do about it. One action, not a list. */
  advice: string;
}

const FAILURE_TEXT: Record<FailureCode, FailureText> = {
  credential_malformed: {
    label: "Connection string could not be parsed",
    advice:
      "Nothing was sent to the remote system — fix the URL in the connector config. A password containing a space or an @ must be percent-encoded.",
  },
  endpoint_unresolvable: {
    label: "Host could not be resolved",
    advice: "DNS returned nothing for that hostname. Check it for a typo, and check the resolver this server uses.",
  },
  endpoint_unreachable: {
    label: "Nothing accepted a connection",
    advice:
      "The name resolved but the port refused. The service is down, the port is wrong, or a firewall is in the way — the credential was never tested.",
  },
  endpoint_timeout: {
    label: "The connection timed out",
    advice: "The endpoint accepted nothing before the deadline. Usually a network path or an overloaded remote, not a credential.",
  },
  auth_rejected: {
    label: "Authentication was rejected",
    advice:
      "The endpoint was reachable and refused the credential. Rotate or correct it — or check the database name, which some drivers report the same way.",
  },
  database_missing: {
    label: "The database does not exist",
    advice: "The server was reached and authenticated; the database named in the connection is not there.",
  },
  permission_denied: {
    label: "The remote system refused for lack of privilege",
    advice: "The credential is valid but the account cannot do this. Grant it on the remote side.",
  },
  // These three classify a *query* failure, and the engine is embedded DuckDB
  // as often as it is a federated cluster — so none of them may assert a
  // remote system. Measured: a no-code flow summing a column of text rendered
  // "A column referenced does not exist on the remote system", naming nothing,
  // about a column visibly present in the picker directly below it, on a
  // statement that never left the process.
  relation_missing: {
    label: "The table does not exist",
    advice: "It was dropped or renamed upstream, or the schema qualifier is wrong.",
  },
  column_missing: {
    label: "A referenced column does not exist, or holds a different kind of value",
    advice: "The schema changed under the query, or a step combines columns whose types do not fit together. Compare the query with the columns the data has now.",
  },
  schema_incompatible: {
    label: "The schema is not what Laurelin expected",
    advice: "Types or columns moved underneath a registered dataset. Re-register it, or fix the source.",
  },
  statement_invalid: {
    label: "The statement was rejected as invalid",
    advice: "A syntax error, or a construct this engine does not accept. The full text is in the server log.",
  },
  resource_exhausted: {
    label: "The remote system ran out of budget",
    advice: "Narrow it with a filter, an aggregate, or a smaller LIMIT.",
  },
  definition_stale: {
    label: "A saved definition no longer matches the data",
    advice: "A panel or an app still names a property that has been renamed or removed. Whoever can edit it sees which one; a reader deliberately does not.",
  },
  transform_failed: {
    // Not "Laurelin's own code": the code that raised is the workspace's own
    // transform or pipeline file, and blaming the platform sent authors to
    // file bugs about their own typo. Deliberately not "while running"
    // either: the same code covers a pipeline file that will not import
    // (phase `compile`), and the phase line below says which.
    label: "The pipeline's code raised",
    advice:
      "A transform or a pipeline file in this workspace raised. Fix the code it names — the exception class is on the technical line below when your role receives it; the full traceback is in the server log.",
  },
  blocked_by_upstream: {
    label: "Skipped: an input it reads from failed",
    // No log pointer on purpose: nothing here ran, nothing here raised, and
    // there is nothing in any log about this task. The one action is
    // upstream.
    advice: "Fix the failed upstream transform first; this one never started.",
  },
  expectation_failed: {
    label: "A data expectation failed",
    advice: "The transform ran and its output did not meet a declared expectation. The build was stopped on purpose.",
  },
  remote_failed: {
    label: "The remote system failed",
    advice: "Laurelin could not classify this one. The driver's own message is in the server log at the reference below.",
  },
};

function failureLabel(f: Failure): string {
  // `subject` is in the viewer projection, so even the narrowest reader can
  // be told WHICH upstream to look at instead of a generic "an input".
  if (f.code === "blocked_by_upstream" && f.subject) {
    return `Skipped: its input '${f.subject}' failed`;
  }
  return FAILURE_TEXT[f.code]?.label ?? f.code.replace(/_/g, " ");
}

export function failureAdvice(f: Failure): string {
  return FAILURE_TEXT[f.code]?.advice ?? "";
}

/** A one-word status for a table cell, with the whole explanation on hover —
 *  and on focus: the span is tabbable and the prose rides aria-label, so the
 *  explanation is not mouse-only. */
export function FailureBadge({ failure }: { failure: Failure }) {
  const prose = `${failureLabel(failure)}. ${failureAdvice(failure)}`;
  return (
    <span tabIndex={0} title={prose} aria-label={prose}>
      <Badge tone="red">{failure.code.replace(/_/g, " ")}</Badge>
    </span>
  );
}

/**
 * The full statement of a failure.
 *
 * A viewer's copy carries only `code` and `subject` — that is the whole
 * projection, by design — so the technical line and the log reference simply do
 * not render, and a line says why rather than leaving the reader wondering
 * whether the build really failed for no reason.
 *
 * `role` is the CALLER's role, from useAuth(). It exists because the
 * missing-detail fallback used to key on the absent `detail_ref` alone and
 * told a superadmin "an editor sees more" — a lie. Only a role below editor
 * gets the your-role-sees-less education; anyone else with no ref is told the
 * truth: no further detail was recorded. Omitting `role` is safe — the
 * neutral sentence renders — but screens that know the caller should pass it.
 */
export function FailureNote({ failure, role }: { failure: Failure; role?: Role }) {
  const detail = [
    failure.driver,
    failure.exc_class,
    failure.vendor_code && `code ${failure.vendor_code}`,
    failure.phase && `during ${failure.phase}`,
  ]
    .filter(Boolean)
    .join(" · ");
  const counters = Object.entries(failure.counters ?? {});

  return (
    <div className="failure-note">
      <div className="failure-head">
        {failureLabel(failure)}
        {failure.endpoint && <span className="mono dim"> at {failure.endpoint}</span>}
      </div>
      <p className="failure-advice">{failureAdvice(failure)}</p>
      {counters.length > 0 && (
        <div className="mono dim failure-meta">
          {counters.map(([k, v]) => `${k}=${fmtNum(v)}`).join(" · ")}
        </div>
      )}
      {detail && <div className="mono dim failure-meta">{detail}</div>}
      {failure.detail_ref ? (
        <div className="failure-meta faint">
          Full text in the server log —{" "}
          <span className="mono">grep {failure.detail_ref}</span>. Laurelin never
          stores a driver's own message, because it cannot know what is in it.
        </div>
      ) : role === "viewer" ? (
        <div className="failure-meta faint">
          This is everything your role is shown. An editor sees which system
          failed, at which step, and a reference into the server log.
        </div>
      ) : (
        <div className="failure-meta faint">
          No further detail was recorded for this failure.
        </div>
      )}
    </div>
  );
}

/**
 * Non-blocking authoring hints from a write that succeeded.
 *
 * These used to be a 400. They are a banner because nothing's confidentiality
 * rests on them any more: panel SQL is not served to a viewer whatever it
 * contains, so a wrong guess costs an editor a yellow box instead of costing a
 * viewer a password.
 */
export function WarningBox({ warnings }: { warnings?: AuthoringWarning[] }) {
  if (!warnings || warnings.length === 0) return null;
  return (
    <div className="warn-box">
      {warnings.map((w, i) => (
        <div key={i}>
          <span className="mono">{w.field}</span> — {w.hint}
        </div>
      ))}
    </div>
  );
}

/**
 * An inline status note on an authoring surface — "saved", "this will not
 * compile", "no rows matched". One component (backed by `.note` in styles.css)
 * where Transforms, Explore and Analyses each used to carry a private, slowly
 * drifting copy. Neutral when no tone is given; `ok` is green, `bad` is a
 * refusal, `warn` advises, `gov` states a governance consequence.
 */
export function Note({
  tone,
  children,
  style,
}: {
  tone?: "ok" | "bad" | "warn" | "gov";
  children: ReactNode;
  style?: CSSProperties;
}) {
  return (
    <div className={tone ? `note note-${tone}` : "note"} style={style}>
      {children}
    </div>
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

/**
 * The keyboard contract: `onRowClick` implies the row is a control, so the
 * row is tabbable and Enter/Space activate it — a view never adds its own
 * key handling on top. A clickable row that only a mouse could reach made
 * ObjectBrowser's whole selection flow keyboard-invisible.
 */
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
              tabIndex={onRowClick ? 0 : undefined}
              onKeyDown={
                onRowClick
                  ? (e: KeyboardEvent<HTMLTableRowElement>) => {
                      // Only when the row itself has focus: Enter inside a
                      // control rendered in a cell must keep meaning that
                      // control, and Space in an input must stay a space.
                      if (e.target !== e.currentTarget) return;
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onRowClick(row);
                      }
                    }
                  : undefined
              }
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
 * The one sentence for every name gate, so six surfaces stop phrasing the
 * same regex five ways (silent disable, raw `^[a-z][a-z0-9_]*$`, literal
 * markdown backticks, and two prose variants — all live at once). Two rules
 * exist in the product and only two: names that allow hyphens (dashboards,
 * schedules, analyses, workspaces: `^[a-z][a-z0-9_-]{0,63}$`) and names that
 * do not (datasets, pipelines: `^[a-z][a-z0-9_]*$`). One sentence each,
 * stated in words a person can act on, never as a regex.
 */
export const NAME_RULE =
  "Lowercase letters, digits, underscores and hyphens, starting with a letter — for example nightly_rollup.";
export const NAME_RULE_NO_HYPHEN =
  "Lowercase letters, digits and underscores, starting with a letter — for example late_orders.";

/**
 * The one phrase for a truncated result, so "truncated at 1000",
 * "(first of more)", "Showing the first 200 rows…" and "first 1,000 of a
 * larger result" stop being four renderings of one fact. Callers may append
 * an action ("— add a filter…") but the fact itself has one voice.
 */
export function truncationNote(n: number): string {
  return `first ${fmtNum(n)} of a larger result`;
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
