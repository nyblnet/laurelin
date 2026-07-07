// The platform shell: sidebar (brand, nav, workspace + user footer) + content.

import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { API, api } from "./api";
import { useAuth } from "./auth";
import { TreeGlyph } from "./brand";
import type { WorkspaceInfo } from "./types";
import { Badge } from "./ui";

const NAV = [
  { to: "/datasets", label: "Datasets" },
  { to: "/pipeline", label: "Pipeline" },
  { to: "/ontology", label: "Ontology" },
  { to: "/workbench", label: "SQL" },
  { to: "/audit", label: "Audit" },
];

export function Layout({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const { data: ws } = useQuery({
    queryKey: ["workspace"],
    queryFn: () => api.get<WorkspaceInfo>(`${API}/workspace`),
  });

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <TreeGlyph />
          <span className="brand-name">Laurelin</span>
        </div>
        <div className="brand-tag">Ontology Data Platform</div>

        <nav className="nav">
          {NAV.map((n) => (
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
        </nav>

        <div className="sidebar-foot">
          {ws && (
            <>
              <div className="ws-name">{ws.name}</div>
              <div className="path">{ws.root}</div>
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
        <Badge tone={auth.user.role === "admin" ? "gold" : "neutral"}>
          {auth.user.role}
        </Badge>
      </div>
      <button className="small" onClick={() => void auth.logout()}>
        Sign out
      </button>
    </div>
  );
}
