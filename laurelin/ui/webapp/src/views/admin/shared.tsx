// Shared helpers for the Admin sub-sections (Groups, Ontology access).

import { API, ApiError } from "../../api";

/** Extract a human message from any thrown error. */
export function errDetail(err: unknown): string {
  if (err instanceof ApiError) return err.detail || `Error ${err.status}`;
  return String((err as Error)?.message ?? err);
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
