// Admin view: user + role management and API token management. Admin-only —
// the router gates this route, but every mutating control also degrades
// gracefully and the server is the real authority.

import { useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { API, ApiError, api } from "../api";
import { useAuth } from "../auth";
import type { ApiToken, Role, User } from "../types";
import {
  Badge,
  DataTable,
  EmptyState,
  ErrorBox,
  PageHeader,
  Spinner,
  fmtTime,
  type Column,
} from "../ui";
import { GroupsSection } from "./admin/GroupsSection";
import { OntologyAccessSection } from "./admin/OntologyAccessSection";
import { DatasetAccessSection } from "./admin/DatasetAccessSection";
import { DataSecuritySection } from "./admin/DataSecuritySection";

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

// --------------------------------------------------------------- users

function CreateUserPanel({ onCreated }: { onCreated: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("viewer");

  const create = useMutation({
    mutationFn: () =>
      api.post<User>(`${API}/users`, { username, password, role }),
    onSuccess: () => {
      setUsername("");
      setPassword("");
      setRole("viewer");
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
        <label>Username</label>
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="jdoe"
          autoComplete="off"
        />
      </div>
      <div className="field">
        <label>Password</label>
        <input
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
        <label>Role</label>
        <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      </div>
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
        <ErrorBox error={usersQuery.error} />
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
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
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
      </div>
    </div>
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
        <label>Name</label>
        <input
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
        <ErrorBox error={tokensQuery.error} />
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

  return (
    <div>
      <PageHeader
        title="Admin"
        subtitle="Users, roles and API tokens for this workspace."
      />

      {!auth.can("admin") ? (
        <ErrorBox error={new ApiError(403, "Admin access required")} />
      ) : (
        <>
          <UsersSection me={me} />
          <TokensSection />
          <GroupsSection />
          <OntologyAccessSection />
          <DatasetAccessSection />
          <DataSecuritySection />
        </>
      )}
    </div>
  );
}
