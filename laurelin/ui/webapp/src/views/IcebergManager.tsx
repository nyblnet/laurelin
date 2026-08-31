// Managing an Iceberg-backed dataset: branches, snapshot history, and schema
// evolution. Shown on the dataset detail page when kind === "iceberg".
//
// The pieces map one-to-one to what Iceberg buys over managed Parquet:
// branches (work in isolation, merge as a metadata swap), time travel (the
// snapshot list), and safe schema change (additive freely; breaking only after
// seeing the downstream impact).

import type { ReactNode } from "react";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../api";
import { Badge, EmptyState, ErrorBox, Spinner, fmtTime } from "../ui";

interface Branch {
  branch: string;
  snapshot_id: number;
}
interface Snapshot {
  snapshot_id: number;
  timestamp_ms: number;
  operation: string | null;
  /** What a scan of THIS snapshot opens, and what those files weigh. */
  data_files: number;
  bytes: number;
  /** Laurelin version rows that pin this snapshot, and Iceberg refs that do.
   *  Both are promises the snapshot stays readable; only the second kind is a
   *  promise Iceberg's own tooling knows about. */
  versions: number[];
  refs: string[];
  pinned: boolean;
}

interface StorageReport {
  dataset: string;
  snapshots: Snapshot[];
  /** Distinct data files across every snapshot, and their union in bytes —
   *  the table's actual footprint. Summing the per-snapshot figures would
   *  double-count files that several snapshots share. */
  data_files: number;
  bytes: number;
  unpinned_snapshots: number;
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

const COLUMN_TYPES = ["string", "integer", "float", "boolean", "timestamp"];

export function IcebergManager({
  name,
  compact,
}: {
  name: string;
  // The compaction control, passed in rather than imported: it lives on the
  // dataset page beside the managed one, and importing it here would make the
  // two modules import each other.
  compact?: ReactNode;
}) {
  return (
    <div style={{ marginTop: 8 }}>
      <BranchesPanel name={name} />
      <SchemaPanel name={name} />
      <SnapshotsPanel name={name} compact={compact} />
    </div>
  );
}

// ----------------------------------------------------------------- branches

function BranchesPanel({ name }: { name: string }) {
  const qc = useQueryClient();
  const [newBranch, setNewBranch] = useState("");
  const q = useQuery({
    queryKey: ["iceberg-branches", name],
    queryFn: () => api.get<Branch[]>(`${API}/datasets/${name}/iceberg/branches`),
  });
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["iceberg-branches", name] });
    qc.invalidateQueries({ queryKey: ["dataset", name] });
  };

  const create = useMutation({
    mutationFn: () =>
      api.post(`${API}/datasets/${name}/iceberg/branches`, { branch: newBranch }),
    onSuccess: () => {
      setNewBranch("");
      invalidate();
    },
  });
  const merge = useMutation({
    mutationFn: (branch: string) =>
      api.post(`${API}/datasets/${name}/iceberg/branches/${branch}/merge`, {}),
    onSettled: invalidate,
  });
  const drop = useMutation({
    mutationFn: (branch: string) =>
      api.del(`${API}/datasets/${name}/iceberg/branches/${branch}`),
    onSettled: invalidate,
  });

  const validName = /^[a-z][a-z0-9_]*$/.test(newBranch);

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <label style={{ marginBottom: 4 }}>Branches</label>
      <p className="hint" style={{ marginTop: 0 }}>
        A branch is a named pointer into the history, so it copies no data. Write
        to it without <code>main</code> seeing it; merge fast-forwards.
      </p>

      {q.isLoading && <Spinner />}
      {q.isError && <ErrorBox error={q.error} />}
      {(create.error || merge.error || drop.error) && (
        <ErrorBox error={create.error || merge.error || drop.error} />
      )}

      {q.data && (
        <div className="table-wrap">
          <table>
            <tbody>
              {q.data.map((b) => (
                <tr key={b.branch}>
                  <td className="mono">
                    {b.branch}
                    {b.branch === "main" && (
                      <Badge tone="gold" >trunk</Badge>
                    )}
                  </td>
                  <td className="mono faint" style={{ fontSize: 12 }}>
                    {b.snapshot_id}
                  </td>
                  <td style={{ textAlign: "right" }}>
                    {b.branch !== "main" && (
                      <span className="toolbar" style={{ gap: 6, justifyContent: "flex-end" }}>
                        <button
                          className="small"
                          disabled={merge.isPending}
                          onClick={() => merge.mutate(b.branch)}
                          title="Fast-forward main to this branch"
                        >
                          Merge to main
                        </button>
                        <button
                          className="small"
                          onClick={() => {
                            if (confirm(`Delete branch ${b.branch}?`)) drop.mutate(b.branch);
                          }}
                        >
                          Delete
                        </button>
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="toolbar" style={{ gap: 8, marginTop: 8 }}>
        <input
          className="mono"
          value={newBranch}
          onChange={(e) => setNewBranch(e.target.value)}
          placeholder="staging"
          style={{ maxWidth: 200 }}
        />
        <button
          className="small"
          disabled={!validName || create.isPending}
          onClick={() => create.mutate()}
        >
          {create.isPending ? "Branching…" : "New branch"}
        </button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------- schema

function SchemaPanel({ name }: { name: string }) {
  const qc = useQueryClient();
  const [col, setCol] = useState("");
  const [colType, setColType] = useState("string");

  const invalidate = () => qc.invalidateQueries({ queryKey: ["dataset", name] });

  const addColumn = useMutation({
    mutationFn: () =>
      api.post(`${API}/datasets/${name}/iceberg/schema`, { add: { [col]: colType } }),
    onSuccess: () => {
      setCol("");
      invalidate();
    },
  });

  // Dropping is a two-step: fetch the impact, show it, and only then send the
  // change with allow_breaking. The point is that "what breaks" is visible
  // before the click that breaks it.
  const dropColumn = useMutation({
    mutationFn: async (column: string) => {
      const impact = await api.get<{ downstream: string[] }>(
        `${API}/datasets/${name}/iceberg/schema/impact`,
      );
      const affected = impact.downstream.length
        ? `\n\nDownstream datasets that will break:\n  ${impact.downstream.join("\n  ")}`
        : "\n\nNo derived datasets, but object types and dashboards may still reference it.";
      if (!confirm(`Drop column "${column}"? This is a breaking change.${affected}`)) {
        return null;
      }
      return api.post(`${API}/datasets/${name}/iceberg/schema`, {
        drop: [column],
        allow_breaking: true,
      });
    },
    onSettled: invalidate,
  });

  const validCol = /^[a-z][a-z0-9_]*$/i.test(col);

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <label style={{ marginBottom: 4 }}>Schema evolution</label>
      <p className="hint" style={{ marginTop: 0 }}>
        Adding a column is safe — Iceberg tracks columns by id, so old snapshots
        stay readable. Dropping one is breaking, and shows what it affects first.
      </p>

      {(addColumn.error || dropColumn.error) && (
        <ErrorBox error={addColumn.error || dropColumn.error} />
      )}

      <div className="toolbar" style={{ gap: 8, flexWrap: "wrap" }}>
        <input
          className="mono"
          value={col}
          onChange={(e) => setCol(e.target.value)}
          placeholder="new_column"
          style={{ maxWidth: 200 }}
        />
        <select value={colType} onChange={(e) => setColType(e.target.value)}>
          {COLUMN_TYPES.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
        <button
          className="small"
          disabled={!validCol || addColumn.isPending}
          onClick={() => addColumn.mutate()}
        >
          {addColumn.isPending ? "Adding…" : "Add column"}
        </button>
      </div>
      <p className="hint" style={{ marginBottom: 0 }}>
        To drop a column, use the button beside it in the schema table above.
      </p>
      {/* The drop trigger is exposed for the detail page's schema list. */}
      <DropColumnHint onDrop={(c) => dropColumn.mutate(c)} />
    </div>
  );
}

// A minimal drop control: the detail page's schema table is managed-dataset
// shaped, so rather than thread a callback through it, an Iceberg dataset gets
// an explicit "drop a column" field here. Keeps the breaking path deliberate.
function DropColumnHint({ onDrop }: { onDrop: (column: string) => void }) {
  const [col, setCol] = useState("");
  return (
    <div className="toolbar" style={{ gap: 8, marginTop: 8 }}>
      <input
        className="mono"
        value={col}
        onChange={(e) => setCol(e.target.value)}
        placeholder="column to drop"
        style={{ maxWidth: 200 }}
      />
      <button
        className="small"
        disabled={!col.trim()}
        onClick={() => {
          onDrop(col.trim());
          setCol("");
        }}
      >
        Drop column…
      </button>
    </div>
  );
}

// ---------------------------------------------------------------- snapshots

function SnapshotsPanel({ name, compact }: { name: string; compact?: ReactNode }) {
  // The storage report rather than the bare snapshot list: it is the same
  // history plus the two numbers the history could not explain — what each
  // snapshot costs, and what is holding it. "Compaction reclaims scan cost,
  // not disk" and "nothing expires snapshots" were sentences in a doc; this is
  // the panel where they become checkable. It costs one manifest plan per
  // snapshot, which is metadata I/O and the only way to attribute bytes at all.
  const q = useQuery({
    queryKey: ["iceberg-storage", name],
    queryFn: () => api.get<StorageReport>(`${API}/datasets/${name}/iceberg/storage`),
  });
  const report = q.data;

  return (
    <div className="card">
      {/* Compaction belongs here rather than beside "Version history": for an
          Iceberg dataset the snapshots *are* the history, and compaction adds
          one — it rewrites the data files into a new snapshot and leaves every
          earlier one readable. The button was hidden for Iceberg because the
          server's compaction was broken here; now that it works, hiding it
          would just be a capability nobody could reach. */}
      <div
        className="toolbar"
        style={{ justifyContent: "space-between", alignItems: "center" }}
      >
        <label style={{ margin: 0 }}>Snapshot history</label>
        {compact}
      </div>
      {q.isLoading && <Spinner />}
      {q.isError && <ErrorBox error={q.error} onRetry={() => q.refetch()} />}
      {report &&
        (report.snapshots.length === 0 ? (
          <EmptyState>
            {/* no in-app door: a snapshot is a side effect of a write. */}
            No snapshots yet — one appears each time this table is written.
          </EmptyState>
        ) : (
          <>
            <p className="hint" style={{ marginTop: 6 }}>
              {report.data_files.toLocaleString()} data file
              {report.data_files === 1 ? "" : "s"}, {fmtBytes(report.bytes)} on
              disk across every snapshot. Compaction merges the files a{" "}
              <em>scan</em> opens; earlier snapshots keep their own, so it makes
              reads cheaper and frees no disk. Nothing expires snapshots here —
              a snapshot a version row pins is a promise that version is still
              readable, and Iceberg's own expiry protects branches and tags
              without knowing about those rows.
            </p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Operation</th>
                    <th>When</th>
                    <th>Files</th>
                    <th>Scan size</th>
                    <th>Held by</th>
                    <th>Snapshot</th>
                  </tr>
                </thead>
                <tbody>
                  {[...report.snapshots].reverse().map((s) => (
                    <tr key={s.snapshot_id}>
                      <td>{s.operation ?? "—"}</td>
                      <td className="dim">
                        {fmtTime(new Date(s.timestamp_ms).toISOString())}
                      </td>
                      <td className="mono">{s.data_files.toLocaleString()}</td>
                      <td className="mono">{fmtBytes(s.bytes)}</td>
                      <td>
                        {s.versions.map((v) => (
                          <Badge key={`v${v}`} tone="green">{`v${v}`}</Badge>
                        ))}
                        {s.refs.map((r) => (
                          <Badge key={r}>{r}</Badge>
                        ))}
                        {!s.pinned && (
                          <span className="faint" title="No version row and no branch or tag holds this snapshot.">
                            nothing
                          </span>
                        )}
                      </td>
                      <td className="mono faint" style={{ fontSize: 12 }}>
                        {s.snapshot_id}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ))}
    </div>
  );
}
