// Admin > Classification markings: mandatory-access-control labels that
// propagate through lineage.
//
// A marking (e.g. "pii", "confidential") is a classification label. Markings
// PROPAGATE: a derived dataset inherits its inputs' markings on build, so a
// dataset's *effective* markings are its own explicit markings plus everything
// inherited upstream. A non-admin user must hold clearance for EVERY effective
// marking on a dataset to see it; admins bypass.

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../../api";
import type {
  DatasetMarkingsEntry,
  Marking,
  User,
  UserClearances,
} from "../../types";
import { Badge, EmptyState, ErrorBox, Spinner } from "../../ui";
import { InlineError, QueuedBanner, apiPut } from "./shared";

// Server rule: ^[a-z0-9][a-z0-9_.-]{0,47}$ (name is lowercased server-side).
const NAME_RE = /^[a-z0-9][a-z0-9_.-]{0,47}$/;

// --------------------------------------------------------------- markings

function CreateMarkingForm({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api.post<Marking>(`${API}/markings`, {
        name: name.trim().toLowerCase(),
        description: description.trim() || undefined,
      }),
    onSuccess: () => {
      setName("");
      setDescription("");
      onCreated();
    },
  });

  const normalized = name.trim().toLowerCase();
  const nameBad = normalized.length > 0 && !NAME_RE.test(normalized);
  const canSubmit = NAME_RE.test(normalized) && !create.isPending;

  return (
    <div className="card" style={{ marginTop: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 12 }}>Create marking</div>
      <div className="field">
        <label>Name</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="pii"
          autoComplete="off"
          style={{ maxWidth: 260 }}
        />
        <div className={`hint${nameBad ? " bad" : ""}`}>
          Lowercase; starts with a letter or digit; then letters, digits,{" "}
          <span className="mono">_ . -</span>; up to 48 characters.
        </div>
      </div>
      <div className="field">
        <label>Description</label>
        <input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Personally identifiable information"
          autoComplete="off"
        />
      </div>
      <button
        className="button primary"
        disabled={!canSubmit}
        onClick={() => create.mutate()}
      >
        {create.isPending ? "Creating…" : "Create marking"}
      </button>
      <InlineError err={create.error} />
    </div>
  );
}

function MarkingsList({
  markings,
  onChanged,
}: {
  markings: Marking[];
  onChanged: () => void;
}) {
  const remove = useMutation({
    mutationFn: (name: string) =>
      api.del<{ ok: boolean }>(`${API}/markings/${encodeURIComponent(name)}`),
    onSuccess: onChanged,
  });

  return (
    <div>
      {remove.error && <InlineError err={remove.error} />}
      <QueuedBanner res={remove.data} />

      {markings.length > 0 ? (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Description</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {markings.map((m) => (
                <tr key={m.name}>
                  <td>
                    <Badge tone="gold">{m.name}</Badge>
                  </td>
                  <td>
                    {m.description ? (
                      m.description
                    ) : (
                      <span className="faint">—</span>
                    )}
                  </td>
                  <td>
                    <button
                      className="button danger small"
                      disabled={remove.isPending}
                      onClick={() => {
                        if (
                          window.confirm(
                            `Delete marking "${m.name}"? It will be removed from all datasets and clearances.`,
                          )
                        ) {
                          remove.mutate(m.name);
                        }
                      }}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState>No markings defined.</EmptyState>
      )}

      <CreateMarkingForm onCreated={onChanged} />
    </div>
  );
}

// ---------------------------------------------------------- dataset markings

function sameSet(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const s = new Set(a);
  return b.every((x) => s.has(x));
}

function DatasetMarkingsCard({
  entry,
  markings,
  onSaved,
}: {
  entry: DatasetMarkingsEntry;
  markings: Marking[];
  onSaved: () => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(entry.explicit),
  );

  // Re-seed from server after a successful save / refetch.
  useEffect(() => {
    setSelected(new Set(entry.explicit));
  }, [entry.explicit]);

  const toggle = (name: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const chosen = useMemo(() => Array.from(selected), [selected]);
  const dirty = !sameSet(chosen, entry.explicit);

  const save = useMutation({
    mutationFn: () =>
      apiPut<DatasetMarkingsEntry>(
        `/datasets/${encodeURIComponent(entry.dataset)}/markings`,
        { markings: chosen },
      ),
    onSuccess: onSaved,
  });

  const explicitSet = new Set(entry.explicit);
  const effective = entry.effective;

  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginBottom: 8,
        }}
      >
        <span className="mono" style={{ fontWeight: 600, fontSize: 14 }}>
          {entry.dataset}
        </span>
        {effective.length === 0 && <Badge tone="green">unclassified</Badge>}
      </div>

      <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6 }}>
        Explicit markings
      </div>
      {markings.length > 0 ? (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: 12,
            marginBottom: 10,
          }}
        >
          {markings.map((m) => (
            <label
              key={m.name}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                cursor: "pointer",
              }}
            >
              <input
                type="checkbox"
                checked={selected.has(m.name)}
                onChange={() => toggle(m.name)}
                style={{ width: "auto" }}
              />
              <span className="mono">{m.name}</span>
            </label>
          ))}
        </div>
      ) : (
        <div className="faint" style={{ marginBottom: 10 }}>
          No markings defined yet.
        </div>
      )}

      <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6 }}>
        Effective markings
      </div>
      <div className="hint" style={{ marginTop: -4, marginBottom: 8 }}>
        Explicit markings plus any inherited from upstream inputs. A user needs
        clearance for all of these to see the dataset.
      </div>
      {effective.length > 0 ? (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: 6,
            alignItems: "center",
            marginBottom: 10,
          }}
        >
          {effective.map((name) => (
            <span
              key={name}
              style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
            >
              <Badge tone="gold">{name}</Badge>
              {!explicitSet.has(name) && (
                <span className="faint" style={{ fontSize: 11.5 }}>
                  (inherited)
                </span>
              )}
            </span>
          ))}
        </div>
      ) : (
        <div className="faint" style={{ marginBottom: 10 }}>
          None — unclassified.
        </div>
      )}

      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <button
          className="button primary small"
          disabled={!dirty || save.isPending}
          onClick={() => save.mutate()}
        >
          {save.isPending ? "Saving…" : "Save"}
        </button>
        {dirty && !save.isPending && (
          <span className="dim" style={{ margin: 0, fontSize: 12.5 }}>
            Unsaved changes
          </span>
        )}
      </div>

      <InlineError err={save.error} />
      <QueuedBanner res={save.data} />
    </div>
  );
}

// ---------------------------------------------------------- user clearances

function UserClearancesEditor({
  users,
  markings,
}: {
  users: User[];
  markings: Marking[];
}) {
  const qc = useQueryClient();
  const [username, setUsername] = useState<string>(
    () => users[0]?.username ?? "",
  );

  // Default to the first user once the list loads.
  useEffect(() => {
    if (!username && users.length > 0) setUsername(users[0].username);
  }, [users, username]);

  const clearancesQuery = useQuery({
    queryKey: ["clearances", username],
    queryFn: () =>
      api.get<UserClearances>(
        `${API}/users/${encodeURIComponent(username)}/clearances`,
      ),
    enabled: !!username,
  });

  const [selected, setSelected] = useState<Set<string>>(new Set());

  // Re-seed whenever we load a (different) user's clearances.
  useEffect(() => {
    setSelected(new Set(clearancesQuery.data?.markings ?? []));
  }, [clearancesQuery.data]);

  const toggle = (name: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const chosen = useMemo(() => Array.from(selected), [selected]);
  const dirty = !sameSet(chosen, clearancesQuery.data?.markings ?? []);

  const save = useMutation({
    mutationFn: () =>
      apiPut<UserClearances>(
        `/users/${encodeURIComponent(username)}/clearances`,
        { markings: chosen },
      ),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["clearances", username] }),
  });

  return (
    <div className="card">
      <div className="field" style={{ maxWidth: 260 }}>
        <label>User</label>
        {users.length > 0 ? (
          <select
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          >
            {users.map((u) => (
              <option key={u.id} value={u.username}>
                {u.username}
                {u.disabled ? " (disabled)" : ""}
              </option>
            ))}
          </select>
        ) : (
          <span className="faint">No users.</span>
        )}
      </div>

      {!username ? null : clearancesQuery.isLoading ? (
        <Spinner />
      ) : clearancesQuery.error ? (
        <ErrorBox error={clearancesQuery.error} />
      ) : (
        <>
          <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6 }}>
            Cleared markings
          </div>
          <div className="hint" style={{ marginTop: -4, marginBottom: 8 }}>
            Checked = the user is cleared. The user can see a dataset only if
            they hold clearance for every one of its effective markings.
          </div>
          {markings.length > 0 ? (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 4,
                marginBottom: 12,
              }}
            >
              {markings.map((m) => (
                <label
                  key={m.name}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    cursor: "pointer",
                  }}
                >
                  <input
                    type="checkbox"
                    checked={selected.has(m.name)}
                    onChange={() => toggle(m.name)}
                    style={{ width: "auto" }}
                  />
                  <span className="mono">{m.name}</span>
                  {m.description && (
                    <span className="faint">{m.description}</span>
                  )}
                </label>
              ))}
            </div>
          ) : (
            <div className="faint" style={{ marginBottom: 12 }}>
              No markings defined yet.
            </div>
          )}

          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button
              className="button primary small"
              disabled={!dirty || save.isPending}
              onClick={() => save.mutate()}
            >
              {save.isPending ? "Saving…" : "Save clearances"}
            </button>
            {dirty && !save.isPending && (
              <span className="dim" style={{ margin: 0, fontSize: 12.5 }}>
                Unsaved changes
              </span>
            )}
          </div>

          <InlineError err={save.error} />
      <QueuedBanner res={save.data} />
        </>
      )}
    </div>
  );
}

// --------------------------------------------------------------- section

export function MarkingsSection({ filter = "" }: { filter?: string }) {
  const qc = useQueryClient();

  const markingsQuery = useQuery({
    queryKey: ["markings"],
    queryFn: () => api.get<Marking[]>(`${API}/markings`),
  });

  const datasetMarkingsQuery = useQuery({
    queryKey: ["dataset-markings"],
    queryFn: () =>
      api.get<DatasetMarkingsEntry[]>(`${API}/dataset-markings`),
  });

  const usersQuery = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get<User[]>(`${API}/users`),
  });

  const markings = useMemo(
    () => markingsQuery.data ?? [],
    [markingsQuery.data],
  );
  const users = useMemo(() => usersQuery.data ?? [], [usersQuery.data]);
  const all = datasetMarkingsQuery.data ?? [];
  const needle = filter.trim().toLowerCase();
  const entries = needle
    ? all.filter((e) => e.dataset.toLowerCase().includes(needle))
    : all;

  // Deleting/creating a marking can change dataset & clearance markings too.
  const invalidateMarkings = () => {
    qc.invalidateQueries({ queryKey: ["markings"] });
    qc.invalidateQueries({ queryKey: ["dataset-markings"] });
    qc.invalidateQueries({ queryKey: ["clearances"] });
  };
  const invalidateDatasetMarkings = () =>
    qc.invalidateQueries({ queryKey: ["dataset-markings"] });

  return (
    <section style={{ marginBottom: 32 }}>
      <h2 style={{ fontSize: 15, marginBottom: 12 }}>
        Classification markings
      </h2>
      <div className="subtitle" style={{ marginTop: -6, marginBottom: 16 }}>
        Markings are mandatory classification labels that propagate through
        lineage: a derived dataset inherits its inputs' markings on build. A
        user needs clearance for every marking on a dataset to see it; admins
        bypass.
      </div>

      {/* 1. Markings */}
      <h3 style={{ fontSize: 13.5, marginBottom: 8 }}>Markings</h3>
      {markingsQuery.isLoading ? (
        <Spinner />
      ) : markingsQuery.error ? (
        <ErrorBox error={markingsQuery.error} />
      ) : (
        <MarkingsList markings={markings} onChanged={invalidateMarkings} />
      )}

      {/* 2. Dataset markings */}
      <h3 style={{ fontSize: 13.5, margin: "24px 0 8px" }}>Dataset markings</h3>
      {datasetMarkingsQuery.isLoading ? (
        <Spinner />
      ) : datasetMarkingsQuery.error ? (
        <ErrorBox error={datasetMarkingsQuery.error} />
      ) : entries.length > 0 ? (
        entries.map((entry) => (
          <DatasetMarkingsCard
            key={entry.dataset}
            entry={entry}
            markings={markings}
            onSaved={invalidateDatasetMarkings}
          />
        ))
      ) : needle ? (
        <EmptyState>
          No dataset named like "{filter.trim()}" — clear the filter above to
          see all {all.length}.
        </EmptyState>
      ) : (
        <EmptyState>No datasets defined.</EmptyState>
      )}

      {/* 3. User clearances */}
      <h3 style={{ fontSize: 13.5, margin: "24px 0 8px" }}>User clearances</h3>
      {usersQuery.isLoading ? (
        <Spinner />
      ) : usersQuery.error ? (
        <ErrorBox error={usersQuery.error} />
      ) : (
        <UserClearancesEditor users={users} markings={markings} />
      )}
    </section>
  );
}
