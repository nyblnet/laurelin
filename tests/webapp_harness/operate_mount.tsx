// Mounts the REAL Operate screens — BuildsView, SchedulesView, HealthView,
// imported from the source tree, never copied — with their queries seeded, and
// prints one JSON object of markup for tests/test_operate_ui.py.
//
// renderToStaticMarkup runs no effects, so what is pinned here is the first
// render's markup: which card the `?build=` deep link opens, that expectation
// failures are visible text rather than a tooltip, that the expander is a real
// button, and where the build-id links point. Effect-side behavior (scroll
// into view, run-now poll convergence) is pinned at source in the companion
// test file.
//
// Bundled at test time with the webapp's own esbuild (no vitest, no second
// test framework — see tests/test_charts_render.py for why).
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthContext } from "../../laurelin/ui/webapp/src/auth";
import { BuildsView } from "../../laurelin/ui/webapp/src/views/Pipeline";
import { SchedulesView } from "../../laurelin/ui/webapp/src/views/Schedules";
import { HealthView } from "../../laurelin/ui/webapp/src/views/Health";

const out: Record<string, string> = {};

// A signed-in editor: the Operate screens are editor surfaces (Schedules is
// even editor-gated on the server). Only the fields the screens read matter.
const RANK: Record<string, number> = { viewer: 0, editor: 1, admin: 2 };
const AUTH = {
  loading: false,
  authRequired: true,
  setupRequired: false,
  user: { username: "op", role: "editor" },
  role: "editor",
  can: (needed: string) => RANK["editor"] >= RANK[needed],
  multi: false,
  isSuperadmin: false,
  workspaces: [],
  activeSlug: null,
  setActiveWorkspace: () => {},
  refresh: async () => {},
  login: async () => {},
  setup: async () => {},
  logout: async () => {},
  onUnauthorized: () => {},
};

function mount(
  name: string,
  path: string,
  el: JSX.Element,
  seed: (qc: QueryClient) => void,
  auth: typeof AUTH = AUTH,
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  seed(qc);
  try {
    out[name] = renderToStaticMarkup(
      <AuthContext.Provider value={auth as any}>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={[path]}>{el}</MemoryRouter>
        </QueryClientProvider>
      </AuthContext.Provider>,
    );
  } catch (e) {
    out[name] = `CRASH: ${(e as Error)?.stack ?? String(e)}`;
  }
}

// A signed-in VIEWER: same shape, weakest role. Used to pin that the Builds
// page never renders a door into the editor-gated /pipelines surface for a
// role whose nav (correctly) hides it.
const VIEWER_AUTH = {
  ...AUTH,
  user: { username: "vi", role: "viewer" },
  role: "viewer",
  can: (needed: string) => RANK["viewer"] >= RANK[needed],
};

// ------------------------------------------------------------------ builds

const TASK_ENRICH = {
  transform_name: "enrich",
  output_dataset: "enriched",
  status: "failed",
  started_at: "2026-08-30T01:00:00Z",
  finished_at: "2026-08-30T01:00:05Z",
  failure: { code: "expectation_failed", subject: "enrich", detail_ref: "err-0123456789ab" },
  rows_written: null,
  output_version: null,
  expectations: [
    {
      expectation: "unique(tail_number)",
      passed: false,
      severity: "error",
      measured: 39,
      message: "'tail_number' must be unique — 39 row(s) violate it",
    },
  ],
};

const BUILD_FOCUSED = {
  id: "bf-aaaaaaaaaaaa",
  targets: [],
  status: "failed",
  started_at: "2026-08-30T01:00:00Z",
  finished_at: "2026-08-30T01:00:05Z",
  failure: { code: "expectation_failed", subject: "enrich" },
  tasks: [TASK_ENRICH],
};

// A second, NOT deep-linked failed build: its card stays collapsed, so its
// header must name the failed transform on its own, and its expectation
// message must not be in the markup at all.
const BUILD_COLLAPSED = {
  id: "bc-bbbbbbbbbbbb",
  targets: ["raw_loaded"],
  status: "failed",
  started_at: "2026-08-29T01:00:00Z",
  finished_at: "2026-08-29T01:00:04Z",
  failure: { code: "transform_failed", subject: "load_raw" },
  tasks: [
    {
      ...TASK_ENRICH,
      transform_name: "load_raw",
      output_dataset: "raw_loaded",
      failure: { code: "transform_failed", subject: "load_raw", detail_ref: "err-ffffffffffff" },
      expectations: [
        {
          expectation: "nonempty",
          passed: false,
          severity: "error",
          measured: 0,
          message: "COLLAPSED_CARD_SENTINEL must not render",
        },
      ],
    },
  ],
};

const TRANSFORMS = [
  { name: "agg_flow", output: "agg_out", inputs: ["enriched"], kind: "flow" },
  { name: "enrich", output: "enriched", inputs: ["raw_loaded"], kind: "python" },
];

mount(
  "builds_deeplink",
  `/builds?build=${BUILD_FOCUSED.id}`,
  <BuildsView />,
  (qc) => {
    qc.setQueryData(["lineage"], { nodes: [], edges: [] });
    qc.setQueryData(["transforms"], TRANSFORMS);
    qc.setQueryData(["builds"], [BUILD_FOCUSED, BUILD_COLLAPSED]);
  },
);

// Same page, no deep link: every card collapsed.
mount("builds_plain", "/builds", <BuildsView />, (qc) => {
  qc.setQueryData(["lineage"], { nodes: [], edges: [] });
  qc.setQueryData(["transforms"], TRANSFORMS);
  qc.setQueryData(["builds"], [BUILD_FOCUSED, BUILD_COLLAPSED]);
});

// The same page as a VIEWER, deep link open so the task table renders too:
// identical data, weakest role — transform names must be plain text, because
// /pipelines is an editor door and an in-page link reopens what the nav hides.
mount(
  "builds_viewer",
  `/builds?build=${BUILD_FOCUSED.id}`,
  <BuildsView />,
  (qc) => {
    qc.setQueryData(["lineage"], { nodes: [], edges: [] });
    qc.setQueryData(["transforms"], TRANSFORMS);
    qc.setQueryData(["builds"], [BUILD_FOCUSED, BUILD_COLLAPSED]);
  },
  VIEWER_AUTH,
);

// ---------------------------------------------------------------- schedules

mount("schedules", "/schedules", <SchedulesView />, (qc) => {
  qc.setQueryData(["transforms"], TRANSFORMS);
  qc.setQueryData(
    ["schedules"],
    [
      {
        name: "nightly",
        enabled: true,
        trigger: "cron",
        cron: "0 2 * * *",
        upstream_dataset: "",
        action: "build",
        targets: [],
        source: "",
        next_run_at: "2026-08-31T02:00:00Z",
        last_run_at: "2026-08-30T02:00:00Z",
        last_status: "succeeded",
        last_failure: null,
        last_build_id: "bld-1234567890ab",
        created_at: "2026-08-01T00:00:00Z",
        created_by: "op",
      },
    ],
  );
});

// ------------------------------------------------------------------ health

mount("health", "/health", <HealthView />, (qc) => {
  qc.setQueryData(["health", "events"], []);
  qc.setQueryData(
    ["health", "datasets"],
    [
      {
        dataset: "enriched",
        status: "failing",
        last_success_at: "2026-08-29T01:00:00Z",
        last_build_status: "failed",
        last_build_id: "bf-aaaaaaaaaaaa",
        last_failure: { code: "transform_failed", subject: "enrich" },
        failing_expectations: [],
        expected_fresh_within: null,
        schedule_overdue: false,
        last_scheduled_run_at: null,
        sync_failing: false,
        // S3's derivation: the schedule whose last run failed, named in the
        // editor-and-above detail projection exactly like overdue_schedules.
        detail: { transform: "enrich", schedule_run_failed: ["nightly"] },
      },
    ],
  );
});

process.stdout.write(JSON.stringify(out));
