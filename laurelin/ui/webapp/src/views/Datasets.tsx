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
import { API, ApiError, api } from "../api";
import { useAuth } from "../auth";
import type {
  ColumnSchema,
  Dataset,
  DatasetDetail,
  DatasetKind,
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
  RedactedValue,
  Spinner,
  WithheldBox,
  fmtNum,
  fmtTime,
  fmtValue,
} from "../ui";
import { SourcesSection } from "./Sources";
import { IcebergManager } from "./IcebergManager";

const PAGE_SIZE = 50;

/**
 * A dataset whose bytes did not survive an import.
 *
 * The import stamps this sentinel into `source` for anything it could not
 * carry — a federated table's endpoint was withheld with the rest of the
 * credentials, and a metadata-only archive carries no Parquet at all. Reads
 * then fail with 409 rather than returning zero rows, because in a governance
 * product an empty result is indistinguishable from a working row policy. The
 * pill exists so that refusal is legible *before* someone clicks into it.
 */
function needsCredentials(d: { source_descriptor?: Record<string, unknown> }): boolean {
  return d.source_descriptor?.needs_credentials === true;
}

function importedDataState(d: { source_descriptor?: Record<string, unknown> }): string {
  const state = d.source_descriptor?.data_state;
  return typeof state === "string" ? state : "elsewhere";
}

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
  const auth = useAuth();
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
          {d.kind === "clickhouse" && <Badge tone="blue">clickhouse</Badge>}
          {/* Gold, not blue: blue is the peer tone federated and clickhouse
              already carry, and gold is what this UI uses for the thing you
              are meant to notice. */}
          {d.kind === "starrocks" && <Badge tone="gold">starrocks</Badge>}
          {needsCredentials(d) && <Badge tone="red">needs credentials</Badge>}
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
          <EmptyState>
            No datasets yet — import a file above to make one.
            {/* First-run next steps: three plain links, role-filtered like the
                nav. No modal tours, no checklists. */}
            <div style={{ marginTop: 8 }}>
              Then: chart it in <Link to="/analyses">Analyses</Link>
              {auth.can("editor") && (
                <>
                  {" "}· clean it with a <Link to="/pipelines">Pipeline</Link>
                </>
              )}{" "}
              · or follow the{" "}
              <a
                href="https://github.com/laurelin-data/laurelin/blob/main/docs/tutorials/01-ingest-transform-build.md"
                target="_blank"
                rel="noreferrer"
              >
                10-minute tutorial
              </a>{" "}
              {/* The docs ship in the repo but are not served by this app yet,
                  so on an offline or air-gapped install the link is dead —
                  name the in-repo path so the tutorial is still findable. */}
              <span className="faint">
                (docs/tutorials/01-ingest-transform-build.md in the Laurelin
                repo)
              </span>
              .
            </div>
          </EmptyState>
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

// "clickhouse" and "starrocks" are not federated *source* types — each selects
// a different engine and a different route. Kept in one dropdown because to the
// person registering, the question is the same one: where does this table live?
type FederatedType =
  | "starrocks"
  | "iceberg"
  | "delta"
  | "parquet"
  | "postgres"
  | "clickhouse";

/** Which registration route a kind uses. */
function routeFor(type: FederatedType): string {
  if (type === "starrocks") return "starrocks";
  if (type === "clickhouse") return "clickhouse";
  return "federated";
}

/**
 * The `source` body for a kind.
 *
 * Split out of the JSX because three special cases in one ternary stopped being
 * readable. Note the trap in the starrocks branch: its source `type` is the
 * literal "table" — `validate_source` accepts nothing else, and "starrocks"
 * there is a 400.
 */
function sourceFor(
  type: FederatedType,
  f: { path: string; url: string; table: string },
): Record<string, string> {
  if (type === "starrocks") return { type: "table", url: f.url, table: f.table };
  if (type === "postgres") return { type, url: f.url, table: f.table };
  // ClickHouse reads files here, so it registers a parquet source and differs
  // only in the route (and therefore the engine that scans it).
  return { type: type === "clickhouse" ? "parquet" : type, path: f.path };
}

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

  const clickhouse = type === "clickhouse";
  const starrocks = type === "starrocks";
  // Both remote-server kinds are addressed by url + table; the rest by a path.
  const remote = starrocks || type === "postgres";
  const source = sourceFor(type, { path, url, table });

  const complete =
    /^[a-z][a-z0-9_]*$/.test(name) &&
    (remote ? url.trim() !== "" && table.trim() !== "" : path.trim() !== "");

  const register = useMutation({
    mutationFn: () =>
      api.put(`${API}/datasets/${name}/${routeFor(type)}`, {
        source,
        description: "",
      }),
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
          Register an external table…
        </button>
      </div>
    );
  }

  return (
    <div className="card" style={{ marginBottom: 24 }}>
      <div className="toolbar" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <label style={{ marginBottom: 0 }}>Register an external table</label>
        <button className="small" onClick={() => setOpen(false)}>Cancel</button>
      </div>
      <p className="hint" style={{ marginTop: 4 }}>
        A table Laurelin governs and scans where it already lives — it isn't
        copied, and it has no versions. Which engine does the scanning depends on
        the kind you pick; the ACLs and policies are the same either way.
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
            {/* First on purpose: StarRocks is the serving tier this project
                leads with, and option order is the cheapest way to say so. */}
            <option value="starrocks">StarRocks (remote server)</option>
            <option value="iceberg">Iceberg</option>
            <option value="delta">Delta Lake</option>
            <option value="parquet">Parquet file/glob</option>
            <option value="postgres">PostgreSQL table</option>
            <option value="clickhouse">ClickHouse (embedded, read-only)</option>
          </select>
        </div>
      </div>

      {remote ? (
        <>
          <div className="toolbar" style={{ gap: 12, flexWrap: "wrap" }}>
            <div className="field" style={{ flex: "1 1 260px" }}>
              <label>Connection URL</label>
              <input className="mono" value={url} onChange={(e) => setUrl(e.target.value)}
                placeholder={
                  starrocks
                    ? "starrocks://laurelin_ro:password@fe.internal:9030/analytics"
                    : "postgresql://user:pw@host:5432/db"
                } />
              {starrocks && (
                <p className="hint">
                  The FE's MySQL-protocol port (9030 by default). The database
                  segment is required.
                </p>
              )}
            </div>
            <div className="field">
              <label>Table</label>
              <input className="mono" value={table} onChange={(e) => setTable(e.target.value)}
                placeholder={starrocks ? "analytics.orders" : "public.orders"} />
              {starrocks && (
                <p className="hint">
                  <code>table</code>, <code>db.table</code>, or{" "}
                  <code>catalog.db.table</code> for an external Iceberg/Hive
                  catalog.
                </p>
              )}
            </div>
          </div>
          {starrocks && (
            <div className="field">
              <p className="hint" style={{ marginTop: 0 }}>
                Use an account that holds SELECT and nothing else. Stacked
                statements execute on StarRocks, so this credential is the blast
                radius.
              </p>
              <p className="hint">
                Not yet run against a live StarRocks server — every StarRocks path
                here has only been exercised against an in-memory double.
              </p>
            </div>
          )}
        </>
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
          {clickhouse && (
            <p className="hint" style={{ marginTop: 4 }}>
              Scanned by ClickHouse embedded in this server (chdb) — there is no
              ClickHouse service to run, and Laurelin never writes to it.
              Connecting to a real ClickHouse server is not implemented.
            </p>
          )}
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

/**
 * The next-step doors from a dataset, so the page a new user lands on right
 * after their first import is not a dead end. The guidance used to live only
 * in the dataset LIST's empty state — which the import navigates away from,
 * and which disappears forever once one dataset exists — so the first thing a
 * new user successfully did stranded them.
 *
 * Same filter as the nav: a door renders only when the caller's role passes
 * the target's `needs` (Explore, Pipelines and Schedules are editor-gated;
 * SQL is the one authoring-adjacent surface a viewer can use). No
 * permissions/grants display for non-admins — revealing restriction-existence
 * to editors is a governance decision the attack passes have not reviewed —
 * so the admin door is one link to the Admin page, nothing more.
 */
export function DatasetOpenIn({ name }: { name: string }) {
  const auth = useAuth();
  const canEdit = auth.can("editor");
  const ds = encodeURIComponent(name);
  return (
    <div
      className="toolbar"
      style={{ gap: 14, flexWrap: "wrap", alignItems: "baseline", marginBottom: 16 }}
    >
      <span className="faint" style={{ fontSize: 12.5 }}>Open in:</span>
      {canEdit && (
        <Link to={`/explore?dataset=${ds}`} title="Shape this dataset into a chart by clicking, then save it to a dashboard">
          Explore — chart it
        </Link>
      )}
      <Link to={`/workbench?dataset=${ds}`} title="Query this dataset with SQL">
        SQL — query it
      </Link>
      {canEdit && (
        <Link to={`/pipelines?from=${ds}`} title="Start a no-code pipeline that reads this dataset">
          New pipeline from this dataset
        </Link>
      )}
      {canEdit && (
        <Link to="/schedules" title="Build pipelines on a schedule">
          Schedule builds
        </Link>
      )}
      {auth.can("admin") && (
        <Link to="/admin" title="Who can read this dataset — Admin → Dataset access">
          Dataset access
        </Link>
      )}
    </div>
  );
}

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
  // Mirrors DatasetInfo.scans_at_source on the server: what the *reader* cares
  // about is not who owns the table but whether there are local versions to
  // show. Iceberg has them; federated and clickhouse do not.
  const atSource =
    detail.kind === "federated" ||
    detail.kind === "clickhouse" ||
    detail.kind === "starrocks";
  const iceberg = detail.kind === "iceberg";
  const starrocks = detail.kind === "starrocks";
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
            <Badge tone={starrocks ? "gold" : iceberg ? "green" : "blue"}>
              {detail.kind}
            </Badge>
          ) : undefined
        }
      />

      {needsCredentials(detail) && <NeedsCredentialsBanner detail={detail} />}

      {/* Hidden while reads refuse: every door here runs a query, and offering
          four ways to hit the same 409 would repeat the banner in red boxes. */}
      {!needsCredentials(detail) && <DatasetOpenIn name={detail.name} />}

      {atSource ? (
        <FederatedSource
          source={detail.source}
          descriptor={detail.source_descriptor}
          kind={detail.kind}
        />
      ) : auth.can("editor") ? (
        <UploadControl name={detail.name} iceberg={iceberg} />
      ) : (
        <p className="faint" style={{ margin: "0 0 20px" }}>
          Uploading a new version requires the editor role.
        </p>
      )}

      {iceberg && auth.can("editor") && (
        <IcebergManager
          name={detail.name}
          compact={<CompactButton name={detail.name} iceberg />}
        />
      )}

      {!atSource && <SchemaSection version={schemaVersion} />}

      {!atSource && (
        <>
          <div className="toolbar" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
            <h3 style={{ marginBottom: 0 }}>Version history</h3>
            {auth.can("editor") && !iceberg && detail.latest_version != null && (
              <CompactButton name={detail.name} />
            )}
          </div>
          <VersionHistory versions={versions} />
        </>
      )}

      {/* Not rendered when the sentinel is set: the request would 409, and a
          red box under a heading called "Row preview" reads as a bug rather
          than as the deliberate refusal it is. The banner above says it once,
          in words, with the fix. */}
      {!needsCredentials(detail) && (
        <>
          <h3>Row preview</h3>
          <RowPreview
            name={detail.name}
            schema={schemaVersion?.schema}
            atSource={atSource}
          />
        </>
      )}
    </div>
  );
}

/**
 * A dataset that arrived in an import without the bytes or the endpoint it
 * needs.
 *
 * The server refuses to read it — 409, never an empty result set — and this is
 * the sentence that turns that refusal into an instruction. Which instruction
 * depends on why: a metadata-only archive is missing Parquet, while anything
 * scanned at source is missing the connection details the export withheld on
 * purpose.
 */
function NeedsCredentialsBanner({ detail }: { detail: DatasetDetail }) {
  const metadataOnly = importedDataState(detail) === "metadata_only";
  // `source` is admin-only, so the itemised list of withheld keys is too. The
  // banner itself is not: every role needs to know that reads will refuse,
  // because an empty result and a working row policy look identical.
  const missing = Object.entries(detail.source ?? {})
    .filter(([k, v]) => v === null && !k.startsWith("__"))
    .map(([k]) => k);
  return (
    <div className="error-box" style={{ marginBottom: 20 }}>
      <div style={{ fontWeight: 600 }}>
        This dataset was imported without {metadataOnly ? "its data" : "its endpoint"}.
      </div>
      <p style={{ fontSize: 12.5, marginTop: 6, marginBottom: 6 }}>
        {metadataOnly
          ? "Its governance, versions and schema arrived; the Parquet did not. Re-import from a full export, or upload a new version."
          : "Its rows live in a remote system, and the export withheld the connection details rather than shipping a credential in a file. Re-supply them and this clears."}
        {" "}Reads refuse with a 409 until then — returning zero rows would be
        indistinguishable from a row policy that is working correctly.
      </p>
      {missing.length > 0 && (
        <div className="mono" style={{ fontSize: 12 }}>
          withheld: {missing.join(", ")}
        </div>
      )}
      <div style={{ marginTop: 8, fontSize: 12 }}>
        <a href="#/admin">Admin → Portability → Re-supply checklist</a>
      </div>
    </div>
  );
}

/**
 * What a source-scanned dataset shows instead of upload / versions: where the
 * data actually lives. It's scanned in place, so there are no versions to
 * manage — saying "no versions yet" would imply one is coming, which is wrong.
 */
function FederatedSource({
  source,
  descriptor,
  kind,
}: {
  source?: Record<string, unknown>;
  descriptor?: Record<string, unknown>;
  kind?: DatasetKind;
}) {
  // R2: `source` is a connection config written by the three ADMIN registration
  // routes, so it reaches admin and nobody else — an editor authors the
  // *dataset*, not its endpoint. Below that the key is absent, and what an
  // editor gets instead is `source_descriptor`: which table, in which format,
  // built by Laurelin from an allowlist. The rest of this component would
  // otherwise render a badge saying "external" over an
  // empty location: a blank that reads as "misconfigured" about a dataset that
  // is working perfectly. Say the true thing instead.
  if (!source) {
    // Not a blank, and not nothing either: `source_descriptor` is what Laurelin
    // itself can say about the table — which one, in which format — assembled
    // from an allowlist rather than by subtracting from the operator's config.
    // Withholding a field is not a reason to withhold the screen.
    const shape = Object.entries(descriptor ?? {}).filter(
      ([k]) => k !== "needs_credentials" && k !== "data_state",
    );
    return (
      <WithheldBox what="Where this dataset's rows actually live" role="admin">
        <p style={{ marginTop: 8 }}>
          It is scanned in place in a remote system, and the endpoint is part of
          the connection config. Your access to the <em>rows</em> is unchanged —
          the same ACLs, row policy and column masks apply as to any dataset,
          and the preview below reads them.
        </p>
        {shape.length > 0 && (
          <div className="mono" style={{ fontSize: 12.5, marginTop: 8 }}>
            {shape.map(([k, v]) => (
              <div key={k}>
                <span className="faint">{k}</span> <span className="dim">{String(v)}</span>
              </div>
            ))}
          </div>
        )}
      </WithheldBox>
    );
  }
  const clickhouse = kind === "clickhouse";
  const starrocks = kind === "starrocks";
  // A StarRocks source's `type` is always the literal "table" — showing it
  // would be noise, so the badge carries the kind instead.
  const type = starrocks ? "starrocks" : String(source?.type ?? "external");
  const table = source?.table ? String(source.table) : "";
  const url = source?.url ? String(source.url) : "";
  const location = String(source?.path ?? source?.table ?? source?.url ?? "");
  return (
    <div className="card" style={{ marginBottom: 20 }}>
      <label style={{ marginBottom: 4 }}>
        {starrocks
          ? "StarRocks source"
          : clickhouse
            ? "ClickHouse source"
            : "Federated source"}
      </label>
      <p className="hint" style={{ marginTop: 0 }}>
        Laurelin governs and scans this table in place — it isn't copied and has
        no versions. Same ACLs and policies as any dataset.
        {clickhouse && " Read-only: Laurelin does not write to ClickHouse."}
        {starrocks &&
          " Read-only: Laurelin holds SELECT on this table and nothing else." +
            " No writes, no versions, no time travel."}
      </p>
      {starrocks ? (
        <>
          {/* Table and server both matter here: the table names what you are
              reading, the url names the cluster and the account reading it. */}
          <div className="mono" style={{ fontSize: 13 }}>
            <Badge tone="gold">{type}</Badge>{" "}
            {table && <span className="dim">{table}</span>}
          </div>
          {url && (
            <div className="mono faint" style={{ fontSize: 12, marginTop: 4 }}>
              <RedactedValue value={url} />
            </div>
          )}
          <p className="hint" style={{ marginBottom: 0 }}>
            Untested against a live StarRocks server — the read path has only run
            against an in-memory double.
          </p>
        </>
      ) : (
        <div className="mono" style={{ fontSize: 13 }}>
          <Badge tone="blue">{type}</Badge>{" "}
          {location && (
            <span className="dim">
              <RedactedValue value={location} />
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------- compaction

/**
 * Merge a dataset's parts back into one file.
 *
 * Appends are cheap but leave a version made of many small parts, and many
 * small files eventually slow scans. Compaction pays that cost once,
 * deliberately — which is why it's a button, not automatic (unless
 * LAURELIN_AUTO_COMPACT_PARTS is set on the server).
 *
 * `iceberg` changes only what the button *says*. The server rewrites the
 * Iceberg table's data files into one new snapshot, so the layout gets tidier
 * and history keeps every older snapshot — meaning it buys scan cost, not disk.
 * Promising "one file" there would be true; promising space back would not.
 */
export function CompactButton({ name, iceberg = false }: { name: string; iceberg?: boolean }) {
  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: () => api.post<DatasetVersion>(`${API}/datasets/${name}/compact`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dataset", name] });
      qc.invalidateQueries({ queryKey: ["datasets"] });
      qc.invalidateQueries({ queryKey: ["iceberg-snapshots", name] });
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
        title={
          iceberg
            ? "Rewrite this table's data files into one new snapshot. Earlier " +
              "snapshots stay readable, so this buys scan cost rather than disk."
            : "Merge the latest version's parts into one file"
        }
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
  atSource = false,
}: {
  name: string;
  schema: ColumnSchema[] | undefined;
  atSource?: boolean;
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
  if (error) {
    // The rows endpoint does not map a source-connection failure to a status
    // that carries its reason: StarRocksError, ClickHouseError and
    // FederationError are each caught when the table is *registered* but not
    // when it is *read*, so an unreachable remote arrives here as a bare 500
    // with the diagnostic left in the server log. Verified by pointing a
    // registered dataset at a host that does not resolve. Until the read path
    // maps those the way the registration routes already do, say where the
    // reason went rather than leaving "Internal Server Error" as the whole
    // story.
    const opaque =
      atSource && error instanceof ApiError && error.status >= 500;
    return (
      <>
        <ErrorBox error={error} />
        {opaque && (
          <p className="hint" style={{ marginTop: 0 }}>
            This table is scanned where it lives, so a failure here is usually
            the source being unreachable or the credentials being wrong. The
            server log holds the reason this response does not.
          </p>
        )}
      </>
    );
  }
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
