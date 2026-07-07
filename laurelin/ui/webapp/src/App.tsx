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
import { PipelineView } from "./views/Pipeline";
import { OntologyView } from "./views/Ontology";
import { WorkbenchView } from "./views/Workbench";
import { AuditView } from "./views/Audit";
import { AdminView } from "./views/Admin";

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

  return (
    <QueryClientProvider client={queryClient}>
      <HashRouter>
        <Layout>
          <Routes>
            <Route path="/datasets/*" element={<DatasetsView />} />
            <Route path="/pipeline" element={<PipelineView />} />
            <Route path="/ontology/*" element={<OntologyView />} />
            <Route path="/workbench" element={<WorkbenchView />} />
            <Route path="/audit" element={<AuditView />} />
            <Route
              path="/admin"
              element={auth.can("admin") ? <AdminView /> : <Navigate to="/datasets" replace />}
            />
            <Route path="*" element={<Navigate to="/datasets" replace />} />
          </Routes>
        </Layout>
      </HashRouter>
    </QueryClientProvider>
  );
}
