// App gate: decides between the setup screen, login screen, and the main app,
// and installs a QueryClient whose errors drop back to login on a 401.

import { useMemo } from "react";
import {
  QueryCache,
  QueryClient,
  QueryClientProvider,
  MutationCache,
} from "@tanstack/react-query";
import { HashRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { ApiError } from "./api";
import { useAuth } from "./auth";
import {
  Layout,
  RETIRED_ROUTES,
  exploreRedirectTarget,
  flowsRedirectTarget,
} from "./Layout";
import { LoginScreen, SetupScreen } from "./screens/AuthScreens";
import { Spinner } from "./ui";
import { DatasetsView } from "./views/Datasets";
import { DashboardsView } from "./views/Dashboards";
import { AnalysesView } from "./views/Analyses";
import { AppsView } from "./views/Apps";
import { BuildsView } from "./views/Pipeline";
import { SchedulesView } from "./views/Schedules";
import { HealthView } from "./views/Health";
import { PipelinesView } from "./views/Pipelines";
import { OntologyView } from "./views/Ontology";
import { WorkbenchView } from "./views/Workbench";
import { AuditView } from "./views/Audit";
import { AdminView } from "./views/Admin";
import { WorkspacesView } from "./views/Workspaces";

export function App() {
  const auth = useAuth();

  // One QueryClient for the app's lifetime; a 401 anywhere forces re-auth.
  const queryClient = useMemo(() => {
    const onError = (err: unknown) => {
      if (err instanceof ApiError && err.status === 401) auth.onUnauthorized();
    };
    return new QueryClient({
      queryCache: new QueryCache({ onError }),
      mutationCache: new MutationCache({ onError }),
      defaultOptions: {
        queries: { retry: false, refetchOnWindowFocus: false, staleTime: 5000 },
      },
    });
  }, [auth]);

  if (auth.loading) {
    return (
      <div className="auth-screen">
        <Spinner />
      </div>
    );
  }

  if (auth.authRequired && auth.setupRequired) return <SetupScreen />;
  if (auth.authRequired && !auth.user) return <LoginScreen />;

  // Multi-workspace: a signed-in user with no workspace access gets a clear
  // dead-end (only a superadmin can grant them membership).
  if (auth.multi && !auth.isSuperadmin && auth.workspaces.length === 0) {
    return <NoWorkspaceAccess />;
  }

  // Workspace-scoped views need an active workspace. A superadmin with none yet
  // is sent to the Workspaces control panel to create one.
  const needsWorkspace = auth.multi && !auth.activeSlug;
  const scoped = (el: JSX.Element) =>
    needsWorkspace ? <Navigate to="/workspaces" replace /> : el;

  // Role-aware landing: a viewer's entry points are things made FOR them
  // (dashboards); everyone who can author lands on the data itself.
  const landing = auth.can("editor") ? "/datasets" : "/dashboards";

  return (
    <QueryClientProvider client={queryClient}>
      <HashRouter>
        <Layout>
          <Routes>
            <Route path="/datasets/*" element={scoped(<DatasetsView />)} />
            <Route path="/dashboards/*" element={scoped(<DashboardsView />)} />
            <Route path="/analyses/*" element={scoped(<AnalysesView />)} />
            <Route path="/explore" element={<ExploreRedirect />} />
            <Route path="/builds" element={scoped(<BuildsView />)} />
            <Route path="/schedules" element={scoped(<SchedulesView />)} />
            <Route path="/health" element={scoped(<HealthView />)} />
            <Route path="/pipelines/*" element={scoped(<PipelinesView />)} />
            {/* Retired routes (see RETIRED_ROUTES in Layout.tsx). Kept
                indefinitely so bookmarks, docs and muscle memory keep
                working after the IA consolidation. */}
            <Route
              path="/pipeline"
              element={<Navigate to={RETIRED_ROUTES["/pipeline"]} replace />}
            />
            <Route path="/flows/*" element={<FlowsRedirect />} />
            <Route
              path="/transforms"
              element={<Navigate to={RETIRED_ROUTES["/transforms"]} replace />}
            />
            <Route path="/apps/*" element={scoped(<AppsView />)} />
            <Route path="/ontology/*" element={scoped(<OntologyView />)} />
            <Route path="/workbench" element={scoped(<WorkbenchView />)} />
            <Route path="/audit" element={scoped(<AuditView />)} />
            <Route
              path="/admin"
              element={
                auth.can("admin") ? scoped(<AdminView />) : <Navigate to={landing} replace />
              }
            />
            <Route
              path="/workspaces"
              element={auth.isSuperadmin ? <WorkspacesView /> : <Navigate to={landing} replace />}
            />
            <Route
              path="*"
              element={<Navigate to={needsWorkspace ? "/workspaces" : landing} replace />}
            />
          </Routes>
        </Layout>
      </HashRouter>
    </QueryClientProvider>
  );
}

// `/flows/:name` deep links carry the pipeline's name in the path, so the
// redirect has to preserve the subpath (and any search params) rather than
// dumping every old link on the list page.
function FlowsRedirect() {
  const loc = useLocation();
  return <Navigate to={flowsRedirectTarget(loc.pathname, loc.search)} replace />;
}

// `/explore` merged into Analyses as its quick-chart entry. The redirect keeps
// every search param (`?dataset=`, `?dashboard=&panel=`) so the dataset-detail
// door and the dashboard panel-edit round trip both land where the capability
// now lives, not on a bare list page.
function ExploreRedirect() {
  const loc = useLocation();
  return <Navigate to={exploreRedirectTarget(loc.search)} replace />;
}

function NoWorkspaceAccess() {
  const auth = useAuth();
  return (
    <div className="auth-screen">
      <div className="auth-card">
        <h2>No workspace access</h2>
        <p className="dim" style={{ textAlign: "center" }}>
          Your account isn't a member of any workspace yet. Ask a server
          administrator to add you.
        </p>
        <button className="primary" style={{ width: "100%", marginTop: 12 }} onClick={() => void auth.logout()}>
          Sign out
        </button>
      </div>
    </div>
  );
}
