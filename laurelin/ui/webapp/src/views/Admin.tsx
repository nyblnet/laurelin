// Admin view: user + role management and API token management. Admin-only —
// the router gates this route, but every mutating control also degrades
// gracefully and the server is the real authority.
//
// The page is long (a dozen sections), so it carries its own in-page table of
// contents — sticky, anchor-per-section — and a dataset-name filter that
// narrows the three per-dataset card lists (Dataset access, Row & column
// security, Classification markings) at once. Deep links carry search params
// (`#/admin?dataset=flights&section=access`), because with hash routing a
// second `#` fragment does not exist.

import { useEffect, useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { API, ApiError, api } from "../api";
import { useAuth } from "../auth";
import type { ApiToken, Role, User, WorkspaceSummary } from "../types";
import {
  Badge,
  DataTable,
  EmptyState,
  ErrorBox,
  Modal,
  PageHeader,
  Spinner,
  fmtTime,
  type Column,
} from "../ui";
import { apiPut } from "./admin/shared";
import { ApprovalsSection } from "./admin/ApprovalsSection";
import { ApprovalSettingsCard } from "./admin/ApprovalSettingsCard";
import { AlertsSection } from "./admin/AlertsSection";
import { GroupsSection } from "./admin/GroupsSection";
import { OntologyAccessSection } from "./admin/OntologyAccessSection";
import { DatasetAccessSection } from "./admin/DatasetAccessSection";
import { DataSecuritySection } from "./admin/DataSecuritySection";
import { MarkingsSection } from "./admin/MarkingsSection";
import { EnginesSection } from "./admin/EnginesSection";
import { PortabilitySection } from "./admin/PortabilitySection";
import { FileSecuritySection } from "./admin/FileSecuritySection";

const ROLES: Role[] = ["viewer", "editor", "admin"];

function errDetail(err: unknown): string {
  if (err instanceof ApiError) return err.detail || `Error ${err.status}`;
  return String((err as Error)?.message ?? err);
}

/** Small inline error box for a single control's failed mutation. */
function InlineError({ err }: { err: unknown }) {
  if (!err) return null;
  return (
    <div className="error-box" style={{ marginTop: 8 }}>
      {errDetail(err)}
    </div>
  );
}

// ----------------------------------------------------------------- contents
//
// One row per rendered section, in render order. `datasetScoped` marks the
// three card lists the dataset filter narrows.

export const ADMIN_SECTIONS: {
  id: string;
  label: string;
  datasetScoped?: boolean;
}[] = [
  { id: "users", label: "Users" },
  { id: "tokens", label: "API tokens" },
  { id: "approvals", label: "Approvals" },
  { id: "groups", label: "Groups" },
  { id: "ontology-access", label: "Ontology access" },
  { id: "access", label: "Dataset access", datasetScoped: true },
  { id: "security", label: "Row & column security", datasetScoped: true },
  { id: "markings", label: "Markings", datasetScoped: true },
  { id: "engines", label: "Delegated engines" },
  { id: "alerts", label: "Alerts" },
  { id: "files", label: "File security" },
  { id: "portability", label: "Portability" },
];

function scrollToSection(id: string) {
  document.getElementById(`admin-${id}`)?.scrollIntoView({ block: "start" });
}

function AdminToc({
  datasetFilter,
  onFilterChange,
}: {
  datasetFilter: string;
  onFilterChange: (v: string) => void;
}) {
  return (
    <nav
      aria-label="Admin sections"
      className="admin-toc"
      style={{
        position: "sticky",
        top: 0,
        zIndex: 5,
        display: "flex",
        alignItems: "center",
        gap: 4,
        flexWrap: "wrap",
        padding: "8px 0",
        marginBottom: 16,
        background: "var(--bg-0)",
        borderBottom: "1px solid var(--border)",
      }}
    >
      {ADMIN_SECTIONS.map((s) => (
        <button
          key={s.id}
          type="button"
          className="small"
          onClick={() => scrollToSection(s.id)}
        >
          {s.label}
        </button>
      ))}
      <span style={{ flex: 1 }} />
      <div className="field" style={{ margin: 0 }}>
        <label htmlFor="admin-dataset-filter" className="faint" style={{ fontSize: 11 }}>
          Filter dataset cards
        </label>
        <input
          id="admin-dataset-filter"
          className="mono"
          placeholder="dataset name"
          value={datasetFilter}
          onChange={(e) => onFilterChange(e.target.value)}
          style={{ width: 180 }}
        />
      </div>
    </nav>
  );
}

// --------------------------------------------------------------- users

function CreateUserPanel({ onCreated }: { onCreated: () => void }) {
  const auth = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [wsSlug, setWsSlug] = useState("");

  // In multi-workspace mode a fresh account is a door to nowhere until it is
  // a member of some workspace — so membership is offered at creation rather
  // than as a second trip through Workspaces → Manage members. Listing
  // workspaces is a control-plane (superadmin) call, hence the gate.
  const offerMembership = auth.multi && auth.isSuperadmin;
  const wsQuery = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => api.get<WorkspaceSummary[]>(`${API}/workspaces`),
    enabled: offerMembership,
  });

  const create = useMutation({
    mutationFn: async () => {
      const user = await api.post<User>(`${API}/users`, { username, password, role });
      if (offerMembership && wsSlug) {
        await apiPut<unknown>(
          `/workspaces/${encodeURIComponent(wsSlug)}/members`,
          { username: user.username, role },
        );
      }
      return user;
    },
    onSuccess: () => {
      setUsername("");
      setPassword("");
      setRole("viewer");
      setWsSlug("");
      onCreated();
    },
  });

  const tooShort = password.length > 0 && password.length < 8;
  const canSubmit =
    username.trim().length > 0 && password.length >= 8 && !create.isPending;

  return (
    <div className="card" style={{ marginTop: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 12 }}>Create user</div>
      <div className="field">
        <label htmlFor="admin-new-username">Username</label>
        <input
          id="admin-new-username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="jdoe"
          autoComplete="off"
        />
      </div>
      <div className="field">
        <label htmlFor="admin-new-password">Password</label>
        <input
          id="admin-new-password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="new-password"
        />
        <div className={`hint${tooShort ? " bad" : ""}`}>
          At least 8 characters.
        </div>
      </div>
      <div className="field">
        <label htmlFor="admin-new-role">Role</label>
        <select
          id="admin-new-role"
          value={role}
          onChange={(e) => setRole(e.target.value as Role)}
        >
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      </div>
      {offerMembership && (
        <div className="field">
          <label htmlFor="admin-new-workspace">Workspace membership</label>
          <select
            id="admin-new-workspace"
            value={wsSlug}
            onChange={(e) => setWsSlug(e.target.value)}
          >
            <option value="">— none yet —</option>
            {(wsQuery.data ?? []).map((w) => (
              <option key={w.slug} value={w.slug}>
                {w.slug}
              </option>
            ))}
          </select>
          <div className="hint">
            Adds the new user to this workspace with the same role. Without a
            membership they cannot enter any workspace until one is granted in
            Workspaces → Manage members.
          </div>
        </div>
      )}
      <button
        className="button primary"
        disabled={!canSubmit}
        onClick={() => create.mutate()}
      >
        {create.isPending ? "Creating…" : "Create user"}
      </button>
      <InlineError err={create.error} />
    </div>
  );
}

function UsersSection({ me }: { me: User | null }) {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["users"] });

  const usersQuery = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get<User[]>(`${API}/users`),
  });

  // Keyed by username so the failing row shows its own error.
  const patchRole = useMutation({
    mutationFn: (v: { username: string; role: Role }) =>
      api.patch<User>(`${API}/users/${encodeURIComponent(v.username)}`, {
        role: v.role,
      }),
    onSuccess: invalidate,
  });

  const patchDisabled = useMutation({
    mutationFn: (v: { username: string; disabled: boolean }) =>
      api.patch<User>(`${API}/users/${encodeURIComponent(v.username)}`, {
        disabled: v.disabled,
      }),
    onSuccess: invalidate,
  });

  const removeUser = useMutation({
    mutationFn: (username: string) =>
      api.del<{ ok: boolean }>(`${API}/users/${encodeURIComponent(username)}`),
    onSuccess: invalidate,
  });

  const isSelf = (u: User) => !!me && u.username === me.username;

  const columns: Column<User>[] = [
    {
      label: "Username",
      className: "mono",
      render: (u) => (
        <span>
          {u.username}
          {isSelf(u) && <span className="faint"> (you)</span>}
        </span>
      ),
    },
    {
      label: "Role",
      render: (u) => (
        <select
          value={u.role}
          disabled={isSelf(u) || patchRole.isPending}
          onChange={(e) =>
            patchRole.mutate({ username: u.username, role: e.target.value as Role })
          }
          style={{ width: "auto" }}
        >
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      ),
    },
    {
      label: "Status",
      render: (u) => (
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Badge tone={u.disabled ? "red" : "green"}>
            {u.disabled ? "disabled" : "active"}
          </Badge>
          {!isSelf(u) && (
            <button
              className="button small"
              disabled={patchDisabled.isPending}
              onClick={() =>
                patchDisabled.mutate({
                  username: u.username,
                  disabled: !u.disabled,
                })
              }
            >
              {u.disabled ? "Enable" : "Disable"}
            </button>
          )}
        </div>
      ),
    },
    { label: "Created", render: (u) => fmtTime(u.created_at) },
    {
      label: "Actions",
      render: (u) =>
        isSelf(u) ? (
          <span className="faint">—</span>
        ) : (
          <button
            className="button danger small"
            disabled={removeUser.isPending}
            onClick={() => {
              if (
                window.confirm(
                  `Delete user "${u.username}"? This cannot be undone.`,
                )
              ) {
                removeUser.mutate(u.username);
              }
            }}
          >
            Delete
          </button>
        ),
    },
  ];

  return (
    <section style={{ marginBottom: 32 }}>
      <h2 style={{ fontSize: 15, marginBottom: 12 }}>Users</h2>

      {(patchRole.error || patchDisabled.error || removeUser.error) && (
        <InlineError
          err={patchRole.error ?? patchDisabled.error ?? removeUser.error}
        />
      )}

      {usersQuery.isLoading ? (
        <Spinner />
      ) : usersQuery.error ? (
        <ErrorBox error={usersQuery.error} onRetry={() => usersQuery.refetch()} />
      ) : usersQuery.data && usersQuery.data.length > 0 ? (
        <DataTable
          columns={columns}
          rows={usersQuery.data}
          rowKey={(u) => u.id}
        />
      ) : (
        <EmptyState>No users.</EmptyState>
      )}

      <CreateUserPanel onCreated={invalidate} />
    </section>
  );
}

// --------------------------------------------------------------- tokens

function TokenCreatedModal({
  token,
  onClose,
}: {
  token: string;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    let ok = false;
    try {
      await navigator.clipboard.writeText(token);
      ok = true;
    } catch {
      // Fallback for insecure contexts / older browsers.
      const ta = document.createElement("textarea");
      ta.value = token;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        ok = document.execCommand("copy");
      } catch {
        ok = false;
      }
      document.body.removeChild(ta);
    }
    if (ok) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <Modal label="API token created" onClose={onClose}>
      <h2 style={{ fontSize: 16, marginBottom: 8 }}>API token created</h2>
      <div className="token-value">{token}</div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <button className="button" onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <div className="hint bad" style={{ marginTop: 12 }}>
        Copy it now — you won't see this token again.
      </div>
      <div style={{ marginTop: 16 }}>
        <button className="button primary" onClick={onClose}>
          Done
        </button>
      </div>
    </Modal>
  );
}

function CreateTokenPanel({ onCreated }: { onCreated: (token: string) => void }) {
  const [name, setName] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api.post<{ id: string; name: string; token: string }>(`${API}/tokens`, {
        name,
      }),
    onSuccess: (res) => {
      setName("");
      onCreated(res.token);
    },
  });

  const canSubmit = name.trim().length > 0 && !create.isPending;

  return (
    <div className="card" style={{ marginTop: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 12 }}>Create token</div>
      <div className="field">
        <label htmlFor="admin-new-token-name">Name</label>
        <input
          id="admin-new-token-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="ci-pipeline"
          autoComplete="off"
        />
      </div>
      <button
        className="button primary"
        disabled={!canSubmit}
        onClick={() => create.mutate()}
      >
        {create.isPending ? "Creating…" : "Create token"}
      </button>
      <InlineError err={create.error} />
    </div>
  );
}

function TokensSection() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["tokens"] });
  const [newToken, setNewToken] = useState<string | null>(null);

  const tokensQuery = useQuery({
    queryKey: ["tokens"],
    queryFn: () => api.get<ApiToken[]>(`${API}/tokens`),
  });

  const revoke = useMutation({
    mutationFn: (id: string) =>
      api.del<{ ok: boolean }>(`${API}/tokens/${encodeURIComponent(id)}`),
    onSuccess: invalidate,
  });

  const columns: Column<ApiToken>[] = [
    { label: "Name", render: (t) => t.name },
    {
      label: "User",
      render: (t) =>
        t.username ? (
          <span className="mono">{t.username}</span>
        ) : (
          <span className="faint">—</span>
        ),
    },
    { label: "Created", render: (t) => fmtTime(t.created_at) },
    { label: "Last used", render: (t) => fmtTime(t.last_used_at) },
    {
      label: "Actions",
      render: (t) => (
        <button
          className="button danger small"
          disabled={revoke.isPending}
          onClick={() => {
            if (window.confirm(`Revoke token "${t.name}"?`)) {
              revoke.mutate(t.id);
            }
          }}
        >
          Revoke
        </button>
      ),
    },
  ];

  return (
    <section>
      <h2 style={{ fontSize: 15, marginBottom: 12 }}>API tokens</h2>

      {revoke.error && <InlineError err={revoke.error} />}

      {tokensQuery.isLoading ? (
        <Spinner />
      ) : tokensQuery.error ? (
        <ErrorBox error={tokensQuery.error} onRetry={() => tokensQuery.refetch()} />
      ) : tokensQuery.data && tokensQuery.data.length > 0 ? (
        <DataTable
          columns={columns}
          rows={tokensQuery.data}
          rowKey={(t) => t.id}
        />
      ) : (
        <EmptyState>No API tokens.</EmptyState>
      )}

      <CreateTokenPanel
        onCreated={(token) => {
          invalidate();
          setNewToken(token);
        }}
      />

      {newToken && (
        <TokenCreatedModal token={newToken} onClose={() => setNewToken(null)} />
      )}
    </section>
  );
}

// --------------------------------------------------------------- view

export function AdminView() {
  const auth = useAuth();
  const me = auth.user;
  const [searchParams] = useSearchParams();

  // Deep links: `?dataset=flights` pre-fills the card filter; `?section=access`
  // scrolls there. A dataset link with no section means "the access question",
  // so it lands on Dataset access.
  const [datasetFilter, setDatasetFilter] = useState(
    () => searchParams.get("dataset") ?? "",
  );
  useEffect(() => {
    const section =
      searchParams.get("section") ??
      (searchParams.get("dataset") ? "access" : null);
    if (!section) return;
    // After first paint, so the sections exist to scroll to.
    const t = window.setTimeout(() => scrollToSection(section), 50);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div>
      <PageHeader
        title="Admin"
        subtitle="Users, roles, access control, and this workspace's portability."
      />

      {!auth.can("admin") ? (
        <ErrorBox error={new ApiError(403, "Admin access required")} />
      ) : (
        <>
          <AdminToc datasetFilter={datasetFilter} onFilterChange={setDatasetFilter} />
          <div id="admin-users">
            <UsersSection me={me} />
          </div>
          <div id="admin-tokens">
            <TokensSection />
          </div>
          {/* The inbox sits above the sections whose writes it gates, with its
              mode toggle attached, so "why did my grant queue?" has its answer
              one scroll up. */}
          <div id="admin-approvals">
            <ApprovalsSection />
            <ApprovalSettingsCard />
          </div>
          <div id="admin-groups">
            <GroupsSection />
          </div>
          <div id="admin-ontology-access">
            <OntologyAccessSection />
          </div>
          <div id="admin-access">
            <DatasetAccessSection filter={datasetFilter} />
          </div>
          <div id="admin-security">
            <DataSecuritySection filter={datasetFilter} />
          </div>
          <div id="admin-markings">
            <MarkingsSection filter={datasetFilter} />
          </div>
          <div id="admin-engines">
            <EnginesSection />
          </div>
          <div id="admin-alerts">
            <AlertsSection />
          </div>
          <div id="admin-files">
            <FileSecuritySection />
          </div>
          <div id="admin-portability">
            <PortabilitySection />
          </div>
        </>
      )}
    </div>
  );
}
