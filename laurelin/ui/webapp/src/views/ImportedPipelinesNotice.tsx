// The banner that explains an empty transform list.
//
// After an import, `pipelines/*.py` is on disk but the registry is deliberately
// empty: those files are `exec`'d unsandboxed on every build, so an import that
// ran them would be a code-delivery channel wearing a data-movement costume.
// The server therefore returns *no transforms* and refuses builds with a 409
// until an admin says they have read the files.
//
// Without this notice the Pipeline and Transforms pages show an empty list with
// a "no transforms yet" empty state — which reads as "the import lost my
// pipelines" rather than "your pipelines are parked behind one click". A safety
// control that looks like data loss is a safety control people route around.

import { useQuery } from "@tanstack/react-query";
import { API, api } from "../api";
import type { ImportState } from "../types";

export function ImportedPipelinesNotice() {
  const { data } = useQuery({
    queryKey: ["import-state"],
    queryFn: () => api.get<ImportState>(`${API}/workspace/import/state`),
    // Admin-only route: a viewer's 403 must not turn every page red, and the
    // notice is admin-actionable anyway.
    retry: false,
  });

  if (!data?.imported || data.pipelines_acknowledged) return null;

  return (
    <div className="error-box" style={{ marginBottom: 16 }}>
      <div style={{ fontWeight: 600 }}>
        Imported pipelines are parked — this list is empty on purpose.
      </div>
      <p style={{ fontSize: 12.5, marginTop: 6, marginBottom: 6 }}>
        The files arrived in an import and have not been reviewed. Transform files run as
        code on every build, so nothing here is loaded and builds refuse until an
        administrator confirms they have read them.
      </p>
      {data.content_warnings.length > 0 && (
        <ul style={{ fontSize: 12, paddingLeft: 18, marginTop: 0 }}>
          {data.content_warnings.map((w, i) => (
            <li key={i} className="mono">
              {w.line ? `${w.file}:${w.line}` : w.file} ({w.pattern}) — {w.preview}
            </li>
          ))}
        </ul>
      )}
      <div style={{ fontSize: 12 }}>
        <a href="#/admin">Admin → Portability → Re-supply checklist</a>
      </div>
    </div>
  );
}
