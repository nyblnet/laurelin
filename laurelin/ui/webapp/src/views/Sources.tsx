// Data sources (connectors): pull external data into datasets.
//
// Rendered inside the Datasets view. Editors see the list and can trigger
// syncs (subject to dataset edit access); admins can add and delete sources.
// Secret config values (passwords, tokens) are redacted by the API.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../api";
import { useAuth } from "../auth";
import type { Source, SourceType } from "../types";
import {
  Badge,
  Column,
  DataTable,
  EmptyState,
  ErrorBox,
  FailureBadge,
  FailureNote,
  PageHeader,
  RedactedValue,
  Spinner,
  Withheld,
  fmtNum,
  fmtTime,
} from "../ui";

const NAME_RE = /^[a-z][a-z0-9_]*$/;

function typeTone(t: SourceType): "gold" | "blue" | "green" | "neutral" {
  // Exhaustive on purpose: a new SourceType must pick a tone here, not
  // inherit whatever the last else-branch happened to be.
  switch (t) {
    case "postgres":
      return "blue";
    case "http":
      return "green";
    case "file":
      return "gold";
    case "object_store":
      return "neutral";
  }
}

/**
 * Where this connector reads from, as much of it as this reader gets.
 *
 * `config` is admin-only under R2 — a source is admin-authored and editor-read,
 * so the crossing runs through the middle of the record. Returning `null` here
 * means "you were not sent it", which the caller renders as a statement rather
 * than as an empty cell. It used to be a `redact_mapping` denylist over key
 * names in the connector's own vocabulary, and round 3 read an ODBC keyword
 * string out of a `path` key no denylist covered.
 */
function configSummary(s: Source): string | null {
  const c = s.config;
  if (!c) return null;
  if (s.type === "postgres") {
    return String(c.table ?? c.query ?? "");
  }
  if (s.type === "http") return String(c.url ?? "");
  // Explicit, not a fall-through: an unknown future type must not silently
  // read somebody else's `path` key (see the round-3 note above).
  if (s.type === "object_store") return String(c.uri ?? "");
  return String(c.path ?? "");
}

function SyncButton({ source }: { source: Source }) {
  const qc = useQueryClient();
  const sync = useMutation({
    mutationFn: () => api.post(`${API}/sources/${source.name}/sync`, {}),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["sources"] });
      qc.invalidateQueries({ queryKey: ["datasets"] });
      qc.invalidateQueries({ queryKey: ["dataset", source.dataset] });
    },
  });
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
      <button
        className="small"
        disabled={sync.isPending}
        onClick={(e) => {
          e.stopPropagation();
          sync.mutate();
        }}
      >
        {sync.isPending ? "Syncing…" : "Sync now"}
      </button>
      {/* Visible text, not a tooltip: the reason a sync failed is the whole
          point of reading it, and prose that exists only in a title= is
          unreachable by keyboard and invisible until hovered. */}
      {sync.isError && (
        <span className="hint bad" style={{ margin: 0 }}>
          failed — {(sync.error as Error).message}
        </span>
      )}
    </span>
  );
}

function DeleteButton({ source }: { source: Source }) {
  const qc = useQueryClient();
  const del = useMutation({
    mutationFn: () => api.del(`${API}/sources/${source.name}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sources"] }),
  });
  return (
    <button
      className="small danger"
      disabled={del.isPending}
      onClick={(e) => {
        e.stopPropagation();
        if (window.confirm(`Delete source "${source.name}"? Synced dataset versions are kept.`)) {
          del.mutate();
        }
      }}
    >
      Delete
    </button>
  );
}

// The provider choice as the form asks it. "s3" and "s3_compatible" are the
// same wire value (`provider: "s3"`); the split exists so the endpoint field
// appears exactly when it is needed (MinIO, R2, …) and stays out of the way
// for real AWS.
type BucketProvider = "s3" | "s3_compatible" | "gcs";

function AddSourceForm({ onDone }: { onDone: () => void }) {
  const [type, setType] = useState<SourceType>("postgres");
  const [name, setName] = useState("");
  const [dataset, setDataset] = useState("");
  const [url, setUrl] = useState("");
  const [table, setTable] = useState("");
  const [query, setQuery] = useState("");
  const [path, setPath] = useState("");
  const [format, setFormat] = useState("");
  const [provider, setProvider] = useState<BucketProvider>("s3");
  const [uri, setUri] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [region, setRegion] = useState("");
  const [mode, setMode] = useState<"replace" | "append">("replace");
  const [accessKey, setAccessKey] = useState("");
  const [secretKey, setSecretKey] = useState("");

  const create = useMutation({
    mutationFn: () => {
      const config: Record<string, unknown> = {};
      if (type === "postgres") {
        config.url = url.trim();
        if (query.trim()) config.query = query.trim();
        else config.table = table.trim();
      } else if (type === "http") {
        config.url = url.trim();
        if (format) config.format = format;
      } else if (type === "object_store") {
        config.provider = provider === "gcs" ? "gcs" : "s3";
        config.uri = uri.trim();
        if (provider === "s3_compatible") config.endpoint_url = endpoint.trim();
        if (region.trim()) config.region = region.trim();
        if (format) config.format = format;
        if (mode !== "replace") config.mode = mode;
        // Both or neither: an absent pair means an anonymous/public bucket.
        // Never send empty strings — "" is not "unset" to the server.
        if (accessKey.trim() !== "" || secretKey.trim() !== "") {
          config.access_key_id = accessKey.trim();
          config.secret_access_key = secretKey.trim();
        }
      } else {
        config.path = path.trim();
        if (format) config.format = format;
      }
      return api.put<Source>(`${API}/sources/${name.trim()}`, {
        type,
        dataset: dataset.trim(),
        config,
      });
    },
    onSuccess: () => {
      setName("");
      setDataset("");
      setUrl("");
      setTable("");
      setQuery("");
      setPath("");
      setFormat("");
      setUri("");
      setEndpoint("");
      setRegion("");
      setMode("replace");
      setAccessKey("");
      setSecretKey("");
      onDone();
    },
  });

  const nameOk = NAME_RE.test(name.trim());
  const datasetOk = NAME_RE.test(dataset.trim());
  const configOk =
    type === "postgres"
      ? url.trim().startsWith("postgres") && (table.trim() !== "" || query.trim() !== "")
      : type === "http"
        ? /^https?:\/\//.test(url.trim())
        : type === "object_store"
          ? uri.trim().startsWith(provider === "gcs" ? "gs://" : "s3://") &&
            (provider !== "s3_compatible" || /^https?:\/\//.test(endpoint.trim())) &&
            (accessKey.trim() === "") === (secretKey.trim() === "")
          : path.trim() !== "";
  const canSubmit = nameOk && datasetOk && configOk && !create.isPending;

  return (
    <div className="card" style={{ marginTop: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 12 }}>Add source</div>
      <div className="toolbar" style={{ gap: 12, flexWrap: "wrap" }}>
        <div className="field">
          <label>Type</label>
          <select value={type} onChange={(e) => setType(e.target.value as SourceType)}>
            <option value="postgres">PostgreSQL</option>
            <option value="http">HTTP(S) file</option>
            <option value="file">Server file / glob</option>
            <option value="object_store">Object storage (S3 / GCS)</option>
          </select>
        </div>
        <div className="field">
          <label>Source name</label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="crm_accounts"
            autoComplete="off"
          />
        </div>
        <div className="field">
          <label>Target dataset</label>
          <input
            value={dataset}
            onChange={(e) => setDataset(e.target.value)}
            placeholder="accounts"
            autoComplete="off"
          />
        </div>
      </div>

      {type === "postgres" && (
        <div className="toolbar" style={{ gap: 12, flexWrap: "wrap" }}>
          <div className="field" style={{ flex: "1 1 320px" }}>
            <label>Connection URL</label>
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="postgresql://user:password@host:5432/db"
              autoComplete="off"
            />
          </div>
          <div className="field">
            <label>Table</label>
            <input
              value={table}
              onChange={(e) => setTable(e.target.value)}
              placeholder="public.orders"
              autoComplete="off"
              disabled={query.trim() !== ""}
            />
          </div>
          <div className="field" style={{ flex: "1 1 260px" }}>
            <label>…or SQL query</label>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="SELECT * FROM orders WHERE …"
              autoComplete="off"
            />
          </div>
        </div>
      )}

      {type === "http" && (
        <div className="toolbar" style={{ gap: 12, flexWrap: "wrap" }}>
          <div className="field" style={{ flex: "1 1 380px" }}>
            <label>URL (.csv, .parquet, .json, .jsonl, .avro)</label>
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://example.com/exports/orders.csv"
              autoComplete="off"
            />
          </div>
          <FormatSelect value={format} onChange={setFormat} />
        </div>
      )}

      {type === "object_store" && (
        <>
          <div className="toolbar" style={{ gap: 12, flexWrap: "wrap" }}>
            <div className="field">
              <label>Provider</label>
              <select
                value={provider}
                onChange={(e) => setProvider(e.target.value as BucketProvider)}
              >
                <option value="s3">Amazon S3</option>
                <option value="s3_compatible">S3-compatible (MinIO, R2, …)</option>
                <option value="gcs">Google Cloud Storage (HMAC)</option>
              </select>
            </div>
            <div className="field" style={{ flex: "1 1 340px" }}>
              <label>Bucket URI (key, prefix, or glob)</label>
              <input
                value={uri}
                onChange={(e) => setUri(e.target.value)}
                placeholder={
                  provider === "gcs"
                    ? "gs://bucket/prefix/*.parquet"
                    : "s3://bucket/prefix/*.parquet"
                }
                autoComplete="off"
              />
            </div>
            {provider === "s3_compatible" && (
              <div className="field" style={{ flex: "1 1 240px" }}>
                <label>Endpoint URL</label>
                <input
                  value={endpoint}
                  onChange={(e) => setEndpoint(e.target.value)}
                  placeholder="http://minio.internal:9000"
                  autoComplete="off"
                />
              </div>
            )}
            {provider !== "gcs" && (
              <div className="field">
                <label>Region (optional)</label>
                <input
                  value={region}
                  onChange={(e) => setRegion(e.target.value)}
                  placeholder="us-east-1"
                  autoComplete="off"
                />
              </div>
            )}
          </div>
          <div className="toolbar" style={{ gap: 12, flexWrap: "wrap" }}>
            <FormatSelect value={format} onChange={setFormat} />
            <div className="field">
              <label>Mode</label>
              <select
                value={mode}
                onChange={(e) => setMode(e.target.value as "replace" | "append")}
              >
                <option value="replace">Replace (full refresh)</option>
                <option value="append">Append (new objects only)</option>
              </select>
            </div>
            <div className="field">
              <label>Access key ID</label>
              <input
                value={accessKey}
                onChange={(e) => setAccessKey(e.target.value)}
                placeholder="leave blank for a public bucket"
                autoComplete="off"
              />
            </div>
            <div className="field">
              <label>Secret access key</label>
              <input
                type="password"
                value={secretKey}
                onChange={(e) => setSecretKey(e.target.value)}
                autoComplete="off"
              />
            </div>
          </div>
        </>
      )}

      {type === "file" && (
        <div className="toolbar" style={{ gap: 12, flexWrap: "wrap" }}>
          <div className="field" style={{ flex: "1 1 380px" }}>
            <label>Server path or glob</label>
            <input
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="/mnt/landing/orders/*.parquet"
              autoComplete="off"
            />
          </div>
          <FormatSelect value={format} onChange={setFormat} />
        </div>
      )}

      {create.isError && <ErrorBox error={create.error} />}
      <div className="toolbar" style={{ marginTop: 8, marginBottom: 0 }}>
        <button className="primary" disabled={!canSubmit} onClick={() => create.mutate()}>
          {create.isPending ? "Saving…" : "Save source"}
        </button>
        <span className="hint">
          Credentials are stored server-side and never shown again.
        </span>
      </div>
    </div>
  );
}

function FormatSelect({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="field">
      <label>Format</label>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">Infer from extension</option>
        <option value="csv">CSV</option>
        <option value="parquet">Parquet</option>
        <option value="json">JSON</option>
        <option value="jsonl">JSONL / NDJSON</option>
        <option value="avro">Avro</option>
      </select>
    </div>
  );
}

export function SourcesSection() {
  const auth = useAuth();
  const qc = useQueryClient();
  const [showAdd, setShowAdd] = useState(false);
  const isAdmin = auth.can("admin");

  const sourcesQ = useQuery({
    queryKey: ["sources"],
    queryFn: () => api.get<Source[]>(`${API}/sources`),
    enabled: auth.can("editor"),
  });

  // Viewers have no access to source configs at all.
  if (!auth.can("editor")) return null;

  const columns: Column<Source>[] = [
    { label: "Source", className: "mono", render: (s) => s.name },
    {
      label: "Type",
      render: (s) => <Badge tone={typeTone(s.type)}>{s.type}</Badge>,
    },
    {
      label: "From",
      className: "mono dim",
      // Two different absences, and they must not look alike. `null` is "your
      // role is not sent the connector config"; WITHHELD is "the server had it
      // and could not redact it safely". Blank would read as "no source
      // configured", and an operator's next move after reading that is to type
      // the connection string in again.
      render: (s) => {
        const summary = configSummary(s);
        return summary === null ? (
          <Withheld
            what="A connector's configuration"
            role="admin"
            why="It is the connection string, and it is the level that writes it that reads it."
          />
        ) : (
          <RedactedValue value={summary} />
        );
      },
    },
    { label: "Dataset", className: "mono", render: (s) => s.dataset },
    {
      label: "Last sync",
      render: (s) =>
        s.last_sync_status == null ? (
          <span className="faint">never</span>
        ) : s.last_sync_status === "succeeded" ? (
          <span>
            <Badge tone="green">ok</Badge>{" "}
            <span className="dim">
              {fmtNum(s.last_sync_rows ?? 0)} rows → v{s.last_sync_version} ·{" "}
              {fmtTime(s.last_sync_at!)}
            </span>
          </span>
        ) : (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            {/* R1: this used to be `title={s.last_sync_error}` — the driver's
                own sentence, which on a Postgres connect failure begins with
                the connection string. The badge now names Laurelin's own
                classification, which is what an operator was reading it for:
                "auth rejected" means rotate the credential, "endpoint
                unreachable" means the credential was never even tried. */}
            {s.last_sync_failure ? (
              <FailureBadge failure={s.last_sync_failure} />
            ) : (
              <Badge tone="red">failed</Badge>
            )}
            <span className="dim">{fmtTime(s.last_sync_at!)}</span>
          </span>
        ),
    },
    {
      label: "",
      render: (s) => (
        <span style={{ display: "inline-flex", gap: 8 }}>
          <SyncButton source={s} />
          {isAdmin && <DeleteButton source={s} />}
        </span>
      ),
    },
  ];

  return (
    <section id="data-sources" style={{ marginTop: 28 }}>
      <PageHeader
        title="Data sources"
        subtitle="Connectors that pull external data into datasets — PostgreSQL, HTTP exports, files landed on the server, or object-storage buckets (S3 / GCS)."
        actions={
          isAdmin ? (
            <button className="small" onClick={() => setShowAdd((v) => !v)}>
              {showAdd ? "Close" : "Add source"}
            </button>
          ) : undefined
        }
      />
      {/* The other ingest door, named up front. Each sync COPIES rows into a
          new version of the target dataset; registering an external table
          scans it in place and copies nothing. The two doors look identical
          until the data is stale, so each names the other. */}
      <p className="hint" style={{ marginTop: 0 }}>
        Each sync copies the rows into a new version of the target dataset. To
        query a table where it already lives — copying nothing —{" "}
        {isAdmin ? (
          <>
            use{" "}
            <button
              className="link-button"
              onClick={() =>
                document
                  .getElementById("external-table")
                  ?.scrollIntoView({ behavior: "smooth", block: "start" })
              }
            >
              Register an external table
            </button>{" "}
            above.
          </>
        ) : (
          <>
            an admin can register an external table from the button above the
            dataset list.
          </>
        )}
      </p>
      {showAdd && isAdmin && (
        <AddSourceForm
          onDone={() => {
            setShowAdd(false);
            qc.invalidateQueries({ queryKey: ["sources"] });
          }}
        />
      )}
      {sourcesQ.isLoading ? (
        <Spinner />
      ) : sourcesQ.isError ? (
        <ErrorBox error={sourcesQ.error} onRetry={() => sourcesQ.refetch()} />
      ) : sourcesQ.data!.length === 0 ? (
        <EmptyState>
          {/* The editor branch names the gate instead of ending in a full
              stop: capability-existence ("this needs an admin") is product
              documentation, not a secret — what stays undisclosed is any
              restriction on a specific object. */}
          No sources configured
          {isAdmin
            ? " — add one to pull external data."
            : " — adding sources requires an admin."}
        </EmptyState>
      ) : (
        <>
          <DataTable columns={columns} rows={sourcesQ.data!} rowKey={(s) => s.name} />
          {/* The badge is a status; this is the diagnosis. Spelled out under
              the table rather than hidden in a tooltip, because deciding
              between "rotate the credential" and "open the firewall" is the
              whole reason anyone reads a failed sync. */}
          {sourcesQ.data!.filter((s) => s.last_sync_failure).map((s) => (
            <div key={s.name} style={{ marginTop: 10 }}>
              <div className="mono dim" style={{ fontSize: 12 }}>{s.name}</div>
              {/* role: the fallback sentence differs for a viewer vs an
                  editor+ (SH3) — and this section is editor-gated, so the
                  reader here is never told a stronger role sees more. */}
              <FailureNote failure={s.last_sync_failure!} role={auth.role} />
            </div>
          ))}
        </>
      )}
    </section>
  );
}
