// The platform shell: sidebar (brand, nav, workspace + user footer) + content.

import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { API, api } from "./api";
import { useAuth } from "./auth";
import { TreeGlyph } from "./brand";
import type { Role, WorkspaceInfo } from "./types";
import { Badge } from "./ui";

// `needs` is the role the page's *own* endpoints require, so the sidebar never
// offers a link whose only possible outcome is a 403 in a red box.
//
// R2 raised two of these. `/transforms` reads pipeline files, which are exec'd
// Python — writing one is code-execution-equivalent, so reading one is now
// editor-gated too. `/schedules` was always editor. `/audit` stays for
// everyone because the viewer's half of it is real: `GET /audit/mine` shows
// what *you* did, and you cannot learn a secret from a row you wrote.
//
// `/pipeline` deliberately stays viewer: lineage and build history are names,
// edges and statuses — the viewer's legitimate need — and none of it is
// authored prose.
const NAV: { to: string; label: string; needs?: Role }[] = [
  { to: "/datasets", label: "Datasets" },
  { to: "/dashboards", label: "Dashboards" },
  // Beside Dashboards because it shares their sharing model: an analysis is
  // a multi-cell notebook a viewer opens for RESULTS. Viewer-visible — the
  // read and run routes are VIEWER, exactly like dashboards; adding and
  // editing cells inside the page is editor-gated by the page itself.
  { to: "/analyses", label: "Analyses" },
  // Beside Dashboards, because it is how dashboards get made: point-and-click
  // shaping into a chart. Editor-gated like Flows — its preview compiles and
  // runs queries, an authoring act.
  { to: "/explore", label: "Explore", needs: "editor" },
  { to: "/pipeline", label: "Pipeline" },
  { to: "/schedules", label: "Schedules", needs: "editor" },
  // Flows sits ABOVE Transforms: the no-code builder is the front door and
  // the Python editor is the advanced surface, not the other way round.
  // Editor-gated for the same reason /transforms is — a flow authors a
  // transform that runs as the system and reads datasets.
  { to: "/flows", label: "Flows", needs: "editor" },
  { to: "/transforms", label: "Transforms", needs: "editor" },
  { to: "/apps", label: "Apps" },
  { to: "/ontology", label: "Ontology" },
  { to: "/workbench", label: "SQL" },
  { to: "/audit", label: "Audit" },
];

export function Layout({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const { data: ws } = useQuery({
    queryKey: ["workspace", auth.activeSlug],
    queryFn: () => api.get<WorkspaceInfo>(`${API}/workspace`),
    // In multi mode the endpoint is workspace-scoped; skip it until one is active.
    enabled: !auth.multi || !!auth.activeSlug,
  });

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <TreeGlyph />
          <span className="brand-name">Laurelin</span>
        </div>
        <div className="brand-tag">Ontology Data Platform</div>

        {auth.multi && auth.workspaces.length > 0 && (
          <div className="ws-switch">
            <label>Workspace</label>
            <select
              value={auth.activeSlug ?? ""}
              onChange={(e) => auth.setActiveWorkspace(e.target.value)}
            >
              {auth.workspaces.map((w) => (
                <option key={w.slug} value={w.slug}>
                  {w.name} ({w.role})
                </option>
              ))}
            </select>
          </div>
        )}

        <nav className="nav">
          {NAV.filter((n) => !n.needs || auth.can(n.needs)).map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              className={({ isActive }) => (isActive ? "active" : "")}
            >
              <span className="nav-dot" />
              {n.label}
            </NavLink>
          ))}
          {auth.can("admin") && (
            <NavLink to="/admin" className={({ isActive }) => (isActive ? "active" : "")}>
              <span className="nav-dot" />
              Admin
            </NavLink>
          )}
          {auth.isSuperadmin && (
            <NavLink to="/workspaces" className={({ isActive }) => (isActive ? "active" : "")}>
              <span className="nav-dot" />
              Workspaces
            </NavLink>
          )}
        </nav>

        <div className="sidebar-foot">
          {ws && (
            <>
              <div className="ws-name">{ws.name}</div>
              {/* Admin only. `root` is the server's filesystem layout —
                  deployment information a viewer cannot act on and was never
                  meant to have — so the API omits it and this row disappears
                  rather than rendering an empty line. */}
              {ws.root && <div className="path">{ws.root}</div>}
            </>
          )}
          <UserFooter />
        </div>
      </aside>

      <main className="main">{children}</main>
    </div>
  );
}

function UserFooter() {
  const auth = useAuth();

  if (!auth.authRequired) {
    return (
      <div className="user-box">
        <Badge tone="neutral">auth disabled · dev</Badge>
      </div>
    );
  }
  if (!auth.user) return null;

  return (
    <div className="user-box">
      <div className="who">
        <div className="name">{auth.user.username}</div>
        <Badge tone={auth.isSuperadmin || auth.role === "admin" ? "gold" : "neutral"}>
          {auth.isSuperadmin ? "superadmin" : auth.role}
        </Badge>
      </div>
      <button className="small" onClick={() => void auth.logout()}>
        Sign out
      </button>
    </div>
  );
}
