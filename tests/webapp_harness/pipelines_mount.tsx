// Mounts the REAL FlowsView — imported from the source tree, never copied —
// in the states the Pipelines UX pass pinned, and reports whether each state
// rendered what it must (or threw).
//
// renderToStaticMarkup performs exactly React's first render pass and runs no
// effects, so a state is built by seeding the QueryClient cache: a query whose
// key holds seeded data resolves on that first pass, which is how "the flow
// read RESOLVED with {flow: null, error}" — the corrupt-pipeline blocker —
// can be mounted without a server.
//
// Bundled at test time with the webapp's own esbuild (no vitest, no second
// test framework — see tests/test_charts_render.py for why).
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthContext } from "../../laurelin/ui/webapp/src/auth";
import { FlowsView } from "../../laurelin/ui/webapp/src/views/Flows";
import { saveConflict } from "../../laurelin/ui/webapp/src/views/flow/model";

const out: Record<string, unknown> = {};

// FlowBuilder reads window.location.hash for its ?new=1 / ?dataset= params;
// node has no window, so give it the minimum the read needs.
(globalThis as any).window = (globalThis as any).window ?? {};
(globalThis as any).window.location = { hash: "" };

// A signed-in editor, so the screens render their real content. Only the
// fields the screens read are meaningful; the rest satisfy the context shape.
const RANK: Record<string, number> = { viewer: 0, editor: 1, admin: 2 };
const AUTH = {
  loading: false,
  authRequired: true,
  setupRequired: false,
  user: { username: "ana", role: "editor" },
  role: "editor",
  can: (needed: string) => RANK["editor"] >= RANK[needed],
  multi: false,
  isSuperadmin: false,
  workspaces: [],
  activeSlug: null,
  setActiveWorkspace: () => {},
  flowsLocked: false,
  pipelinesLocked: false,
  oidc: undefined,
  saml: undefined,
  refresh: async () => {},
  refreshAuth: async () => {},
  login: async () => {},
  setup: async () => {},
  logout: async () => {},
  onUnauthorized: () => {},
};

function mount(name: string, path: string, seed?: (qc: QueryClient) => void) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  seed?.(qc);
  try {
    const html = renderToStaticMarkup(
      <AuthContext.Provider value={AUTH as any}>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route path="/pipelines/*" element={<FlowsView />} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </AuthContext.Provider>,
    );
    out[name] = html;
  } catch (e) {
    out[name] = `CRASH: ${(e as Error)?.stack ?? String(e)}`;
  }
}

// 1. The corrupt-flow page: GET /flows/{name} RESOLVED with {flow: null,
//    error} — a hand-edited file. The old page fell through to a spinner that
//    never ended; it must render the error and a delete door instead.
mount("broken", "/pipelines/broken_flow", (qc) => {
  qc.setQueryData(["flow", "broken_flow"], {
    flow: null,
    error: "pipelines/broken_flow.flow.json is not valid pipeline JSON: unexpected token at line 3",
    output_will_be_restricted: false,
  });
});

// 2. The visual list is empty but Python pipelines exist: the first-run hero
//    would claim an empty workspace, so a pointer at the Python tab renders
//    instead.
mount("list_python_only", "/pipelines", (qc) => {
  qc.setQueryData(["flows"], []);
  qc.setQueryData(["datasets"], []);
  qc.setQueryData(
    ["pipelines"],
    [{ name: "aviation", transforms: ["late_flights"], failed: false }],
  );
});

// 3. Nothing of either kind exists: the true first run, hero intact.
mount("list_first_run", "/pipelines", (qc) => {
  qc.setQueryData(["flows"], []);
  qc.setQueryData(["datasets"], []);
  qc.setQueryData(["pipelines"], []);
});

// 4. The 409 split, on the pure discriminator the banner branches on: the
//    server's machine-readable {code} when present, the route's two known
//    collision sentences when not, and everything else stays workspace-broken.
out["conflicts"] = {
  json_collision: saveConflict(JSON.stringify({ code: "name_collision", message: "taken" })).kind,
  json_workspace: saveConflict(
    JSON.stringify({ code: "workspace_collect_failed", message: "broken.py will not import" }),
  ).kind,
  legacy_output_owned: saveConflict(
    "Dataset 'late_flights' is already produced by transform 'late_flights'. Each dataset may "
    + "have only one producing transform.",
  ).kind,
  legacy_code_transform: saveConflict(
    "code transform 'late_flights' (pipeline file 'aviation') already uses this name or produces "
    + "'late_flights'. A flow cannot replace or share a name with a code transform; pick a "
    + "different flow name or remove the pipeline file first.",
  ).kind,
  legacy_collect_failed: saveConflict(
    "The workspace's pipelines cannot be collected: broken.py raised at import.",
  ).kind,
};

process.stdout.write(JSON.stringify(out));
