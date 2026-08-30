// Manage the members of a single workspace: list current members with their
// role, add a global user with a chosen role, change a role, or remove one.
// All calls are superadmin control-plane (workspace-independent).

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../../api";
import type { Role, User, WorkspaceMember, WorkspaceSummary } from "../../types";
import { Badge, EmptyState, Modal, Spinner } from "../../ui";
import { InlineError, apiPut } from "../admin/shared";

const ROLES: Role[] = ["viewer", "editor", "admin"];

function roleTone(role: Role): "gold" | "blue" | "neutral" {
  if (role === "admin") return "gold";
  if (role === "editor") return "blue";
  return "neutral";
}

export function ManageMembersModal({
  workspace,
  onClose,
}: {
  workspace: WorkspaceSummary;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const slug = workspace.slug;
  const membersKey = ["ws-members", slug];
  const invalidate = () => qc.invalidateQueries({ queryKey: membersKey });

  const membersQuery = useQuery({
    queryKey: membersKey,
    queryFn: () =>
      api.get<WorkspaceMember[]>(
        `${API}/workspaces/${encodeURIComponent(slug)}/members`,
      ),
  });

  // Global users to pick a new member from. Deduped with the Admin view's key.
  const usersQuery = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get<User[]>(`${API}/users`),
  });

  const [newUser, setNewUser] = useState("");
  const [newRole, setNewRole] = useState<Role>("viewer");

  const members = membersQuery.data ?? [];
  const memberNames = useMemo(
    () => new Set(members.map((m) => m.username)),
    [members],
  );
  const users = usersQuery.data ?? [];
  const available = users.filter((u) => !memberNames.has(u.username));

  const upsert = useMutation({
    mutationFn: (vars: { username: string; role: Role }) =>
      apiPut<unknown>(
        `/workspaces/${encodeURIComponent(slug)}/members`,
        { username: vars.username, role: vars.role },
      ),
    onSuccess: (_d, vars) => {
      invalidate();
      // Clear the add form only when it was an add (fresh username).
      if (!memberNames.has(vars.username)) {
        setNewUser("");
        setNewRole("viewer");
      }
    },
  });

  const remove = useMutation({
    mutationFn: (username: string) =>
      api.del<{ ok: boolean }>(
        `${API}/workspaces/${encodeURIComponent(slug)}/members/${encodeURIComponent(username)}`,
      ),
    onSuccess: invalidate,
  });

  const canAdd = newUser.trim().length > 0 && !upsert.isPending;

  return (
    <Modal label={`Members of ${workspace.slug}`} onClose={onClose} width={560}>
        <h2 style={{ fontSize: 16, marginBottom: 4 }}>
          Members of <span className="mono">{workspace.slug}</span>
        </h2>
        <div className="dim" style={{ marginBottom: 16, fontSize: 12.5 }}>
          Members get the chosen role inside this workspace. Superadmins can
          access every workspace regardless of membership.
        </div>

        {membersQuery.isLoading ? (
          <Spinner />
        ) : membersQuery.error ? (
          <InlineError err={membersQuery.error} />
        ) : members.length === 0 ? (
          <EmptyState>No members yet.</EmptyState>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>User</th>
                  <th>Role</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {members.map((m) => (
                  <tr key={m.username}>
                    <td className="mono">{m.username}</td>
                    <td>
                      <select
                        value={m.role}
                        disabled={upsert.isPending}
                        onChange={(e) =>
                          upsert.mutate({
                            username: m.username,
                            role: e.target.value as Role,
                          })
                        }
                        style={{ width: "auto" }}
                      >
                        {ROLES.map((r) => (
                          <option key={r} value={r}>
                            {r}
                          </option>
                        ))}
                      </select>{" "}
                      <Badge tone={roleTone(m.role)}>{m.role}</Badge>
                    </td>
                    <td>
                      <button
                        className="button danger small"
                        disabled={remove.isPending}
                        onClick={() => {
                          if (
                            window.confirm(
                              `Remove ${m.username} from workspace "${slug}"?`,
                            )
                          ) {
                            remove.mutate(m.username);
                          }
                        }}
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <InlineError err={remove.error} />

        {/* ---- add a member ---- */}
        <div className="card" style={{ marginTop: 16 }}>
          <div style={{ fontWeight: 600, marginBottom: 12 }}>Add member</div>
          <div
            style={{
              display: "flex",
              gap: 8,
              alignItems: "flex-end",
              flexWrap: "wrap",
            }}
          >
            <div className="field" style={{ margin: 0, flex: "1 1 220px" }}>
              <label>User</label>
              <select
                value={newUser}
                onChange={(e) => setNewUser(e.target.value)}
              >
                <option value="">Select a user…</option>
                {available.map((u) => (
                  <option key={u.id} value={u.username}>
                    {u.username}
                    {u.disabled ? " (disabled)" : ""}
                  </option>
                ))}
              </select>
            </div>
            <div className="field" style={{ margin: 0, flex: "0 0 140px" }}>
              <label>Role</label>
              <select
                value={newRole}
                onChange={(e) => setNewRole(e.target.value as Role)}
              >
                {ROLES.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </div>
            <button
              className="button primary"
              disabled={!canAdd}
              onClick={() =>
                upsert.mutate({ username: newUser.trim(), role: newRole })
              }
            >
              {upsert.isPending ? "Saving…" : "Add"}
            </button>
          </div>
          {available.length === 0 && users.length > 0 && (
            <div className="hint" style={{ marginTop: 8 }}>
              Every global user is already a member.
            </div>
          )}
          {/* Accounts are not created here — say where they are, so "add a
              person who has no account yet" is one hop, not a search. */}
          <div className="hint" style={{ marginTop: 8 }}>
            Need a new account? <a href="#/admin?section=users">Create the user
            in Admin → Users</a>, then add them here.
          </div>
          {usersQuery.error && <InlineError err={usersQuery.error} />}
          <InlineError err={upsert.error} />
        </div>

        <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
          <button className="button" onClick={onClose}>
            Done
          </button>
        </div>
    </Modal>
  );
}
