// Mounts the REAL components behind the point-of-decision guidance — imported
// from the source tree, never copied — and prints one JSON object of results
// for tests/test_ui_guidance.py to assert against.
//
// The findings this file pins were all "a new user stalls here" cliffs: the
// dataset detail page was a dead end, the Dashboards empty state pointed at a
// door (Analyses) that cannot put anything on a dashboard, and a dashboard
// never said who can see it. Every claim below is made about the exact markup
// react-dom/server emits from the shipped components.
//
// Bundled at test time with the webapp's own esbuild (no vitest, no second
// test framework — see tests/test_charts_render.py for why).
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthContext } from "../../laurelin/ui/webapp/src/auth";
import { DatasetOpenIn } from "../../laurelin/ui/webapp/src/views/Datasets";
import { DashboardsView } from "../../laurelin/ui/webapp/src/views/Dashboards";
import { ExploreView } from "../../laurelin/ui/webapp/src/views/Explore";
import { FlowsView } from "../../laurelin/ui/webapp/src/views/Flows";

const out: Record<string, unknown> = {};

// Explore persists its draft to sessionStorage from render-adjacent code;
// node has none, so give it a real (in-memory) one.
const store = new Map<string, string>();
(globalThis as any).sessionStorage = {
  getItem: (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => void store.set(k, String(v)),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
};

const RANK: Record<string, number> = { viewer: 0, editor: 1, admin: 2 };

function makeAuth(role: string) {
  return {
    loading: false,
    authRequired: true,
    setupRequired: false,
    user: { username: "t", role },
    role,
    can: (needed: string) => RANK[role] >= RANK[needed],
    pipelinesLocked: false,
    flowsLocked: false,
    multi: false,
    isSuperadmin: false,
    workspaces: [],
    activeSlug: null,
    setActiveWorkspace: () => {},
    oidc: undefined,
    saml: undefined,
    refresh: async () => {},
    login: async () => {},
    setup: async () => {},
    logout: async () => {},
    onUnauthorized: () => {},
  };
}

function mount(
  el: JSX.Element,
  role: string,
  path: string,
  seed?: (qc: QueryClient) => void,
): string {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  seed?.(qc);
  try {
    return renderToStaticMarkup(
      <AuthContext.Provider value={makeAuth(role) as any}>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={[path]}>{el}</MemoryRouter>
        </QueryClientProvider>
      </AuthContext.Provider>,
    );
  } catch (e) {
    return `CRASH: ${(e as Error)?.stack ?? String(e)}`;
  }
}

function hrefs(html: string): string[] {
  return [...html.matchAll(/href="#?([^"]*)"/g)].map((m) => m[1]);
}

// ---------------------------------------------------- dataset next-step doors

for (const role of ["viewer", "editor", "admin"]) {
  const html = mount(<DatasetOpenIn name="tidy_flights" />, role, "/datasets/tidy_flights");
  out[`open_in_${role}`] = { html, hrefs: hrefs(html) };
}

// --------------------------------------------- dashboards guidance and cliffs

// The views own nested <Routes>, so mount them exactly as App.tsx does —
// under their wildcard route — or ":name" would never match a two-segment
// location.
const dashboardsApp = (
  <Routes>
    <Route path="/dashboards/*" element={<DashboardsView />} />
  </Routes>
);

// The editor's empty state: which doors does it offer?
out.dashboards_empty_editor = mount(
  dashboardsApp, "editor", "/dashboards",
  (qc) => qc.setQueryData(["dashboards"], []),
);

// A dashboard page: does it state its zero-step visibility?
const seedOps = (qc: QueryClient) =>
  qc.setQueryData(["dashboard", "ops"], {
    name: "ops",
    title: "Ops overview",
    description: "",
    panels: [],
    updated_at: "2026-08-30T00:00:00Z",
  });
out.dashboard_page_editor = mount(dashboardsApp, "editor", "/dashboards/ops", seedOps);
out.dashboard_page_viewer = mount(dashboardsApp, "viewer", "/dashboards/ops", seedOps);

// --------------------------------------------------------- explore ?dataset=

// Opened from a dataset's "Open in Explore" door, the named dataset is the
// active source — not the resumed draft, not an empty screen.
store.clear();
out.explore_preset = mount(
  <ExploreView />, "editor", "/explore?dataset=flights",
  (qc) => qc.setQueryData(["datasets"], [{ name: "orders" }, { name: "flights" }]),
);

// ------------------------------------------------------- pipelines ?from=

// Opened from a dataset's "New pipeline from this dataset" door, the naming
// dialog is already open; opened plain, it is not.
const pipelinesApp = (
  <Routes>
    <Route path="/pipelines/*" element={<FlowsView />} />
  </Routes>
);
out.pipelines_from = mount(
  pipelinesApp, "editor", "/pipelines?from=tidy_flights",
  (qc) => qc.setQueryData(["flows"], []),
);
out.pipelines_plain = mount(
  pipelinesApp, "editor", "/pipelines",
  (qc) => qc.setQueryData(["flows"], []),
);

process.stdout.write(JSON.stringify(out));
