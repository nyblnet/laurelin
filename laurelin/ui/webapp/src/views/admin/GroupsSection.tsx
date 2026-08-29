// Admin > Groups: named user sets, admin-managed, usable as permission grant
// subjects. Create / delete groups and edit their membership.

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../../api";
import type { Group, User } from "../../types";
import { Badge, EmptyState, ErrorBox, Spinner } from "../../ui";
import { InlineError, QueuedBanner, apiPut } from "./shared";

const NAME_RE = /^[a-z0-9][a-z0-9_.-]{1,31}$/;

// --------------------------------------------------------------- create

function CreateGroupPanel({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");

  const create = useMutation({
    mutationFn: () => api.post<Group>(`${API}/groups`, { name: name.trim() }),
    onSuccess: () => {
      setName("");
      onCreated();
    },
  });

  const trimmed = name.trim();
  const valid = NAME_RE.test(trimmed);
  const showBad = trimmed.length > 0 && !valid;
  const canSubmit = valid && !create.isPending;

  return (
    <div className="card" style={{ marginTop: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 12 }}>Create group</div>
      <div className="field">
        <label>Name</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="data-science"
          autoComplete="off"
          onKeyDown={(e) => {
            if (e.key === "Enter" && canSubmit) create.mutate();
          }}
        />
        <div className={`hint${showBad ? " bad" : ""}`}>
          Lowercase letters, digits, and <span className="mono">_ . -</span>; 2–32
          characters.
        </div>
      </div>
      <button
        className="button primary"
        disabled={!canSubmit}
        onClick={() => create.mutate()}
      >
        {create.isPending ? "Creating…" : "Create group"}
      </button>
      <InlineError err={create.error} />
    </div>
  );
}

// --------------------------------------------------------------- members

function ManageMembersPanel({
  group,
  users,
  onSaved,
  onClose,
}: {
  group: Group;
  users: User[];
  onSaved: () => void;
  onClose: () => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(group.members),
  );

  // Re-seed when the target group changes underneath us.
  useEffect(() => {
    setSelected(new Set(group.members));
  }, [group.name, group.members]);

  const save = useMutation({
    mutationFn: () =>
      apiPut<Group>(`/groups/${encodeURIComponent(group.name)}/members`, {
        members: Array.from(selected),
      }),
    onSuccess: () => onSaved(),
  });

  const toggle = (username: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(username)) next.delete(username);
      else next.add(username);
      return next;
    });
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: 480 }}
      >
        <h2 style={{ fontSize: 16, marginBottom: 4 }}>
          Members of <span className="mono">{group.name}</span>
        </h2>
        <div className="dim" style={{ marginBottom: 12, fontSize: 12.5 }}>
          {selected.size} of {users.length} user
          {users.length === 1 ? "" : "s"} selected.
        </div>

        {users.length === 0 ? (
          <EmptyState>No users to add.</EmptyState>
        ) : (
          <div
            style={{
              maxHeight: 320,
              overflowY: "auto",
              display: "flex",
              flexDirection: "column",
              gap: 4,
            }}
          >
            {users.map((u) => (
              <label
                key={u.id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  cursor: "pointer",
                }}
              >
                <input
                  type="checkbox"
                  checked={selected.has(u.username)}
                  onChange={() => toggle(u.username)}
                  style={{ width: "auto" }}
                />
                <span className="mono">{u.username}</span>
                {u.disabled && <span className="faint">(disabled)</span>}
              </label>
            ))}
          </div>
        )}

        <InlineError err={save.error} />
        <QueuedBanner res={save.data} />

        <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
          <button
            className="button primary"
            disabled={save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending ? "Saving…" : "Save members"}
          </button>
          <button className="button" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------- section

export function GroupsSection() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["groups"] });
  const [managing, setManaging] = useState<string | null>(null);

  const groupsQuery = useQuery({
    queryKey: ["groups"],
    queryFn: () => api.get<Group[]>(`${API}/groups`),
  });

  // Users are also fetched by the Users section; react-query dedupes by key.
  const usersQuery = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get<User[]>(`${API}/users`),
  });

  const removeGroup = useMutation({
    mutationFn: (name: string) =>
      api.del<{ ok: boolean }>(`${API}/groups/${encodeURIComponent(name)}`),
    onSuccess: invalidate,
  });

  const groups = groupsQuery.data ?? [];
  const managingGroup = groups.find((g) => g.name === managing) ?? null;

  return (
    <section style={{ marginBottom: 32 }}>
      <h2 style={{ fontSize: 15, marginBottom: 12 }}>Groups</h2>
      <div className="subtitle" style={{ marginTop: -6, marginBottom: 12 }}>
        Named sets of users. Reference a group as the subject of an ontology
        access grant below.
      </div>

      {removeGroup.error && <InlineError err={removeGroup.error} />}

      {groupsQuery.isLoading ? (
        <Spinner />
      ) : groupsQuery.error ? (
        <ErrorBox error={groupsQuery.error} />
      ) : groups.length > 0 ? (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Group</th>
                <th>Members</th>
                <th>Users</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((g) => (
                <tr key={g.name}>
                  <td className="mono">{g.name}</td>
                  <td>
                    <Badge tone="neutral">{g.members.length}</Badge>
                  </td>
                  <td>
                    {g.members.length === 0 ? (
                      <span className="faint">—</span>
                    ) : (
                      <div
                        style={{ display: "flex", flexWrap: "wrap", gap: 4 }}
                      >
                        {g.members.map((m) => (
                          <span key={m} className="badge badge-neutral mono">
                            {m}
                          </span>
                        ))}
                      </div>
                    )}
                  </td>
                  <td>
                    <div style={{ display: "flex", gap: 8 }}>
                      <button
                        className="button small"
                        onClick={() => setManaging(g.name)}
                      >
                        Manage members
                      </button>
                      <button
                        className="button danger small"
                        disabled={removeGroup.isPending}
                        onClick={() => {
                          if (
                            window.confirm(
                              `Delete group "${g.name}"? This cannot be undone.`,
                            )
                          ) {
                            removeGroup.mutate(g.name);
                          }
                        }}
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState>No groups.</EmptyState>
      )}

      <CreateGroupPanel onCreated={invalidate} />

      {managingGroup && (
        <ManageMembersPanel
          group={managingGroup}
          users={usersQuery.data ?? []}
          onSaved={() => {
            invalidate();
            setManaging(null);
          }}
          onClose={() => setManaging(null)}
        />
      )}
    </section>
  );
}
