// Mounts the REAL AnalysesView — imported from the source tree, never copied —
// in the merged surface's pre-data states and reports whether each state
// rendered or threw. The quick chart (formerly the Explore screen) lives on
// the Analyses landing page now; every invariant the old Explore mount pinned
// (crash-free pre-data mount, draft resume, garbage drafts degrading,
// edit-mode open before the panel loads) is re-asserted against the merged
// surface, plus the merge's own invariants: the one-release legacy-draft
// import, the viewer's landing gaining no authoring surface, document-draft
// restore, and the disabled unsaved-cell chaining hint.
//
// renderToStaticMarkup performs exactly React's first render pass and runs no
// effects, so every useQuery on the screen is caught at isLoading with `data`
// undefined. Any unguarded `.map` reachable at mount makes it throw, and the
// state below records the crash instead of the markup.
//
// Bundled at test time with the webapp's own esbuild (no vitest, no second
// test framework — see tests/test_charts_render.py for why).
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthContext } from "../../laurelin/ui/webapp/src/auth";
import { AnalysesView } from "../../laurelin/ui/webapp/src/views/Analyses";
import { ShapingCards } from "../../laurelin/ui/webapp/src/views/shaping/ShapingCards";
import {
  LEGACY_SCRATCH_KEY,
  SCRATCH_KEY,
  docDraftKey,
  emptyExplore,
  serializeDocDraft,
  serializeDraft,
} from "../../laurelin/ui/webapp/src/views/shaping/model";

const out: Record<string, string> = {};

// The screen persists its drafts to sessionStorage from render-adjacent code;
// node has none, so give it a real (in-memory) one the states below can seed.
const store = new Map<string, string>();
(globalThis as any).sessionStorage = {
  getItem: (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => void store.set(k, String(v)),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
};

// A signed-in user, per role. Only the fields the screens read are
// meaningful; the rest satisfy the context shape.
const RANK: Record<string, number> = { viewer: 0, editor: 1, admin: 2 };
function makeAuth(role: string) {
  return {
    loading: false,
    authRequired: true,
    setupRequired: false,
    user: { username: "ana", role },
    role,
    can: (needed: string) => RANK[role] >= RANK[needed],
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

// The view owns nested <Routes>, so mount it exactly as App.tsx does — under
// its wildcard route — or ":name" would never match a two-segment location.
const analysesApp = (
  <Routes>
    <Route path="/analyses/*" element={<AnalysesView />} />
  </Routes>
);

function mount(
  name: string,
  path: string,
  seed?: (qc: QueryClient) => void,
  role = "editor",
  el: JSX.Element = analysesApp,
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  seed?.(qc);
  try {
    const html = renderToStaticMarkup(
      <AuthContext.Provider value={makeAuth(role) as any}>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={[path]}>{el}</MemoryRouter>
        </QueryClientProvider>
      </AuthContext.Provider>,
    );
    out[name] = html;
  } catch (e) {
    out[name] = `CRASH: ${(e as Error)?.stack ?? String(e)}`;
  }
}

// A shaped quick-chart session: filters, buckets, measures, sort, top,
// bindings — the draft the resume states restore.
function shapedDraft() {
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
  return serializeDraft({
    tab: "datasets",
    state,
    obj: { typeName: "", groupBy: [], metrics: [], filters: [], search: "" },
    bindings: { chart: "bar", x: "when month", y: ["average amount"], series: "", stacked: false },
  });
}

// 1. Fresh first mount: no draft, no query has resolved, nothing selected.
store.clear();
mount("empty", "/analyses");

// 2. The viewer's landing: the list, and NO authoring surface — the quick
//    chart is an editor-only section behind the page's own role gate.
store.clear();
mount("viewer_landing", "/analyses", undefined, "viewer");

// 3. Draft resume on the datasets tab: a shaped session restored while the
//    dataset list and the schema query are still pending — every picker
//    renders before its data.
store.clear();
store.set(SCRATCH_KEY, shapedDraft());
mount("draft_datasets", "/analyses");

// 4. The same draft under the PRE-MERGE Explore key only: read once, so an
//    in-flight shaping session survives the release that merged the screens.
store.clear();
store.set(LEGACY_SCRATCH_KEY, shapedDraft());
mount("legacy_draft", "/analyses");

// 5. Draft resume on the objects tab, object type picked, aggregate pending.
store.clear();
store.set(
  SCRATCH_KEY,
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
mount("draft_objects", "/analyses");

// 6. A stale or hand-mangled draft must degrade to a fresh screen, never a
//    crash on load (parseDraft's contract, exercised through the real mount).
store.clear();
store.set(SCRATCH_KEY, "not json");
mount("draft_garbage", "/analyses");
store.clear();
store.set(SCRATCH_KEY, JSON.stringify({ v: 1, tab: "datasets", state: { dataset: "x" } }));
mount("draft_halfvalid", "/analyses");

// 7. Edit-mode open (?dashboard=…&panel=…): the dashboard query is pending,
//    so the screen renders before the panel it will edit has arrived.
store.clear();
mount("edit_pending", "/analyses?mode=chart&dashboard=flight_ops&panel=p1");

// 8. Source lists resolved, schema still pending: the rail maps over real
//    datasets and object types while the shaping pickers have no columns.
store.clear();
store.set(
  SCRATCH_KEY,
  serializeDraft({
    tab: "datasets",
    state: emptyExplore("orders"),
    obj: { typeName: "", groupBy: [], metrics: [], filters: [], search: "" },
    bindings: { chart: "bar", x: "", y: [], series: "", stacked: false },
  }),
);
mount("lists_resolved", "/analyses", (qc) => {
  qc.setQueryData(["datasets"], [{ name: "orders" }, { name: "flights" }]);
  qc.setQueryData(
    ["object-types"],
    [{ api_name: "flight", display_name: "Flight", properties: { delay: { type: "double" } } }],
  );
});

// 9. Document-draft restore + the chaining hint: an analysis with NO saved
//    cells, whose sessionStorage doc draft holds two unsaved shaping cells.
//    Both must come back after the (simulated) refresh, and cell 2's source
//    picker must list unsaved cell 1 as a DISABLED option that says what to
//    do — "only saved cells chain" used to be discoverable only by noticing
//    an absence.
store.clear();
store.set(
  docDraftKey("inv"),
  serializeDocDraft([
    {
      id: null,
      title: "Filter the orders",
      kind: "shaping",
      sql: "",
      shaping: {
        source: "ds:orders",
        filters: [{ column: "region", op: "eq", value: "eu", values: [] }],
        groups: [],
        measures: [],
        sort: null,
        top: "",
      },
      bindings: { chart: "table", x: "", y: [], series: "", stacked: false },
      width: 12,
      dirty: true,
    },
    {
      id: null,
      title: "Summarise",
      kind: "shaping",
      sql: "",
      shaping: { source: "", filters: [], groups: [], measures: [], sort: null, top: "" },
      bindings: { chart: "table", x: "", y: [], series: "", stacked: false },
      width: 12,
      dirty: true,
    },
  ]),
);
mount("doc_draft_restored", "/analyses/inv", (qc) => {
  qc.setQueryData(["analysis", "inv"], {
    name: "inv",
    title: "Investigation",
    description: "",
    cells: [],
    updated_at: "2026-08-30T00:00:00Z",
  });
});

// 10. The shared cards honor masked columns and value suggestions — mounted
//     directly, because the cell preview that feeds them is debounced out of
//     a single server render pass.
{
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  try {
    out.cards_masked_and_suggesting = renderToStaticMarkup(
      <AuthContext.Provider value={makeAuth("editor") as any}>
        <QueryClientProvider client={qc}>
          <ShapingCards
            state={{
              filters: [{ column: "region", op: "eq", value: "", values: [] }],
              groups: [],
              measures: [{ fn: "sum", column: "", alias: "total" }],
              sort: null,
              top: "",
            }}
            set={() => {}}
            columns={["region", "salary", "amount"]}
            kinds={{ region: "text", salary: "number", amount: "number" }}
            masked={new Set(["salary"])}
            measuresRequired={false}
            suggestDataset="orders"
            idPrefix="t"
          />
        </QueryClientProvider>
      </AuthContext.Provider>,
    );
  } catch (e) {
    out.cards_masked_and_suggesting = `CRASH: ${(e as Error)?.stack ?? String(e)}`;
  }
}

process.stdout.write(JSON.stringify(out));
