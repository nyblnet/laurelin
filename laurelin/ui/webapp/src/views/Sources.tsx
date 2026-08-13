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
  RedactedValue,
  Spinner,
  Withheld,
  fmtNum,
  fmtTime,
} from "../ui";

const NAME_RE = /^[a-z][a-z0-9_]*$/;

function typeTone(t: SourceType): "gold" | "blue" | "green" {
  return t === "postgres" ? "blue" : t === "http" ? "green" : "gold";
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
      {sync.isError && (
        <span className="hint bad" title={(sync.error as Error).message}>
          failed
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

function AddSourceForm({ onDone }: { onDone: () => void }) {
  const [type, setType] = useState<SourceType>("postgres");
  const [name, setName] = useState("");
  const [dataset, setDataset] = useState("");
  const [url, setUrl] = useState("");
  const [table, setTable] = useState("");
  const [query, setQuery] = useState("");
  const [path, setPath] = useState("");
  const [format, setFormat] = useState("");

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
            <label>URL (.csv or .parquet)</label>
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
    <section style={{ marginTop: 28 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
        <h2 style={{ margin: 0 }}>Data sources</h2>
        {isAdmin && (
          <button className="small" onClick={() => setShowAdd((v) => !v)}>
            {showAdd ? "Close" : "Add source"}
          </button>
        )}
      </div>
      <p className="dim" style={{ margin: "6px 0 0" }}>
        Connectors that pull external data into datasets — PostgreSQL, HTTP
        exports, or files landed on the server.
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
        <ErrorBox error={sourcesQ.error} />
      ) : sourcesQ.data!.length === 0 ? (
        <EmptyState>
          No sources configured{isAdmin ? " — add one to pull external data." : "."}
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
              <FailureNote failure={s.last_sync_failure!} />
            </div>
          ))}
        </>
      )}
    </section>
  );
}
