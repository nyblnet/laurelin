// Datasets view: list of versioned Parquet datasets and a per-dataset detail
// page with schema, version history, an editor-only upload control, and a paged
// row preview. Mounted at /datasets/* — internal routing below.

import { useState } from "react";
import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { API, api } from "../api";
import { useAuth } from "../auth";
import type {
  ColumnSchema,
  Dataset,
  DatasetDetail,
  DatasetVersion,
  RowsPage,
  UploadPreview,
} from "../types";
import {
  Badge,
  Column,
  DataTable,
  EmptyState,
  ErrorBox,
  PageHeader,
  Spinner,
  fmtNum,
  fmtTime,
  fmtValue,
} from "../ui";
import { SourcesSection } from "./Sources";
import { IcebergManager } from "./IcebergManager";

const PAGE_SIZE = 50;

export function DatasetsView() {
  return (
    <Routes>
      <Route path="/" element={<DatasetList />} />
      <Route path=":name" element={<DatasetDetailPage />} />
    </Routes>
  );
}

// ------------------------------------------------------------------ list

function DatasetList() {
  const navigate = useNavigate();
  const { data, isLoading, error } = useQuery({
    queryKey: ["datasets"],
    queryFn: () => api.get<Dataset[]>(`${API}/datasets`),
  });

  const columns: Column<Dataset>[] = [
    {
      label: "Name",
      className: "mono",
      render: (d) => (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <Link to={`/datasets/${d.name}`}>{d.name}</Link>
          {d.kind === "federated" && <Badge tone="blue">federated</Badge>}
          {d.kind === "iceberg" && <Badge tone="green">iceberg</Badge>}
        </span>
      ),
    },
    {
      label: "Description",
      render: (d) =>
        d.description ? <span className="dim">{d.description}</span> : <span className="faint">—</span>,
    },
    {
      label: "Latest",
      render: (d) =>
        d.latest_version != null ? (
          <Badge tone="gold">v{d.latest_version}</Badge>
        ) : (
          <span className="faint">—</span>
        ),
    },
    {
      label: "Created",
      render: (d) => <span className="dim">{fmtTime(d.created_at)}</span>,
    },
  ];

  return (
    <div>
      <PageHeader
        title="Datasets"
        subtitle="Versioned Parquet datasets in this workspace."
      />
      <ImportPanel />
      <FederatedPanel />
      {isLoading && <Spinner />}
      {error && <ErrorBox error={error} />}
      {data &&
        (data.length === 0 ? (
          <EmptyState>No datasets yet — import a file above to make one.</EmptyState>
        ) : (
          <DataTable
            columns={columns}
            rows={data}
            rowKey={(d) => d.name}
            onRowClick={(d) => navigate(`/datasets/${d.name}`)}
          />
        ))}
      <SourcesSection />
    </div>
  );
}

// ------------------------------------------------------------------ import

/**
 * Create a dataset from a file, without leaving the browser.
 *
 * The flow is deliberately preview-then-commit. Importing a file is a decision
 * about column names and types, and making it blind — upload, then discover
 * every column came back VARCHAR — is how a dataset ends up wrong on v1. So
 * dropping a file shows the inferred schema and a sample first; nothing is
 * created until "Import" is pressed.
 */
function ImportPanel() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { user } = useAuth();
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [dragging, setDragging] = useState(false);

  const canEdit = user?.role === "editor" || user?.role === "admin";

  const previewM = useMutation({
    mutationFn: (f: File) => {
      const form = new FormData();
      form.append("file", f);
      return api.upload<UploadPreview>(`${API}/datasets/preview`, form);
    },
    onSuccess: (p) => setName(p.suggested_name),
  });

  const [asIceberg, setAsIceberg] = useState(false);

  const importM = useMutation({
    mutationFn: ({ f, target }: { f: File; target: string }) => {
      const form = new FormData();
      form.append("file", f);
      // Same upload, different backend: the Iceberg path writes a snapshot
      // table other engines can open, at the cost of the [iceberg] extra.
      const endpoint = asIceberg
        ? `${API}/datasets/${target}/iceberg`
        : `${API}/datasets/${target}/upload`;
      return api.upload<DatasetVersion>(endpoint, form);
    },
    onSuccess: (_v, { target }) => {
      qc.invalidateQueries({ queryKey: ["datasets"] });
      navigate(`/datasets/${target}`);
    },
  });

  function choose(f: File | null | undefined) {
    if (!f) return;
    setFile(f);
    importM.reset();
    previewM.mutate(f);
  }

  function reset() {
    setFile(null);
    setName("");
    previewM.reset();
    importM.reset();
  }

  if (!canEdit) return null;

  const preview = previewM.data;
  const nameValid = /^[a-z][a-z0-9_]*$/.test(name);

  return (
    <div className="card" style={{ marginBottom: 24 }}>
      <label style={{ marginBottom: 8 }}>Import a file</label>

      {!file ? (
        <div
          className={`dropzone${dragging ? " dropzone-active" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            choose(e.dataTransfer.files?.[0]);
          }}
        >
          <p>Drop a CSV or Parquet file here</p>
          <label className="link-button">
            or choose a file
            <input
              type="file"
              accept=".csv,.parquet"
              style={{ display: "none" }}
              onChange={(e) => choose(e.target.files?.[0])}
            />
          </label>
        </div>
      ) : (
        <div>
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <span className="mono">{file.name}</span>
            <span className="dim">{fmtBytes(file.size)}</span>
            <button className="small" onClick={reset}>
              Choose another
            </button>
          </div>

          {previewM.isPending && <Spinner />}
          {previewM.error && <ErrorBox error={previewM.error} />}

          {preview && (
            <>
              <div className="toolbar" style={{ marginBottom: 12 }}>
                <label htmlFor="ds-name" style={{ marginBottom: 0 }}>
                  Dataset name
                </label>
                <input
                  id="ds-name"
                  className="mono"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  style={{ maxWidth: 260 }}
                />
                <button
                  className="primary"
                  disabled={!nameValid || importM.isPending}
                  onClick={() => importM.mutate({ f: file, target: name })}
                >
                  {importM.isPending ? "Importing…" : "Import"}
                </button>
              </div>
              {!nameValid && (
                <p className="hint" style={{ marginTop: 0 }}>
                  Lowercase letters, digits and underscores; must start with a
                  letter.
                </p>
              )}
              <label style={{ display: "flex", alignItems: "center", gap: 8, margin: "4px 0 8px" }}>
                <input
                  type="checkbox"
                  checked={asIceberg}
                  onChange={(e) => setAsIceberg(e.target.checked)}
                />
                <span style={{ fontSize: 13 }}>
                  Store as an Iceberg table — versioned, branchable, and readable
                  by Spark / Trino / DuckDB directly.
                </span>
              </label>
              {importM.error && <ErrorBox error={importM.error} />}

              <p className="dim" style={{ fontSize: 12.5 }}>
                {preview.columns.length} column
                {preview.columns.length === 1 ? "" : "s"}, showing{" "}
                {preview.sampled_rows} row
                {preview.sampled_rows === 1 ? "" : "s"}
                {preview.truncated ? " (first of more)" : ""} — types inferred
                from the file.
              </p>

              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      {preview.columns.map((c) => (
                        <th key={c.name}>
                          {c.name}
                          <div className="faint mono" style={{ fontWeight: 400 }}>
                            {c.type}
                          </div>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {preview.rows.slice(0, 10).map((row, i) => (
                      <tr key={i}>
                        {preview.columns.map((c) => (
                          <td key={c.name} className="mono">
                            {fmtValue(row[c.name])}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// --------------------------------------------------------------- federated

type FederatedType = "iceberg" | "delta" | "parquet" | "postgres";

/**
 * Register a table Laurelin governs but does not hold.
 *
 * Admin-only, and collapsed by default: this points the server at a remote
 * system with credentials, so it isn't the common path and shouldn't crowd the
 * import flow that is. The server probes the source before storing it, so an
 * unreachable table fails here rather than at first query.
 */
function FederatedPanel() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [type, setType] = useState<FederatedType>("iceberg");
  const [path, setPath] = useState("");
  const [url, setUrl] = useState("");
  const [table, setTable] = useState("");

  if (user?.role !== "admin") return null;

  const source =
    type === "postgres"
      ? { type, url, table }
      : { type, path };

  const complete =
    /^[a-z][a-z0-9_]*$/.test(name) &&
    (type === "postgres" ? url.trim() !== "" && table.trim() !== "" : path.trim() !== "");

  const register = useMutation({
    mutationFn: () =>
      api.put(`${API}/datasets/${name}/federated`, { source, description: "" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["datasets"] });
      setName("");
      setPath("");
      setUrl("");
      setTable("");
      setOpen(false);
    },
  });

  if (!open) {
    return (
      <div style={{ margin: "0 0 24px" }}>
        <button className="small" onClick={() => setOpen(true)}>
          Register a federated table…
        </button>
      </div>
    );
  }

  return (
    <div className="card" style={{ marginBottom: 24 }}>
      <div className="toolbar" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <label style={{ marginBottom: 0 }}>Register a federated table</label>
        <button className="small" onClick={() => setOpen(false)}>Cancel</button>
      </div>
      <p className="hint" style={{ marginTop: 4 }}>
        A table Laurelin governs and scans in place — it isn't copied, and it has
        no versions. Governed by the same ACLs and policies as any dataset.
      </p>

      <div className="toolbar" style={{ gap: 12, flexWrap: "wrap", marginTop: 8 }}>
        <div className="field">
          <label>Dataset name</label>
          <input className="mono" value={name} onChange={(e) => setName(e.target.value)}
            placeholder="external_orders" style={{ maxWidth: 220 }} />
        </div>
        <div className="field">
          <label>Kind</label>
          <select value={type} onChange={(e) => setType(e.target.value as FederatedType)}>
            <option value="iceberg">Iceberg</option>
            <option value="delta">Delta Lake</option>
            <option value="parquet">Parquet file/glob</option>
            <option value="postgres">PostgreSQL table</option>
          </select>
        </div>
      </div>

      {type === "postgres" ? (
        <div className="toolbar" style={{ gap: 12, flexWrap: "wrap" }}>
          <div className="field" style={{ flex: "1 1 260px" }}>
            <label>Connection URL</label>
            <input className="mono" value={url} onChange={(e) => setUrl(e.target.value)}
              placeholder="postgresql://user:pw@host:5432/db" />
          </div>
          <div className="field">
            <label>Table</label>
            <input className="mono" value={table} onChange={(e) => setTable(e.target.value)}
              placeholder="public.orders" />
          </div>
        </div>
      ) : (
        <div className="field">
          <label>Path or URI</label>
          <input className="mono" value={path} onChange={(e) => setPath(e.target.value)}
            placeholder={
              type === "iceberg"
                ? "s3://bucket/warehouse/db/table/metadata/….metadata.json"
                : type === "delta"
                  ? "s3://bucket/path/to/delta-table"
                  : "/data/exports/*.parquet or s3://bucket/file.parquet"
            } />
        </div>
      )}

      {register.error && <ErrorBox error={register.error} />}

      <div className="toolbar" style={{ marginTop: 8, justifyContent: "flex-end" }}>
        <button className="primary" disabled={!complete || register.isPending}
          onClick={() => register.mutate()}>
          {register.isPending ? "Probing source…" : "Register"}
        </button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ detail

function DatasetDetailPage() {
  const { name = "" } = useParams();
  const { data, isLoading, error } = useQuery({
    queryKey: ["dataset", name],
    queryFn: () => api.get<DatasetDetail>(`${API}/datasets/${name}`),
  });

  return (
    <div>
      <div style={{ marginBottom: 12 }}>
        <Link to="/datasets">← Datasets</Link>
      </div>
      {isLoading && <Spinner />}
      {error && <ErrorBox error={error} />}
      {data && <DatasetDetailBody detail={data} />}
    </div>
  );
}

function DatasetDetailBody({ detail }: { detail: DatasetDetail }) {
  const auth = useAuth();
  const federated = detail.kind === "federated";
  const schemaVersion =
    detail.versions.find((v) => v.version === detail.latest_version) ??
    detail.versions[detail.versions.length - 1];

  const versions = [...detail.versions].sort((a, b) => b.version - a.version);

  return (
    <div>
      <PageHeader
        title={detail.name}
        subtitle={detail.description || undefined}
        actions={
          detail.kind && detail.kind !== "managed" ? (
            <Badge tone={federated ? "blue" : "green"}>{detail.kind}</Badge>
          ) : undefined
        }
      />

      {federated ? (
        <FederatedSource source={detail.source} />
      ) : auth.can("editor") ? (
        <UploadControl name={detail.name} iceberg={detail.kind === "iceberg"} />
      ) : (
        <p className="faint" style={{ margin: "0 0 20px" }}>
          Uploading a new version requires the editor role.
        </p>
      )}

      {detail.kind === "iceberg" && auth.can("editor") && (
        <IcebergManager name={detail.name} />
      )}

      {!federated && <SchemaSection version={schemaVersion} />}

      {!federated && (
        <>
          <div className="toolbar" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
            <h3 style={{ marginBottom: 0 }}>Version history</h3>
            {auth.can("editor") && detail.kind !== "iceberg" && detail.latest_version != null && (
              <CompactButton name={detail.name} />
            )}
          </div>
          <VersionHistory versions={versions} />
        </>
      )}

      <h3>Row preview</h3>
      <RowPreview name={detail.name} schema={schemaVersion?.schema} />
    </div>
  );
}

/**
 * What a federated dataset shows instead of upload / versions: where the data
 * actually lives. It's scanned in place, so there are no versions to manage —
 * saying "no versions yet" would imply one is coming, which is wrong.
 */
function FederatedSource({ source }: { source?: Record<string, unknown> }) {
  const type = String(source?.type ?? "external");
  const location = String(source?.path ?? source?.table ?? source?.url ?? "");
  return (
    <div className="card" style={{ marginBottom: 20 }}>
      <label style={{ marginBottom: 4 }}>Federated source</label>
      <p className="hint" style={{ marginTop: 0 }}>
        Laurelin governs and scans this table in place — it isn't copied and has
        no versions. Same ACLs and policies as any dataset.
      </p>
      <div className="mono" style={{ fontSize: 13 }}>
        <Badge tone="blue">{type}</Badge>{" "}
        {location && <span className="dim">{location}</span>}
      </div>
    </div>
  );
}

// --------------------------------------------------------------- compaction

/**
 * Merge a dataset's Parquet parts back into one file.
 *
 * Appends are cheap but leave a version made of many small parts, and many
 * small files eventually slow scans. Compaction pays that cost once,
 * deliberately — which is why it's a button, not automatic (unless
 * LAURELIN_AUTO_COMPACT_PARTS is set on the server).
 */
function CompactButton({ name }: { name: string }) {
  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: () => api.post<DatasetVersion>(`${API}/datasets/${name}/compact`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dataset", name] });
      qc.invalidateQueries({ queryKey: ["datasets"] });
    },
  });
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      {m.isError && <ErrorBox error={m.error} />}
      {m.isSuccess && <span className="hint ok" style={{ margin: 0 }}>Compacted.</span>}
      <button
        className="small"
        disabled={m.isPending}
        onClick={() => m.mutate()}
        title="Merge the latest version's parts into one file"
      >
        {m.isPending ? "Compacting…" : "Compact"}
      </button>
    </div>
  );
}

// ------------------------------------------------------------------ upload

function UploadControl({ name, iceberg = false }: { name: string; iceberg?: boolean }) {
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<"replace" | "append">("replace");
  const [done, setDone] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (f: File) => {
      const form = new FormData();
      form.append("file", f);
      // An iceberg dataset's new versions must go through the iceberg writer,
      // or they'd land as managed Parquet parts on a table marked iceberg.
      const base = iceberg
        ? `${API}/datasets/${name}/iceberg`
        : `${API}/datasets/${name}/upload`;
      return api.upload<DatasetVersion>(`${base}?mode=${mode}`, form);
    },
    onSuccess: (v) => {
      qc.invalidateQueries({ queryKey: ["dataset", name] });
      qc.invalidateQueries({ queryKey: ["datasets"] });
      setDone(`Uploaded version v${v.version}.`);
      setFile(null);
    },
  });

  return (
    <div className="card" style={{ marginBottom: 20 }}>
      <label style={{ marginBottom: 0 }}>Upload new version</label>
      <div className="toolbar" style={{ marginTop: 10, marginBottom: 0 }}>
        <input
          type="file"
          accept=".csv,.parquet"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
            setDone(null);
            mutation.reset();
          }}
        />
        <select
          value={mode}
          onChange={(e) => setMode(e.target.value as "replace" | "append")}
          aria-label="Upload mode"
        >
          <option value="replace">Replace</option>
          <option value="append">Append</option>
        </select>
        <button
          className="primary"
          disabled={!file || mutation.isPending}
          onClick={() => file && mutation.mutate(file)}
        >
          {mutation.isPending ? "Uploading…" : "Upload"}
        </button>
      </div>
      <p className="hint" style={{ marginTop: 8, marginBottom: 0 }}>
        {mode === "replace"
          ? "Replace writes a new version containing only this file's rows."
          : "Append adds these rows to the existing ones and writes only the delta. The schema must match."}
      </p>
      {mutation.error && <ErrorBox error={mutation.error} />}
      {done && (
        <p className="hint ok" style={{ marginTop: 8 }}>
          {done}
        </p>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ schema

function SchemaSection({ version }: { version: DatasetVersion | undefined }) {
  const columns: Column<ColumnSchema>[] = [
    { label: "Column", render: (c) => c.name },
    { label: "Type", className: "mono", render: (c) => c.type },
  ];
  return (
    <div>
      <h3>Schema</h3>
      {version && version.schema.length > 0 ? (
        <DataTable
          columns={columns}
          rows={version.schema}
          rowKey={(c) => c.name}
        />
      ) : (
        <EmptyState>No schema available.</EmptyState>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ versions

function VersionHistory({ versions }: { versions: DatasetVersion[] }) {
  if (versions.length === 0) {
    return <EmptyState>No versions yet.</EmptyState>;
  }
  const columns: Column<DatasetVersion>[] = [
    { label: "Version", className: "mono", render: (v) => `v${v.version}` },
    { label: "Source", render: (v) => <Badge tone="blue">{v.source}</Badge> },
    { label: "Rows", className: "num", render: (v) => fmtNum(v.row_count) },
    {
      label: "Created",
      render: (v) => <span className="dim">{fmtTime(v.created_at)}</span>,
    },
    {
      label: "Build",
      className: "mono",
      render: (v) =>
        v.build_id ? v.build_id : <span className="faint">—</span>,
    },
  ];
  return (
    <DataTable
      columns={columns}
      rows={versions}
      rowKey={(v) => String(v.version)}
    />
  );
}

// ------------------------------------------------------------------ rows

function RowPreview({
  name,
  schema,
}: {
  name: string;
  schema: ColumnSchema[] | undefined;
}) {
  const [offset, setOffset] = useState(0);
  const { data, isLoading, error } = useQuery({
    queryKey: ["rows", name, offset],
    queryFn: () =>
      api.get<RowsPage>(
        `${API}/datasets/${name}/rows?limit=${PAGE_SIZE}&offset=${offset}`,
      ),
  });

  if (isLoading) return <Spinner />;
  if (error) return <ErrorBox error={error} />;
  if (!data) return null;

  const { rows, row_count } = data;
  if (rows.length === 0) {
    return <EmptyState>No rows to preview.</EmptyState>;
  }

  // Prefer schema column order; fall back to keys of the first row.
  const cols =
    schema && schema.length > 0
      ? schema.map((c) => c.name)
      : Object.keys(rows[0]);

  return (
    <div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {cols.map((c) => (
                <th key={c} className="mono">
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={offset + i}>
                {cols.map((c) => (
                  <td key={c} className="mono">
                    {fmtValue(row[c])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="pager">
        <button
          className="small"
          disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        >
          Prev
        </button>
        <button
          className="small"
          // For a federated dataset the total is unknown (row_count null), so
          // fall back to "was this page full?" — a short page means the end.
          disabled={
            row_count == null ? rows.length < PAGE_SIZE : offset + rows.length >= row_count
          }
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          Next
        </button>
        <span>
          Rows {offset + 1}–{offset + rows.length}
          {row_count == null ? "" : ` of ${fmtNum(row_count)}`}
        </span>
      </div>
    </div>
  );
}
