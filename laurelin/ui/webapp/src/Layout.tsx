// The platform shell: sidebar (brand, grouped nav, workspace + user footer) +
// content. The nav is the product's information architecture: five labeled
// groups, one door per job, instead of the flat fifteen-item list that grew
// one item per workflow.

import { useEffect, useRef, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { API, api } from "./api";
import { useAuth } from "./auth";
import { TreeGlyph } from "./brand";
import { CommandPalette } from "./palette";
import type { Role, WorkspaceInfo } from "./types";
import { Badge } from "./ui";

// `needs` is the role the page's *own* endpoints require, so the sidebar never
// offers a link whose only possible outcome is a 403 in a red box.
//
// R2 raised two of these. `/pipelines` reads pipeline files, which are exec'd
// Python — writing one is code-execution-equivalent, so reading one is
// editor-gated too, and that rationale governs the whole merged Pipelines
// item (Visual and Python tabs alike: a visual pipeline authors a transform
// that runs as the system). `/schedules` was always editor. `/audit` stays
// for everyone because the viewer's half of it is real: `GET /audit/mine`
// shows what *you* did, and you cannot learn a secret from a row you wrote.
//
// `/builds` (the screen formerly named Pipeline) deliberately stays viewer:
// lineage and build history are names, edges and statuses — the viewer's
// legitimate need — and none of it is authored prose.
export interface NavItem {
  to: string;
  label: string;
  needs?: Role;
  /** Superadmin is a flag orthogonal to the viewer<editor<admin rank. */
  superadmin?: boolean;
  /** Only rendered in multi-workspace mode — in single mode the page has
   *  nothing to manage, and a door onto nothing is noise. */
  multiOnly?: boolean;
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

export const NAV_GROUPS: NavGroup[] = [
  {
    label: "Data",
    items: [
      { to: "/datasets", label: "Datasets" },
      { to: "/ontology", label: "Ontology" },
    ],
  },
  {
    label: "Analyze",
    items: [
      { to: "/dashboards", label: "Dashboards" },
      // Beside Dashboards because it shares their sharing model: an analysis
      // is a multi-cell document a viewer opens for RESULTS. Viewer-visible —
      // the read and run routes are VIEWER, exactly like dashboards; adding
      // and editing cells inside the page is editor-gated by the page itself.
      { to: "/analyses", label: "Analyses" },
      // Editor-gated like Pipelines — its preview compiles and runs queries
      // through an EDITOR-gated endpoint, an authoring act. Merges into
      // Analyses in a follow-on milestone; until then it keeps its door.
      { to: "/explore", label: "Explore", needs: "editor" },
      // The throwaway-query scratchpad, deliberately separate from Analyses:
      // folding it in would force naming a persistent artifact for a
      // disposable query, and it is the one authoring-adjacent surface a
      // viewer can use (`POST /query` runs as the caller).
      { to: "/workbench", label: "SQL" },
      { to: "/apps", label: "Apps" },
    ],
  },
  {
    label: "Build",
    items: [
      // One door for "data → new dataset": the no-code builder (Visual tab)
      // is the front door and the Python editor (Python tab) is the advanced
      // surface, not the other way round.
      { to: "/pipelines", label: "Pipelines", needs: "editor" },
    ],
  },
  {
    label: "Operate",
    items: [
      { to: "/builds", label: "Builds" },
      { to: "/schedules", label: "Schedules", needs: "editor" },
      // Viewer-visible on purpose: the rollup is filtered per caller (the
      // GET /datasets precedent), so a viewer sees the health of exactly the
      // datasets they can read — statuses, codes, timestamps, nothing authored.
      { to: "/health", label: "Health" },
    ],
  },
  {
    label: "Govern",
    items: [
      { to: "/audit", label: "Audit" },
      { to: "/admin", label: "Admin", needs: "admin" },
      { to: "/workspaces", label: "Workspaces", superadmin: true, multiOnly: true },
    ],
  },
];

// Retired routes → their replacements. Kept indefinitely: bookmarks, docs and
// muscle memory outlive any release. App.tsx wires each into a redirect.
export const RETIRED_ROUTES: Record<string, string> = {
  "/pipeline": "/builds",
  "/flows": "/pipelines",
  "/transforms": "/pipelines?tab=python",
};

/** `/flows/x?q → /pipelines/x?q` — the visual builder's deep links survive. */
export function flowsRedirectTarget(pathname: string, search: string): string {
  const rest = pathname.replace(/^\/flows(?=\/|$)/, "");
  return `/pipelines${rest}${search}`;
}

interface NavGate {
  can: (role: Role) => boolean;
  isSuperadmin: boolean;
  multi: boolean;
}

/** The role-filtered nav. A group whose every item is hidden disappears with
 *  its header — an empty header would advertise hidden capability. The
 *  command palette consumes this same function, so it can never offer a door
 *  the sidebar hides. */
export function visibleNavGroups(auth: NavGate): NavGroup[] {
  return NAV_GROUPS.map((g) => ({
    label: g.label,
    items: g.items.filter(
      (n) =>
        (!n.needs || auth.can(n.needs)) &&
        (!n.superadmin || auth.isSuperadmin) &&
        (!n.multiOnly || auth.multi),
    ),
  })).filter((g) => g.items.length > 0);
}

export function Layout({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const location = useLocation();
  const firstRoute = useRef(true);
  const { data: ws } = useQuery({
    queryKey: ["workspace", auth.activeSlug],
    queryFn: () => api.get<WorkspaceInfo>(`${API}/workspace`),
    // In multi mode the endpoint is workspace-scoped; skip it until one is active.
    enabled: !auth.multi || !!auth.activeSlug,
  });

  // Standard SPA a11y: on navigation, move focus to the new page's heading so
  // screen readers announce the destination instead of staying mid-sidebar.
  //
  // Detail pages render their <h1> only after their data loads, so a single
  // synchronous query here found nothing (or the outgoing page's heading,
  // which then unmounted and dropped focus to <body> — verified with a CDP
  // probe on /dashboards/:name). So: focus the heading if it is already
  // there; otherwise park focus on the main region — the navigation is
  // announced either way — and hand it to the heading when it appears,
  // unless the user has moved focus themselves in the meantime.
  useEffect(() => {
    if (firstRoute.current) {
      firstRoute.current = false;
      return;
    }
    const main = document.getElementById("main");
    const focusH1 = (): boolean => {
      const h1 = main?.querySelector<HTMLElement>("h1");
      if (!h1) return false;
      h1.setAttribute("tabindex", "-1");
      h1.focus();
      return true;
    };
    if (focusH1()) return;
    main?.focus();
    const observer = new MutationObserver(() => {
      const active = document.activeElement;
      // Only steal focus from where WE parked it (or from body, where a
      // just-unmounted page drops it) — never from a control the user reached.
      if (active !== main && active !== document.body) {
        observer.disconnect();
        return;
      }
      if (focusH1()) observer.disconnect();
    });
    if (main) observer.observe(main, { childList: true, subtree: true });
    // A page that never renders a heading should not leave an observer
    // running for the rest of the session.
    const stop = window.setTimeout(() => observer.disconnect(), 5000);
    return () => {
      observer.disconnect();
      window.clearTimeout(stop);
    };
  }, [location.pathname]);

  return (
    <div className="app">
      {/* First tabbable element. href="#main" would fight HashRouter (the
          hash IS the route), so the jump is done in the handler instead. */}
      <a
        className="skip-link"
        href="#main"
        onClick={(e) => {
          e.preventDefault();
          document.getElementById("main")?.focus();
        }}
      >
        Skip to content
      </a>
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
          {visibleNavGroups(auth).map((g) => (
            <div key={g.label} className="nav-group-block">
              <div className="nav-group">{g.label}</div>
              {g.items.map((n) => (
                <NavLink
                  key={n.to}
                  to={n.to}
                  className={({ isActive }) => (isActive ? "active" : "")}
                >
                  <span className="nav-dot" />
                  {n.label}
                </NavLink>
              ))}
            </div>
          ))}
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

      <main id="main" tabIndex={-1} className="main">
        {children}
      </main>
      <CommandPalette />
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
