// App gate: decides between the setup screen, login screen, and the main app,
// and installs a QueryClient whose errors drop back to login on a 401.

import { useMemo } from "react";
import {
  QueryCache,
  QueryClient,
  QueryClientProvider,
  MutationCache,
} from "@tanstack/react-query";
import { HashRouter, Navigate, Route, Routes } from "react-router-dom";
import { ApiError } from "./api";
import { useAuth } from "./auth";
import { Layout } from "./Layout";
import { LoginScreen, SetupScreen } from "./screens/AuthScreens";
import { Spinner } from "./ui";
import { DatasetsView } from "./views/Datasets";
import { DashboardsView } from "./views/Dashboards";
import { ExploreView } from "./views/Explore";
import { AppsView } from "./views/Apps";
import { PipelineView } from "./views/Pipeline";
import { SchedulesView } from "./views/Schedules";
import { TransformsView } from "./views/Transforms";
import { FlowsView } from "./views/Flows";
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

  return (
    <QueryClientProvider client={queryClient}>
      <HashRouter>
        <Layout>
          <Routes>
            <Route path="/datasets/*" element={scoped(<DatasetsView />)} />
            <Route path="/dashboards/*" element={scoped(<DashboardsView />)} />
            <Route path="/explore" element={scoped(<ExploreView />)} />
            <Route path="/pipeline" element={scoped(<PipelineView />)} />
            <Route path="/schedules" element={scoped(<SchedulesView />)} />
            <Route path="/flows/*" element={scoped(<FlowsView />)} />
            <Route path="/transforms" element={scoped(<TransformsView />)} />
            <Route path="/apps/*" element={scoped(<AppsView />)} />
            <Route path="/ontology/*" element={scoped(<OntologyView />)} />
            <Route path="/workbench" element={scoped(<WorkbenchView />)} />
            <Route path="/audit" element={scoped(<AuditView />)} />
            <Route
              path="/admin"
              element={
                auth.can("admin") ? scoped(<AdminView />) : <Navigate to="/datasets" replace />
              }
            />
            <Route
              path="/workspaces"
              element={auth.isSuperadmin ? <WorkspacesView /> : <Navigate to="/datasets" replace />}
            />
            <Route
              path="*"
              element={<Navigate to={needsWorkspace ? "/workspaces" : "/datasets"} replace />}
            />
          </Routes>
        </Layout>
      </HashRouter>
    </QueryClientProvider>
  );
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
