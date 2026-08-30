// Admin > Dataset access: per-dataset fine-grained permissions.
//
// A dataset with no grants is "open" — it inherits global RBAC (any authed
// user views, editor+ edits). Adding any grant flips it to an allowlist: only
// listed subjects get access. can_edit implies view; admins always bypass.

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../../api";
import type {
  DatasetGrants,
  FingerprintResponse,
  GovernanceCell,
  Group,
  Grant,
  Role,
  SubjectKind,
  User,
} from "../../types";
import {
  Badge,
  DataTable,
  EmptyState,
  ErrorBox,
  Spinner,
  type Column,
} from "../../ui";
import { InlineError, QueuedBanner, apiPut } from "./shared";

const SUBJECT_KINDS: SubjectKind[] = ["everyone", "role", "group", "user"];
const GRANTABLE_ROLES: Role[] = ["viewer", "editor", "admin"];

// --------------------------------------------------------------- grant row

function GrantRow({
  grant,
  groups,
  users,
  onChange,
  onRemove,
}: {
  grant: Grant;
  groups: Group[];
  users: User[];
  onChange: (next: Grant) => void;
  onRemove: () => void;
}) {
  const set = (patch: Partial<Grant>) => onChange({ ...grant, ...patch });

  const onKindChange = (kind: SubjectKind) => {
    // Reset subject to a sensible default for the new kind.
    let subject = "";
    if (kind === "role") subject = "viewer";
    else if (kind === "group") subject = groups[0]?.name ?? "";
    else if (kind === "user") subject = users[0]?.username ?? "";
    onChange({ ...grant, subject_kind: kind, subject });
  };

  return (
    <tr>
      <td>
        <select
          value={grant.subject_kind}
          onChange={(e) => onKindChange(e.target.value as SubjectKind)}
          style={{ width: "auto" }}
        >
          {SUBJECT_KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </td>
      <td>
        {grant.subject_kind === "everyone" ? (
          <span className="faint">—</span>
        ) : grant.subject_kind === "role" ? (
          <select
            value={grant.subject}
            onChange={(e) => set({ subject: e.target.value })}
            style={{ width: "auto" }}
          >
            {GRANTABLE_ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        ) : grant.subject_kind === "group" ? (
          groups.length > 0 ? (
            <select
              value={grant.subject}
              onChange={(e) => set({ subject: e.target.value })}
              style={{ width: "auto" }}
            >
              <option value="">— select group —</option>
              {groups.map((g) => (
                <option key={g.name} value={g.name}>
                  {g.name}
                </option>
              ))}
            </select>
          ) : (
            <span className="faint">no groups defined</span>
          )
        ) : (
          // user
          <select
            value={grant.subject}
            onChange={(e) => set({ subject: e.target.value })}
            style={{ width: "auto" }}
          >
            <option value="">— select user —</option>
            {users.map((u) => (
              <option key={u.id} value={u.username}>
                {u.username}
              </option>
            ))}
          </select>
        )}
      </td>
      <td style={{ textAlign: "center" }}>
        <input
          type="checkbox"
          checked={grant.can_view || grant.can_edit}
          // edit implies view, so view is forced on while edit is checked
          disabled={grant.can_edit}
          onChange={(e) => set({ can_view: e.target.checked })}
          style={{ width: "auto" }}
        />
      </td>
      <td style={{ textAlign: "center" }}>
        <input
          type="checkbox"
          checked={grant.can_edit}
          onChange={(e) =>
            set({
              can_edit: e.target.checked,
              can_view: e.target.checked ? true : grant.can_view,
            })
          }
          style={{ width: "auto" }}
        />
      </td>
      <td>
        <button className="button danger small" onClick={onRemove}>
          Remove
        </button>
      </td>
    </tr>
  );
}

// ---------------------------------------------------- effective access
//
// "Who can see this dataset?" answered on the card that controls it, instead
// of an admin cross-referencing grants, group membership, markings and row
// policy across three screens. The answer is COMPUTED, not summarized: it
// calls the same governance-fingerprint route Portability's verification
// panel uses, which walks every enforcement path for each (principal,
// dataset) pair — so what renders here is what a query would actually do.

function effectiveAccessColumns(
  cells: Record<string, GovernanceCell>,
  dataset: string,
): Column<string>[] {
  const cellOf = (who: string) => cells[`${who}|${dataset}`];
  return [
    {
      label: "Principal",
      className: "mono",
      render: (who) => who,
    },
    {
      label: "Access",
      render: (who) => {
        const cell = cellOf(who);
        return !cell || (!cell.can_view && !cell.can_edit) ? (
          <Badge tone="red">no access</Badge>
        ) : cell.can_edit ? (
          <Badge tone="gold">view + edit</Badge>
        ) : (
          <Badge tone="green">view</Badge>
        );
      },
    },
    {
      label: "Markings in force",
      className: "mono dim",
      render: (who) => {
        const cell = cellOf(who);
        return cell && cell.effective_markings.length > 0
          ? cell.effective_markings.join(", ")
          : "—";
      },
    },
    {
      // The rendered decision as visible text, not a tooltip: governance
      // prose must be readable without a mouse hover (rule F6).
      label: "Rows they see",
      className: "mono dim",
      render: (who) => {
        const cell = cellOf(who);
        return cell ? cell.decision || (cell.can_view ? "all rows" : "—") : "—";
      },
    },
  ];
}

function EffectiveAccessPanel({ dataset }: { dataset: string }) {
  const [open, setOpen] = useState(false);

  const compute = useMutation({
    mutationFn: () =>
      api.post<FingerprintResponse>(`${API}/workspace/governance/fingerprint`, {
        principals: [],
        datasets: [dataset],
      }),
  });

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && !compute.data && !compute.isPending) compute.mutate();
  };

  const fp = compute.data?.fingerprint;
  const principals = fp?.principals ?? [];

  return (
    <div style={{ marginTop: 10 }}>
      <button
        type="button"
        className="button small"
        aria-expanded={open}
        onClick={toggle}
      >
        {open ? "Hide effective access" : "Effective access"}
      </button>
      {open && (
        <div style={{ marginTop: 8 }}>
          {compute.isPending ? (
            <Spinner label="Computing who can see this dataset…" />
          ) : compute.isError ? (
            <InlineError err={compute.error} />
          ) : fp ? (
            <>
              <p className="dim" style={{ fontSize: 12, marginTop: 0, marginBottom: 6 }}>
                Computed live through every enforcement path — grants, groups,
                markings and clearances, row policy and column masks. Admins
                always have access and are shown as such.
              </p>
              {principals.length === 0 ? (
                <EmptyState>No principals to evaluate.</EmptyState>
              ) : (
                <DataTable
                  columns={effectiveAccessColumns(fp.cells, dataset)}
                  rows={principals}
                  rowKey={(who) => who}
                />
              )}
            </>
          ) : null}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------ dataset card

function newGrant(): Grant {
  return {
    subject_kind: "everyone",
    subject: "",
    can_view: true,
    can_edit: false,
  };
}

function DatasetPermissionCard({
  entry,
  groups,
  users,
  onSaved,
}: {
  entry: DatasetGrants;
  groups: Group[];
  users: User[];
  onSaved: () => void;
}) {
  const [grants, setGrants] = useState<Grant[]>(entry.grants);
  const [dirty, setDirty] = useState(false);

  // Re-seed from server after a successful save / refetch.
  useEffect(() => {
    setGrants(entry.grants);
    setDirty(false);
  }, [entry.grants]);

  const save = useMutation({
    mutationFn: () =>
      apiPut<DatasetGrants>(
        `/datasets/${encodeURIComponent(entry.dataset)}/permissions`,
        { grants },
      ),
    onSuccess: () => {
      setDirty(false);
      onSaved();
    },
  });

  const update = (next: Grant[]) => {
    setGrants(next);
    setDirty(true);
  };

  const isOpen = grants.length === 0;

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
        {isOpen ? (
          <Badge tone="green">open</Badge>
        ) : (
          <Badge tone="gold">allowlist ({grants.length})</Badge>
        )}
      </div>

      {isOpen && (
        <div className="dim" style={{ marginBottom: 8, fontSize: 12.5 }}>
          Open — inherits role defaults (viewers view, editors edit). Add a grant
          to restrict this dataset to an allowlist.
        </div>
      )}

      {grants.length > 0 && (
        <div className="table-wrap" style={{ marginBottom: 8 }}>
          <table className="table">
            <thead>
              <tr>
                <th>Subject kind</th>
                <th>Subject</th>
                <th style={{ textAlign: "center" }}>Can view</th>
                <th style={{ textAlign: "center" }}>Can edit</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {grants.map((g, i) => (
                <GrantRow
                  key={i}
                  grant={g}
                  groups={groups}
                  users={users}
                  onChange={(next) =>
                    update(grants.map((x, j) => (j === i ? next : x)))
                  }
                  onRemove={() => update(grants.filter((_, j) => j !== i))}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <button
          className="button small"
          onClick={() => update([...grants, newGrant()])}
        >
          Add grant
        </button>
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
      {/* Widening grants can come back 202 (queued behind a second approver);
          the grants list then legitimately re-seeds unchanged. Say so. */}
      <QueuedBanner res={save.data} />

      <EffectiveAccessPanel dataset={entry.dataset} />
    </div>
  );
}

// --------------------------------------------------------------- section

export function DatasetAccessSection({ filter = "" }: { filter?: string }) {
  const qc = useQueryClient();
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["dataset-permissions"] });
    // A widening save files a proposal; keep the inbox in step.
    void qc.invalidateQueries({ queryKey: ["proposals"] });
  };

  const permsQuery = useQuery({
    queryKey: ["dataset-permissions"],
    queryFn: () => api.get<DatasetGrants[]>(`${API}/dataset-permissions`),
  });

  const groupsQuery = useQuery({
    queryKey: ["groups"],
    queryFn: () => api.get<Group[]>(`${API}/groups`),
  });

  const usersQuery = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get<User[]>(`${API}/users`),
  });

  const groups = useMemo(() => groupsQuery.data ?? [], [groupsQuery.data]);
  const users = useMemo(() => usersQuery.data ?? [], [usersQuery.data]);
  const all = permsQuery.data ?? [];
  const needle = filter.trim().toLowerCase();
  const entries = needle
    ? all.filter((e) => e.dataset.toLowerCase().includes(needle))
    : all;

  return (
    <section style={{ marginBottom: 32 }}>
      <h2 style={{ fontSize: 15, marginBottom: 12 }}>Dataset access</h2>
      <div className="subtitle" style={{ marginTop: -6, marginBottom: 12 }}>
        Per-dataset access control. A dataset with no grants is{" "}
        <strong>open</strong> and inherits role defaults. Adding any grant makes
        it an <strong>allowlist</strong> — only listed subjects can see or edit
        it. This also hides the dataset's ontology objects and blocks it in the
        SQL page. Admins always have access.
      </div>

      {permsQuery.isLoading ? (
        <Spinner />
      ) : permsQuery.error ? (
        <ErrorBox error={permsQuery.error} />
      ) : entries.length > 0 ? (
        entries.map((entry) => (
          <DatasetPermissionCard
            key={entry.dataset}
            entry={entry}
            groups={groups}
            users={users}
            onSaved={invalidate}
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
    </section>
  );
}
