// Admin > Row & column security: per-dataset row-level security (RLS) and
// column masking.
//
// A dataset with no policy is unrestricted (subject to dataset access grants).
// A row policy filters which rows a user sees; column masks hide specific
// column values. Admins are always exempt.

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../../api";
import type {
  ColumnMask,
  DatasetPolicy,
  DatasetPolicyEntry,
  Group,
  MaskMode,
  PolicySubject,
  Role,
  RowPolicy,
  RowRule,
  SubjectKind,
  User,
} from "../../types";
import { Badge, EmptyState, ErrorBox, Spinner } from "../../ui";
import { InlineError, QueuedBanner, apiPut } from "./shared";

const SUBJECT_KINDS: SubjectKind[] = ["everyone", "role", "group", "user"];
const GRANTABLE_ROLES: Role[] = ["viewer", "editor", "admin"];
const MASK_MODES: MaskMode[] = ["redact", "null", "hash"];

const MASK_MODE_HINT: Record<MaskMode, string> = {
  redact: "redact = ***",
  null: "null = empty",
  hash: "hash = pseudonym",
};

// --------------------------------------------------------- subject picker

// A subject_kind select plus the conditional subject control (role select,
// group select, user select, or nothing for "everyone"). Shared by row rules
// and mask exemptions.
function SubjectPicker({
  value,
  groups,
  users,
  onChange,
}: {
  value: PolicySubject;
  groups: Group[];
  users: User[];
  onChange: (next: PolicySubject) => void;
}) {
  const onKindChange = (kind: SubjectKind) => {
    let subject = "";
    if (kind === "role") subject = "viewer";
    else if (kind === "group") subject = groups[0]?.name ?? "";
    else if (kind === "user") subject = users[0]?.username ?? "";
    onChange({ subject_kind: kind, subject });
  };

  return (
    <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
      <select
        value={value.subject_kind}
        onChange={(e) => onKindChange(e.target.value as SubjectKind)}
        style={{ width: "auto" }}
      >
        {SUBJECT_KINDS.map((k) => (
          <option key={k} value={k}>
            {k}
          </option>
        ))}
      </select>

      {value.subject_kind === "everyone" ? (
        <span className="faint">—</span>
      ) : value.subject_kind === "role" ? (
        <select
          value={value.subject}
          onChange={(e) => onChange({ ...value, subject: e.target.value })}
          style={{ width: "auto" }}
        >
          {GRANTABLE_ROLES.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      ) : value.subject_kind === "group" ? (
        groups.length > 0 ? (
          <select
            value={value.subject}
            onChange={(e) => onChange({ ...value, subject: e.target.value })}
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
          <span className="faint">
              {/* Was inert text at a dead end: the door to create the thing it
                  says is missing sits on this same page. NOT an `#groups`
                  anchor — this app routes on the hash, so that would navigate
                  away instead of scrolling. */}
              No groups yet —{" "}
              <button
                type="button"
                className="linklike"
                onClick={() =>
                  document
                    .getElementById("groups")
                    ?.scrollIntoView({ behavior: "smooth", block: "start" })
                }
              >
                create one under Groups
              </button>
              .
            </span>
        )
      ) : (
        // user
        <select
          value={value.subject}
          onChange={(e) => onChange({ ...value, subject: e.target.value })}
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
    </span>
  );
}

// ------------------------------------------------------------------ helpers

function newRule(): RowRule {
  return { subject_kind: "everyone", subject: "", values: [] };
}

function newMask(): ColumnMask {
  return { column: "", mode: "redact", exempt: [] };
}

function parseValues(s: string): string[] {
  return s
    .split(",")
    .map((v) => v.trim())
    .filter((v) => v.length > 0);
}

// --------------------------------------------------------------- row policy

function RowSecurityEditor({
  column,
  rules,
  groups,
  users,
  onColumnChange,
  onRulesChange,
}: {
  column: string;
  rules: RowRule[];
  groups: Group[];
  users: User[];
  onColumnChange: (col: string) => void;
  onRulesChange: (next: RowRule[]) => void;
}) {
  const colLabel = column.trim() || "column";

  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6 }}>
        Row security
      </div>

      <div className="field" style={{ marginBottom: 8 }}>
        <label>Policy column</label>
        <input
          value={column}
          onChange={(e) => onColumnChange(e.target.value)}
          placeholder="e.g. region"
          autoComplete="off"
          style={{ maxWidth: 260 }}
        />
        <div className="hint">
          A user sees a row only if a rule matches them and the row's{" "}
          <span className="mono">{colLabel}</span> value is in that rule's
          values. A dataset with a row policy but no matching rule shows the
          user no rows.
        </div>
      </div>

      {rules.length > 0 && (
        <div className="table-wrap" style={{ marginBottom: 8 }}>
          <table className="table">
            <thead>
              <tr>
                <th>Subject</th>
                <th>Allowed values (comma-separated)</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rules.map((rule, i) => (
                <tr key={i}>
                  <td>
                    <SubjectPicker
                      value={rule}
                      groups={groups}
                      users={users}
                      onChange={(subj) =>
                        onRulesChange(
                          rules.map((r, j) =>
                            j === i ? { ...r, ...subj } : r,
                          ),
                        )
                      }
                    />
                  </td>
                  <td>
                    <input
                      value={rule.values.join(", ")}
                      onChange={(e) =>
                        onRulesChange(
                          rules.map((r, j) =>
                            j === i
                              ? { ...r, values: parseValues(e.target.value) }
                              : r,
                          ),
                        )
                      }
                      placeholder="us-east, us-west"
                      autoComplete="off"
                    />
                  </td>
                  <td>
                    <button
                      className="button danger small"
                      onClick={() =>
                        onRulesChange(rules.filter((_, j) => j !== i))
                      }
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

      <button
        className="button small"
        onClick={() => onRulesChange([...rules, newRule()])}
      >
        Add rule
      </button>
    </div>
  );
}

// ------------------------------------------------------------ column masking

function MaskExemptions({
  exempt,
  groups,
  users,
  onChange,
}: {
  exempt: PolicySubject[];
  groups: Group[];
  users: User[];
  onChange: (next: PolicySubject[]) => void;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {exempt.map((subj, i) => (
        <div
          key={i}
          style={{ display: "flex", gap: 6, alignItems: "center" }}
        >
          <SubjectPicker
            value={subj}
            groups={groups}
            users={users}
            onChange={(next) =>
              onChange(exempt.map((s, j) => (j === i ? next : s)))
            }
          />
          <button
            className="button danger small"
            onClick={() => onChange(exempt.filter((_, j) => j !== i))}
          >
            Remove
          </button>
        </div>
      ))}
      <div>
        <button
          className="button small"
          onClick={() =>
            onChange([...exempt, { subject_kind: "everyone", subject: "" }])
          }
        >
          Add exemption
        </button>
      </div>
    </div>
  );
}

function ColumnMaskingEditor({
  masks,
  groups,
  users,
  onChange,
}: {
  masks: ColumnMask[];
  groups: Group[];
  users: User[];
  onChange: (next: ColumnMask[]) => void;
}) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6 }}>
        Column masking
      </div>
      <div className="hint" style={{ marginBottom: 8 }}>
        Admins always see unmasked values.
      </div>

      {masks.length > 0 && (
        <div className="table-wrap" style={{ marginBottom: 8 }}>
          <table className="table">
            <thead>
              <tr>
                <th>Column</th>
                <th>Mode</th>
                <th>Exempt subjects (see real value)</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {masks.map((mask, i) => (
                <tr key={i}>
                  <td>
                    <input
                      value={mask.column}
                      onChange={(e) =>
                        onChange(
                          masks.map((m, j) =>
                            j === i ? { ...m, column: e.target.value } : m,
                          ),
                        )
                      }
                      placeholder="e.g. ssn"
                      autoComplete="off"
                      style={{ maxWidth: 160 }}
                    />
                  </td>
                  <td>
                    <select
                      value={mask.mode}
                      onChange={(e) =>
                        onChange(
                          masks.map((m, j) =>
                            j === i
                              ? { ...m, mode: e.target.value as MaskMode }
                              : m,
                          ),
                        )
                      }
                      style={{ width: "auto" }}
                    >
                      {MASK_MODES.map((mode) => (
                        <option key={mode} value={mode}>
                          {MASK_MODE_HINT[mode]}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <MaskExemptions
                      exempt={mask.exempt}
                      groups={groups}
                      users={users}
                      onChange={(next) =>
                        onChange(
                          masks.map((m, j) =>
                            j === i ? { ...m, exempt: next } : m,
                          ),
                        )
                      }
                    />
                  </td>
                  <td style={{ verticalAlign: "top" }}>
                    <button
                      className="button danger small"
                      onClick={() =>
                        onChange(masks.filter((_, j) => j !== i))
                      }
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

      <button
        className="button small"
        onClick={() => onChange([...masks, newMask()])}
      >
        Add mask
      </button>
    </div>
  );
}

// ------------------------------------------------------------ dataset card

function DatasetPolicyCard({
  entry,
  groups,
  users,
  onSaved,
}: {
  entry: DatasetPolicyEntry;
  groups: Group[];
  users: User[];
  onSaved: () => void;
}) {
  const [column, setColumn] = useState(entry.policy?.row_policy?.column ?? "");
  const [rules, setRules] = useState<RowRule[]>(
    entry.policy?.row_policy?.rules ?? [],
  );
  const [masks, setMasks] = useState<ColumnMask[]>(
    entry.policy?.column_masks ?? [],
  );
  const [dirty, setDirty] = useState(false);

  // Re-seed from server after a successful save / refetch.
  useEffect(() => {
    setColumn(entry.policy?.row_policy?.column ?? "");
    setRules(entry.policy?.row_policy?.rules ?? []);
    setMasks(entry.policy?.column_masks ?? []);
    setDirty(false);
  }, [entry.policy]);

  const hasRowPolicy = column.trim().length > 0 || rules.length > 0;
  const hasMasks = masks.length > 0;

  const save = useMutation({
    mutationFn: () => {
      const rowPolicy: RowPolicy | null = hasRowPolicy
        ? { column: column.trim(), rules }
        : null;
      const body: DatasetPolicy = {
        row_policy: rowPolicy,
        column_masks: masks,
      };
      return apiPut<DatasetPolicyEntry>(
        `/datasets/${encodeURIComponent(entry.dataset)}/policy`,
        body,
      );
    },
    onSuccess: () => {
      setDirty(false);
      onSaved();
    },
  });

  const touch = () => setDirty(true);

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
        {!hasRowPolicy && !hasMasks ? (
          <Badge tone="green">no policy</Badge>
        ) : (
          <>
            {hasRowPolicy && <Badge tone="gold">RLS</Badge>}
            {hasMasks && (
              <Badge tone="blue">
                {masks.length} masked
              </Badge>
            )}
          </>
        )}
      </div>

      <RowSecurityEditor
        column={column}
        rules={rules}
        groups={groups}
        users={users}
        onColumnChange={(col) => {
          setColumn(col);
          touch();
        }}
        onRulesChange={(next) => {
          setRules(next);
          touch();
        }}
      />

      <ColumnMaskingEditor
        masks={masks}
        groups={groups}
        users={users}
        onChange={(next) => {
          setMasks(next);
          touch();
        }}
      />

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

// --------------------------------------------------------------- section

export function DataSecuritySection({ filter = "" }: { filter?: string }) {
  const qc = useQueryClient();
  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["dataset-policies"] });

  const policiesQuery = useQuery({
    queryKey: ["dataset-policies"],
    queryFn: () => api.get<DatasetPolicyEntry[]>(`${API}/dataset-policies`),
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
  const all = policiesQuery.data ?? [];
  const needle = filter.trim().toLowerCase();
  const entries = needle
    ? all.filter((e) => e.dataset.toLowerCase().includes(needle))
    : all;

  return (
    <section style={{ marginBottom: 32 }}>
      <h2 style={{ fontSize: 15, marginBottom: 12 }}>Row &amp; column security</h2>
      <div className="subtitle" style={{ marginTop: -6, marginBottom: 12 }}>
        Row-level security and column masking apply to the dataset's rows, the
        SQL page, and its ontology objects. Admins are exempt.
      </div>

      {policiesQuery.isLoading ? (
        <Spinner />
      ) : policiesQuery.error ? (
        <ErrorBox error={policiesQuery.error} />
      ) : entries.length > 0 ? (
        entries.map((entry) => (
          <DatasetPolicyCard
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
