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
      render: (d) => <Link to={`/datasets/${d.name}`}>{d.name}</Link>,
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
      {isLoading && <Spinner />}
      {error && <ErrorBox error={error} />}
      {data &&
        (data.length === 0 ? (
          <EmptyState>No datasets yet.</EmptyState>
        ) : (
          <DataTable
            columns={columns}
            rows={data}
            rowKey={(d) => d.name}
            onRowClick={(d) => navigate(`/datasets/${d.name}`)}
          />
        ))}
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
  const schemaVersion =
    detail.versions.find((v) => v.version === detail.latest_version) ??
    detail.versions[detail.versions.length - 1];

  const versions = [...detail.versions].sort((a, b) => b.version - a.version);

  return (
    <div>
      <PageHeader title={detail.name} subtitle={detail.description || undefined} />

      {auth.can("editor") ? (
        <UploadControl name={detail.name} />
      ) : (
        <p className="faint" style={{ margin: "0 0 20px" }}>
          Uploading a new version requires the editor role.
        </p>
      )}

      <SchemaSection version={schemaVersion} />

      <h3>Version history</h3>
      <VersionHistory versions={versions} />

      <h3>Row preview</h3>
      <RowPreview name={detail.name} schema={schemaVersion?.schema} />
    </div>
  );
}

// ------------------------------------------------------------------ upload

function UploadControl({ name }: { name: string }) {
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (f: File) => {
      const form = new FormData();
      form.append("file", f);
      return api.upload<DatasetVersion>(`${API}/datasets/${name}/upload`, form);
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
        <button
          className="primary"
          disabled={!file || mutation.isPending}
          onClick={() => file && mutation.mutate(file)}
        >
          {mutation.isPending ? "Uploading…" : "Upload"}
        </button>
      </div>
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
          disabled={offset + rows.length >= row_count}
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          Next
        </button>
        <span>
          Rows {offset + 1}–{offset + rows.length} of {fmtNum(row_count)}
        </span>
      </div>
    </div>
  );
}
