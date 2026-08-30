// Mounts the REAL Ontology and Apps views — imported from the source tree,
// never copied — and prints one JSON object of markup strings for
// tests/test_ontology_apps_ui.py to assert against.
//
// What this pins is the vocabulary-and-doors contract for the two ontology
// surfaces: an empty Ontology page names the mechanism that fills it
// (ontology/*.yml + the tutorial), an empty Apps page names its gate and its
// mechanism instead of advertising a door that does not exist, the type cards
// and app object rows are reachable by keyboard, and a capped search total
// renders as a floor ("N+") rather than a lie.
//
// Bundled at test time with the webapp's own esbuild (no vitest, no second
// test framework — see tests/test_charts_render.py for why).
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthContext } from "../../laurelin/ui/webapp/src/auth";
import { OntologyView } from "../../laurelin/ui/webapp/src/views/Ontology";
import { AppsView } from "../../laurelin/ui/webapp/src/views/Apps";
import { ObjectBrowser } from "../../laurelin/ui/webapp/src/views/ontology/ObjectBrowser";

const out: Record<string, unknown> = {};

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
    refreshAuth: async () => {},
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

// The views own nested <Routes>, so mount them exactly as App.tsx does.
const ontologyApp = (
  <Routes>
    <Route path="/ontology/*" element={<OntologyView />} />
  </Routes>
);
const appsApp = (
  <Routes>
    <Route path="/apps/*" element={<AppsView />} />
  </Routes>
);

// ------------------------------------------------------------- empty states

out.ontology_empty = mount(
  ontologyApp, "editor", "/ontology",
  (qc) => qc.setQueryData(["object-types"], []),
);
out.apps_empty = mount(
  appsApp, "viewer", "/apps",
  (qc) => qc.setQueryData(["apps"], []),
);

// ------------------------------------------------- keyboard-reachable cards

const aircraftType = {
  api_name: "aircraft",
  display_name: "Aircraft",
  description: "The fleet.",
  backing_dataset: "tidy_flights",
  primary_key: "tail_number",
  title_property: "tail_number",
  properties: {
    tail_number: { type: "string", display_name: "Tail number" },
    status: { type: "string", display_name: "Status" },
  },
};

out.ontology_list = mount(
  ontologyApp, "editor", "/ontology",
  (qc) => qc.setQueryData(["object-types"], [aircraftType]),
);

// ------------------------------------------------------- an app's object rows

const opsApp = {
  name: "ops",
  title: "Aircraft in maintenance",
  description: "",
  object_type: "aircraft",
  columns: [],
  search_placeholder: "",
  actions: [],
  links: [],
  created_at: "2026-08-30T00:00:00Z",
  updated_at: "2026-08-30T00:00:00Z",
};
const aircraftDetail = { ...aircraftType, links: [], actions: [] };
const objectRows = {
  objects: [
    { __pk: "N100", __title: "N100", tail_number: "N100", status: "maintenance" },
    { __pk: "N200", __title: "N200", tail_number: "N200", status: "maintenance" },
  ],
  total: 2,
};

out.app_page = mount(
  appsApp, "viewer", "/apps/ops",
  (qc) => {
    qc.setQueryData(["app", "ops"], opsApp);
    qc.setQueryData(["objectType", "aircraft"], aircraftDetail);
    qc.setQueryData(["objects", "aircraft", "ops", "", 0], objectRows);
  },
);

// ------------------------------------------------------- capped search total

function seededBrowser(result: unknown): string {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["objects", "aircraft", "", 0], result);
  try {
    return renderToStaticMarkup(
      <AuthContext.Provider value={makeAuth("viewer") as any}>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={["/ontology/aircraft"]}>
            <ObjectBrowser detail={aircraftDetail as any} />
          </MemoryRouter>
        </QueryClientProvider>
      </AuthContext.Provider>,
    );
  } catch (e) {
    return `CRASH: ${(e as Error)?.stack ?? String(e)}`;
  }
}

out.browser_capped = seededBrowser({
  objects: [{ __pk: "N100", __title: "N100", tail_number: "N100", status: "ok" }],
  total: 10000,
  total_capped: true,
});
out.browser_exact = seededBrowser({
  objects: [{ __pk: "N100", __title: "N100", tail_number: "N100", status: "ok" }],
  total: 10000,
});
out.browser_no_rows = seededBrowser({ objects: [], total: 0 });

process.stdout.write(JSON.stringify(out));
