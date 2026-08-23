// Mounts the REAL ExploreView — imported from the source tree, never copied —
// in its pre-data states and reports whether each state rendered or threw.
//
// renderToStaticMarkup performs exactly React's first render pass and runs no
// effects, so every useQuery on the screen is caught at isLoading with `data`
// undefined. That is the precise moment the "Cannot read properties of
// undefined (reading 'map')" flash was reported in (chip task_e3bb7900): a
// child mapping over a value before its data resolves. Any unguarded `.map`
// reachable at mount makes renderToStaticMarkup throw, and the state below
// records the crash instead of the markup.
//
// Bundled at test time with the webapp's own esbuild (no vitest, no second
// test framework — see tests/test_charts_render.py for why).
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthContext } from "../../laurelin/ui/webapp/src/auth";
import { ExploreView } from "../../laurelin/ui/webapp/src/views/Explore";
import {
  DRAFT_KEY,
  emptyExplore,
  serializeDraft,
} from "../../laurelin/ui/webapp/src/views/explore/model";

const out: Record<string, string> = {};

// The screen persists its draft to sessionStorage from render-adjacent code;
// node has none, so give it a real (in-memory) one the states below can seed.
const store = new Map<string, string>();
(globalThis as any).sessionStorage = {
  getItem: (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => void store.set(k, String(v)),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
};

// A signed-in editor, so ExploreView renders the real ExploreScreen instead
// of the viewer's "needs the editor role" empty state. Only the fields the
// screen reads are meaningful; the rest satisfy the context shape.
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
  oidc: undefined,
  saml: undefined,
  refresh: async () => {},
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
            <ExploreView />
          </MemoryRouter>
        </QueryClientProvider>
      </AuthContext.Provider>,
    );
    out[name] = html;
  } catch (e) {
    out[name] = `CRASH: ${(e as Error)?.stack ?? String(e)}`;
  }
}

// 1. Fresh first mount: no draft, no query has resolved, nothing selected.
store.clear();
mount("empty", "/explore");

// 2. Draft resume on the datasets tab: a shaped session (filters, buckets,
//    measures, sort, top, bindings) restored while the dataset list and the
//    schema query are still pending — every picker renders before its data.
{
  store.clear();
  const state = emptyExplore("orders");
  state.filters = [
    { column: "region", op: "eq", value: "eu", values: [] },
    { column: "status", op: "in", value: "", values: ["open", "closed"] },
  ];
  state.groups = [
    { column: "when", bucket: "month", binWidth: "", parse: true },
    { column: "amount", bucket: "bin", binWidth: "100", parse: false },
  ];
  state.measures = [{ fn: "avg", column: "amount", alias: "average amount" }];
  state.sort = { column: "when month", dir: "asc" };
  state.top = "10";
  store.set(
    DRAFT_KEY,
    serializeDraft({
      tab: "datasets",
      state,
      obj: { typeName: "", groupBy: [], metrics: [], filters: [], search: "" },
      bindings: { chart: "bar", x: "when month", y: ["average amount"], series: "", stacked: false },
    }),
  );
  mount("draft_datasets", "/explore");
}

// 3. Draft resume on the objects tab, object type picked, aggregate pending.
{
  store.clear();
  store.set(
    DRAFT_KEY,
    serializeDraft({
      tab: "objects",
      state: emptyExplore(),
      obj: {
        typeName: "flight",
        groupBy: ["carrier"],
        metrics: [{ op: "avg", property: "delay", alias: "avg delay" }],
        filters: [{ property: "origin", value: "SFO" }],
        search: "delayed",
      },
      bindings: { chart: "line", x: "", y: [], series: "", stacked: false },
    }),
  );
  mount("draft_objects", "/explore");
}

// 4. A stale or hand-mangled draft must degrade to a fresh screen, never a
//    crash on load (parseDraft's contract, exercised through the real mount).
store.clear();
store.set(DRAFT_KEY, "not json");
mount("draft_garbage", "/explore");
store.clear();
store.set(DRAFT_KEY, JSON.stringify({ v: 1, tab: "datasets", state: { dataset: "x" } }));
mount("draft_halfvalid", "/explore");

// 5. Edit-mode open (?dashboard=…&panel=…): the dashboard query is pending,
//    so the screen renders before the panel it will edit has arrived.
store.clear();
mount("edit_pending", "/explore?dashboard=flight_ops&panel=p1");

// 6. Source lists resolved, schema still pending: the rail maps over real
//    datasets and object types while DatasetShaping's pickers have no columns.
{
  store.clear();
  const state = emptyExplore("orders");
  store.set(
    DRAFT_KEY,
    serializeDraft({
      tab: "datasets",
      state,
      obj: { typeName: "", groupBy: [], metrics: [], filters: [], search: "" },
      bindings: { chart: "bar", x: "", y: [], series: "", stacked: false },
    }),
  );
  mount("lists_resolved", "/explore", (qc) => {
    qc.setQueryData(["datasets"], [{ name: "orders" }, { name: "flights" }]);
    qc.setQueryData(
      ["object-types"],
      [{ api_name: "flight", display_name: "Flight", properties: { delay: { type: "double" } } }],
    );
  });
}

process.stdout.write(JSON.stringify(out));
