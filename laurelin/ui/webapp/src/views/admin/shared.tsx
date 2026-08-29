// Shared helpers for the Admin sub-sections (Groups, Ontology access).

import { API, ApiError } from "../../api";

/** Extract a human message from any thrown error. */
export function errDetail(err: unknown): string {
  if (err instanceof ApiError) return err.detail || `Error ${err.status}`;
  return String((err as Error)?.message ?? err);
}

/**
 * A governance write that came back 202: filed as a proposal, not applied.
 * Every admin section whose PUT/PATCH/DELETE goes through the approval gate
 * renders this under its save control — without it, a queued change looks
 * like a silent no-op (the form re-seeds from unchanged server state) and an
 * operator's next move is to "fix" it by saving again.
 */
export function QueuedBanner({ res }: { res: unknown }) {
  const q = res as { queued?: boolean; proposal_id?: string } | null | undefined;
  if (!q || q.queued !== true) return null;
  return (
    <div className="warn-box" style={{ marginTop: 8 }}>
      Not applied yet — this change loosens access and this workspace requires a
      second approver. Queued as proposal{" "}
      <span className="mono">{q.proposal_id}</span>; another admin can approve it
      in the Approvals inbox.
    </div>
  );
}

/** True when a mutation response is the 202 queued shape. */
export function isQueued(res: unknown): boolean {
  return !!res && (res as { queued?: boolean }).queued === true;
}

/** Small inline error box for a single control's failed mutation. */
export function InlineError({ err }: { err: unknown }) {
  if (!err) return null;
  return (
    <div className="error-box" style={{ marginTop: 8 }}>
      {errDetail(err)}
    </div>
  );
}

// The shared api client (../../api) has no PUT verb, and this view may not edit
// it. This helper mirrors request()'s semantics (JSON body, same-origin cookie
// auth, ApiError surface) for the two PUT endpoints these sections need.
export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API}${path}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      credentials: "same-origin",
    });
  } catch (e) {
    throw new ApiError(0, `Network error: ${(e as Error).message}`);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      if (data && data.detail) {
        detail =
          typeof data.detail === "string"
            ? data.detail
            : JSON.stringify(data.detail);
      }
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get("content-type") ?? "";
  if (!ct.includes("application/json")) return undefined as T;
  return res.json() as Promise<T>;
}
