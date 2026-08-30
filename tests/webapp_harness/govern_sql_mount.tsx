// Mounts the REAL Govern + SQL + Workspaces surfaces — imported from the
// source tree, never copied — and prints one JSON object of results for
// tests/test_govern_sql_ui.py to assert against.
//
// What this pins, per the copy/structure specs:
//   E2  — the Audit empty state never claims an absence the caller cannot
//         verify (editor sees the withheld sentence, admin the real one);
//   A1  — the audit filter bar exists and sends the S4 params;
//         sign-in noise is collapsed by default and recoverable;
//   A3  — every Dataset-access card offers "Effective access" (the one-stop
//         "who can see this dataset" answer);
//   A2  — the Admin page renders its in-page TOC with per-section anchors;
//   F9/L4 — the Workbench comment-only refusal helper and the tolerant
//         draft parser behave;
//   K1  — Manage members signposts Admin → Users for account creation.
//
// Bundled at test time with the webapp's own esbuild (no vitest, no second
// test framework — see tests/test_charts_render.py for why).
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthContext } from "../../laurelin/ui/webapp/src/auth";
import { AuditView } from "../../laurelin/ui/webapp/src/views/Audit";
import { AdminView, ADMIN_SECTIONS } from "../../laurelin/ui/webapp/src/views/Admin";
import { DatasetAccessSection } from "../../laurelin/ui/webapp/src/views/admin/DatasetAccessSection";
import { ManageMembersModal } from "../../laurelin/ui/webapp/src/views/workspaces/ManageMembers";
import {
  WorkbenchView,
  parseWorkbenchDraft,
  sqlHasExecutableStatement,
  COMMENT_ONLY_REFUSAL,
  WORKBENCH_DRAFT_KEY,
} from "../../laurelin/ui/webapp/src/views/Workbench";

const out: Record<string, unknown> = {};

// Drafts ride sessionStorage; node has none, so give it a real in-memory one.
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

// ------------------------------------------------------------------- audit

// The default (filterless) audit query key: ["audit", path, "limit=200"].
const EMPTY_AUDIT = (qc: QueryClient) =>
  qc.setQueryData(["audit", "/audit", "limit=200"], []);

out.audit_empty_editor = mount(<AuditView />, "editor", "/audit", EMPTY_AUDIT);
out.audit_empty_admin = mount(<AuditView />, "admin", "/audit", EMPTY_AUDIT);

const AUDIT_ROWS = [
  {
    id: 1,
    timestamp: "2026-08-29T10:00:00Z",
    actor: "alice",
    action: "login_succeeded",
    details: {},
  },
  {
    id: 2,
    timestamp: "2026-08-29T10:01:00Z",
    actor: "alice",
    action: "dataset_created",
    details: { dataset: "flights" },
  },
  {
    id: 3,
    timestamp: "2026-08-29T10:02:00Z",
    actor: "bob",
    action: "login_failed",
    details: {},
  },
];
out.audit_rows_admin = mount(<AuditView />, "admin", "/audit", (qc) =>
  qc.setQueryData(["audit", "/audit", "limit=200"], AUDIT_ROWS),
);

// ------------------------------------------------------------------- admin

out.admin_page = mount(<AdminView />, "admin", "/admin");
out.admin_sections = ADMIN_SECTIONS.map((s) => s.id);

// -------------------------------------------------------- effective access

out.dataset_access = mount(
  <DatasetAccessSection />, "admin", "/admin",
  (qc) => {
    qc.setQueryData(["dataset-permissions"], [
      { dataset: "flights", grants: [] },
      {
        dataset: "revenue",
        grants: [
          { subject_kind: "group", subject: "finance", can_view: true, can_edit: false },
        ],
      },
    ]);
    qc.setQueryData(["groups"], [{ name: "finance", members: ["alice"] }]);
    qc.setQueryData(["users"], [
      { id: "1", username: "alice", role: "viewer", disabled: false, created_at: "" },
    ]);
  },
);

// --------------------------------------------------------------- workbench

out.workbench_empty = mount(
  <WorkbenchView />, "editor", "/workbench",
  (qc) => qc.setQueryData(["datasets"], []),
);

out.comment_only = {
  refusal: COMMENT_ONLY_REFUSAL,
  comment: sqlHasExecutableStatement("-- just a comment\n"),
  block: sqlHasExecutableStatement("/* block\ncomment */"),
  semicolons: sqlHasExecutableStatement("  ;;  \n"),
  comment_then_sql: sqlHasExecutableStatement("-- c\nSELECT 1"),
  plain: sqlHasExecutableStatement("SELECT 1"),
};

const goodDraft = {
  sql: "SELECT * FROM flights",
  view: "bar",
  result: { columns: ["a"], rows: [{ a: 1 }], row_count: 1, truncated: false },
};
out.draft = {
  key: WORKBENCH_DRAFT_KEY,
  roundtrip: parseWorkbenchDraft(JSON.stringify(goodDraft)),
  garbage: parseWorkbenchDraft("not json {"),
  wrong_shape: parseWorkbenchDraft(JSON.stringify({ sql: 5 })),
  array: parseWorkbenchDraft(JSON.stringify([1, 2])),
  null_input: parseWorkbenchDraft(null),
  bad_view_degrades: parseWorkbenchDraft(
    JSON.stringify({ sql: "SELECT 1", view: "hologram" }),
  ),
};

// -------------------------------------------------------------- workspaces

out.manage_members = mount(
  <ManageMembersModal
    workspace={{ slug: "research", name: "Research", description: "", members: 1, created_at: "" } as any}
    onClose={() => {}}
  />,
  "admin",
  "/workspaces",
  (qc) => {
    qc.setQueryData(["ws-members", "research"], [{ username: "alice", role: "admin" }]);
    qc.setQueryData(["users"], [
      { id: "1", username: "alice", role: "admin", disabled: false, created_at: "" },
    ]);
  },
);

process.stdout.write(JSON.stringify(out));
