// Renders the REAL shell — Layout.tsx, imported from the source tree, never
// copied — once per role, and prints one JSON object of results for
// tests/test_ui_information_architecture.py to assert against.
//
// This is the sixteenth-door harness: the sidebar markup below is exactly what
// react-dom/server emits from the shipped Layout, so a workflow that adds a
// nav item anywhere (inside NAV_GROUPS or hardcoded next to it) changes the
// per-role link lists this file reports, and the pinning test fails until the
// door is placed deliberately.
//
// Bundled at test time with the webapp's own esbuild (no vitest, no second
// test framework — see tests/test_charts_render.py for why).
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthContext } from "../../laurelin/ui/webapp/src/auth";
import {
  Layout,
  NAV_GROUPS,
  RETIRED_ROUTES,
  exploreRedirectTarget,
  flowsRedirectTarget,
  visibleNavGroups,
} from "../../laurelin/ui/webapp/src/Layout";
import { paletteItems } from "../../laurelin/ui/webapp/src/palette";

const out: Record<string, unknown> = {};

const RANK: Record<string, number> = { viewer: 0, editor: 1, admin: 2 };

function makeAuth(role: string, opts?: { superadmin?: boolean; multi?: boolean }) {
  return {
    loading: false,
    authRequired: true,
    setupRequired: false,
    user: { username: "t", role },
    role,
    can: (needed: string) => RANK[role] >= RANK[needed],
    pipelinesLocked: false,
    flowsLocked: false,
    multi: !!opts?.multi,
    isSuperadmin: !!opts?.superadmin,
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

// The full sidebar as rendered per role. The parse below reads only the
// <nav>…</nav> slice so footer buttons and the workspace switcher (real
// elements, not nav doors) stay out of the door count.
function navFor(auth: unknown) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const html = renderToStaticMarkup(
    <AuthContext.Provider value={auth as any}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/datasets"]}>
          <Layout>
            <div />
          </Layout>
        </MemoryRouter>
      </QueryClientProvider>
    </AuthContext.Provider>,
  );
  const nav = html.match(/<nav class="nav">([\s\S]*?)<\/nav>/)?.[1] ?? "";
  // Group blocks in order; inside each, the header label then its anchors.
  const groups: { label: string; items: { to: string; label: string }[] }[] = [];
  const blockRe = /<div class="nav-group-block">([\s\S]*?)<\/div>(?=<div class="nav-group-block">|$)/g;
  for (const block of nav.matchAll(blockRe)) {
    const label = block[1].match(/<div class="nav-group">([^<]*)<\/div>/)?.[1] ?? "";
    // MemoryRouter renders plain hrefs; the shipped HashRouter prefixes "#".
    const items = [...block[1].matchAll(/<a[^>]*href="#?([^"]*)"[^>]*>(?:<span[^>]*><\/span>)?([^<]*)<\/a>/g)].map(
      (m) => ({ to: m[1], label: m[2] }),
    );
    groups.push({ label, items });
  }
  // Any anchor in the nav that escaped a group block would be a hardcoded
  // door outside NAV_GROUPS — report the count so the test can require zero.
  const allAnchors = [...nav.matchAll(/<a /g)].length;
  const grouped = groups.reduce((n, g) => n + g.items.length, 0);
  return { groups, stray_anchors: allAnchors - grouped };
}

out.nav_groups_source = NAV_GROUPS;
out.retired_routes = RETIRED_ROUTES;
out.flows_redirects = {
  bare: flowsRedirectTarget("/flows", ""),
  named: flowsRedirectTarget("/flows/late_orders", ""),
  named_search: flowsRedirectTarget("/flows/late_orders", "?new=1"),
};
// Explore's deep links must land on the merged quick-chart surface with
// every param intact: the dataset-detail door and the dashboard panel-edit
// round trip both ride these.
out.explore_redirects = {
  bare: exploreRedirectTarget(""),
  dataset: exploreRedirectTarget("?dataset=tidy_flights"),
  panel_edit: exploreRedirectTarget("?dashboard=ops&panel=p1"),
};

const ROLES: Record<string, unknown> = {
  viewer: makeAuth("viewer"),
  editor: makeAuth("editor"),
  admin: makeAuth("admin"),
  superadmin_single: makeAuth("admin", { superadmin: true, multi: false }),
  superadmin_multi: makeAuth("admin", { superadmin: true, multi: true }),
};

for (const [name, auth] of Object.entries(ROLES)) {
  out[`rendered_${name}`] = navFor(auth);
  out[`filtered_${name}`] = visibleNavGroups(auth as any);
  out[`palette_${name}`] = paletteItems(auth as any);
}

process.stdout.write(JSON.stringify(out));
