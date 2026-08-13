// Admin > Portability: export this workspace, import one, and prove the copy
// governs identically.
//
// The design constraint that shapes every panel: **a download button must not
// imply completeness it does not have.** An archive omits every credential by
// construction, and it cannot carry the rows of a federated, ClickHouse,
// StarRocks or Iceberg dataset — those are pointers to somewhere else. So the
// export flow is preview-first and the withheld list is rendered in full, with
// the exact route that puts each secret back. Downloading is gated on having
// been shown that list, not on having been offered a link to it.
//
// The import flow is preview-first for the mirror-image reason: import writes
// every governance rule verbatim and binds no principal, which means the
// reconstructed workspace denies people it should allow until an admin rebinds
// them by hand. That is the safe direction, but only if the operator can see
// the checklist — so the rebind list and the quarantined-bindings counts are
// part of the report, not a footnote.
//
// The verification panel is the product. A tarball helper has a download
// button; a portability guarantee has a decision matrix you can look at.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, ApiError, api } from "../../api";
import { useAuth } from "../../auth";
import type {
  Collision,
  Dataset,
  DatasetPlan,
  DataState,
  ExportManifest,
  FingerprintResponse,
  GovernanceCell,
  GovernanceFingerprint,
  Group,
  ImportReport,
  ImportState,
  PrincipalRef,
  User,
  Withheld,
} from "../../types";
import { Badge, EmptyState, ErrorBox, Spinner, fmtNum, fmtTime } from "../../ui";
import { InlineError, errDetail } from "./shared";

// The anonymous principal has no account to pick from a list, and "a principal
// who saw nothing at the source sees nothing at the destination" is half the
// round-trip claim — so it is always offered.
const ANONYMOUS = "anonymous";

function fmtBytes(n: number): string {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

const DATA_STATE_LABEL: Record<DataState, string> = {
  included: "in the archive",
  elsewhere: "lives elsewhere",
  elsewhere_absolute_path: "absolute path elsewhere",
  metadata_only: "metadata only",
};

function DataStateBadge({ plan }: { plan: DatasetPlan }) {
  const tone = plan.data_state === "included" ? "green" : "gold";
  return <Badge tone={tone}>{DATA_STATE_LABEL[plan.data_state] ?? plan.data_state}</Badge>;
}

function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div style={{ marginTop: 20 }}>
      <div style={{ fontWeight: 600, fontSize: 13 }}>{title}</div>
      {hint && (
        <p className="dim" style={{ fontSize: 12, marginTop: 2, marginBottom: 8 }}>
          {hint}
        </p>
      )}
      {children}
    </div>
  );
}

// --------------------------------------------------------------- manifest views

/** What the archive carries, per dataset. Amber wherever the bytes are not in it. */
function DatasetStateTable({ plans }: { plans: DatasetPlan[] }) {
  if (plans.length === 0) return <EmptyState>No datasets.</EmptyState>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Dataset</th>
            <th>Kind</th>
            <th>Data</th>
            <th className="num">Versions</th>
            <th className="num">Parts</th>
            <th className="num">Size</th>
            <th>Why</th>
          </tr>
        </thead>
        <tbody>
          {plans.map((d) => (
            <tr key={d.name}>
              <td className="mono">{d.name}</td>
              <td className="dim">{d.kind}</td>
              <td>
                <DataStateBadge plan={d} />
              </td>
              <td className="num">{fmtNum(d.versions)}</td>
              <td className="num">{fmtNum(d.parts)}</td>
              <td className="num">{d.bytes ? fmtBytes(d.bytes) : "—"}</td>
              <td className="dim" style={{ fontSize: 12 }}>
                {d.reason || d.note || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * The secrets posture, rendered in full.
 *
 * This is the table the whole page exists for. The export omits credentials by
 * column allowlist rather than redacting them by pattern, which means a failure
 * shows up as a *missing* field at the destination rather than as a leaked one
 * — but only if somebody is told which fields went missing and where to type
 * them back in. That is this table.
 */
function WithheldTable({ withheld }: { withheld: Withheld[] }) {
  if (withheld.length === 0) {
    return (
      <EmptyState>
        Nothing withheld — this workspace holds no stored credentials.
      </EmptyState>
    );
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Where</th>
            <th>Field</th>
            <th>Needed for</th>
            <th>Re-supply</th>
          </tr>
        </thead>
        <tbody>
          {withheld.map((w, i) => (
            <tr key={`${w.table}.${w.row}.${w.field}.${i}`}>
              <td className="mono">
                {w.table}
                {w.row === "*" ? (
                  <span className="faint"> (every row)</span>
                ) : (
                  <span className="dim"> / {w.row}</span>
                )}
              </td>
              <td className="mono">{w.field}</td>
              <td className="dim" style={{ fontSize: 12 }}>
                {w.required_for || w.reason}
              </td>
              <td className="mono dim" style={{ fontSize: 12 }}>
                {w.resupply || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TablesTable({ manifest }: { manifest: ExportManifest }) {
  const rows = Object.entries(manifest.tables).sort((a, b) => a[0].localeCompare(b[0]));
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Table</th>
            <th className="num">Rows</th>
            <th>Columns carried</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, stat]) => (
            <tr key={name}>
              <td className="mono">{name}</td>
              <td className="num">{fmtNum(stat.rows)}</td>
              <td className="mono dim" style={{ fontSize: 12 }}>
                {stat.columns.join(", ")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function NotExportedTable({ manifest }: { manifest: ExportManifest }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Table</th>
            <th>Class</th>
            <th>Why not</th>
            <th>Rebuild</th>
          </tr>
        </thead>
        <tbody>
          {manifest.not_exported.map((t) => (
            <tr key={t.table}>
              <td className="mono">{t.table}</td>
              <td>
                <Badge tone={t.cls === "ephemeral" ? "red" : "neutral"}>{t.cls}</Badge>
              </td>
              <td className="dim" style={{ fontSize: 12 }}>
                {t.reason}
              </td>
              <td className="mono dim" style={{ fontSize: 12 }}>
                {t.rebuild || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PrincipalsTable({ principals }: { principals: PrincipalRef[] }) {
  if (principals.length === 0) return <EmptyState>No principals referenced.</EmptyState>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Principal</th>
            <th>Kind</th>
            <th>Named by</th>
          </tr>
        </thead>
        <tbody>
          {principals.map((p) => (
            <tr key={`${p.kind}:${p.name}`}>
              <td className="mono">{p.name}</td>
              <td>
                <Badge tone={p.kind === "group" ? "blue" : "neutral"}>{p.kind}</Badge>
              </td>
              <td className="mono dim" style={{ fontSize: 12 }}>
                {p.referenced_by.join(", ")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ContentWarningBanner({ manifest }: { manifest: ExportManifest }) {
  if (manifest.content_warnings.length === 0) return null;
  return (
    <div className="error-box" style={{ marginTop: 12 }}>
      <div style={{ fontWeight: 600 }}>
        {manifest.content_warnings.length} place(s) in this workspace's authored
        content look like they hold a credential.
      </div>
      <p style={{ fontSize: 12, marginTop: 6, marginBottom: 6 }}>
        Pipelines, ontology YAML and authored config travel near-verbatim —
        transform files are <code>exec</code>'d unsandboxed on every build, and a
        dashboard panel's SQL runs as written — so the export will not edit them
        behind your back. Fix them, or carry them as they are.
      </p>
      <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12 }}>
        {manifest.content_warnings.map((w, i) => (
          <li key={i} className="mono">
            {w.line ? `${w.file}:${w.line}` : w.file} ({w.pattern}) — {w.preview}
          </li>
        ))}
      </ul>
    </div>
  );
}

function OriginSummary({ manifest }: { manifest: ExportManifest }) {
  const o = manifest.origin;
  return (
    <dl className="kv">
      <dt>Workspace</dt>
      <dd>
        {o.workspace_name} <span className="faint mono">({o.workspace_dir})</span>
      </dd>
      <dt>Origin id</dt>
      <dd className="mono" style={{ fontSize: 12 }}>
        {o.origin_id}
      </dd>
      <dt>Metadata</dt>
      <dd>{o.metadata_dialect}</dd>
      <dt>Data plane</dt>
      <dd>
        {o.data_plane}
        {o.data_plane !== "local" && (
          <span className="faint"> — object-store export is unverified; opt in explicitly</span>
        )}
      </dd>
      <dt>Mode</dt>
      <dd>
        {o.mode}
        {o.multi_slug ? ` (${o.multi_slug})` : ""}
      </dd>
      <dt>Laurelin</dt>
      <dd>
        {manifest.laurelin_version} · format v{manifest.format_version}
      </dd>
      <dt>Created</dt>
      <dd>
        {fmtTime(manifest.created_at)} by <span className="mono">{manifest.created_by}</span>
      </dd>
    </dl>
  );
}

/** The three tables the spec puts in front of every download and every import. */
function ManifestReport({ manifest }: { manifest: ExportManifest }) {
  const elsewhere = manifest.datasets.filter((d) => d.data_state !== "included");
  return (
    <>
      <Section title="Origin">
        <OriginSummary manifest={manifest} />
      </Section>

      <Section
        title={`Datasets (${manifest.datasets.length}) — ${elsewhere.length} whose data is not in the archive`}
        hint="Only managed datasets hold Parquet that Laurelin owns. Everything else is a pointer to a system this archive cannot carry."
      >
        <DatasetStateTable plans={manifest.datasets} />
      </Section>

      <Section
        title={`Withheld secrets (${manifest.withheld.length})`}
        hint="Omitted, never redacted: a redactor that misses fails silently, an omission fails loudly at import. Each row names the route that puts it back."
      >
        <WithheldTable withheld={manifest.withheld} />
      </Section>

      <Section
        title={`Metadata tables carried (${Object.keys(manifest.tables).length})`}
        hint="Row-level JSONL through the store, not a copy of metadata.db — a byte copy cannot restore onto the other backend."
      >
        <TablesTable manifest={manifest} />
      </Section>

      <Section
        title={`Tables deliberately absent (${manifest.not_exported.length})`}
        hint="Derived tables are recomputed at the destination; ephemeral ones are live credentials or in-flight work."
      >
        <NotExportedTable manifest={manifest} />
      </Section>

      <Section
        title={`Principals the rules name (${manifest.principals.length})`}
        hint="Import creates none of these. Every one is a deliberate, audited admin act at the destination."
      >
        <PrincipalsTable principals={manifest.principals} />
      </Section>

      <Section
        title="Environment to re-supply"
        hint="Never in the workspace, so no archive could have carried it."
      >
        <div className="mono dim" style={{ fontSize: 12, lineHeight: 1.7 }}>
          {manifest.environment_resupply.join(" · ")}
        </div>
      </Section>

      {Object.keys(manifest.nulled_error_fields).length > 0 && (
        <Section
          title="Failure history nulled"
          // R1 means the newer `*_failure_json` columns are safe by
          // construction — nothing of a driver's is in them. They are dropped
          // anyway: an export is a file that leaves the building, and there is
          // no reason for it to carry a history of which of your endpoints were
          // unreachable. The older `*_error` columns are dropped because they
          // held free-form driver text, which no redactor covers.
          hint="Failure records and any legacy driver error text are dropped from an export."
        >
          <div className="mono dim" style={{ fontSize: 12 }}>
            {Object.entries(manifest.nulled_error_fields)
              .map(([field, n]) => `${field}: ${n}`)
              .join(" · ")}
          </div>
        </Section>
      )}
    </>
  );
}

// --------------------------------------------------------------- export panel

interface ExportOpts {
  metadata_only: boolean;
  include_audit: boolean;
  include_membership: boolean | null;
  allow_content_warnings: boolean;
  allow_remote_data_plane: boolean;
  fingerprint: boolean;
  gzip: boolean | null;
}

function exportQuery(opts: ExportOpts, withGzip: boolean): string {
  const q = new URLSearchParams();
  q.set("metadata_only", String(opts.metadata_only));
  q.set("include_audit", String(opts.include_audit));
  q.set("allow_content_warnings", String(opts.allow_content_warnings));
  q.set("allow_remote_data_plane", String(opts.allow_remote_data_plane));
  q.set("fingerprint", String(opts.fingerprint));
  if (opts.include_membership !== null) {
    q.set("include_membership", String(opts.include_membership));
  }
  if (withGzip && opts.gzip !== null) q.set("gzip", String(opts.gzip));
  return q.toString();
}

function Check({
  checked,
  onChange,
  label,
  hint,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint?: string;
  disabled?: boolean;
}) {
  return (
    <label className="check">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      {label}
      {hint && <span className="check-hint">{hint}</span>}
    </label>
  );
}

function ExportPanel() {
  const auth = useAuth();
  const [opts, setOpts] = useState<ExportOpts>({
    metadata_only: false,
    include_audit: true,
    include_membership: auth.multi ? false : null,
    allow_content_warnings: false,
    allow_remote_data_plane: false,
    fingerprint: false,
    gzip: null,
  });
  // The preview the operator actually read, pinned to the exact options it was
  // taken with. Changing any switch invalidates it, because a download gated on
  // a stale preview is a download gated on nothing.
  const [reviewed, setReviewed] = useState<{ key: string; manifest: ExportManifest } | null>(
    null,
  );
  const [acknowledged, setAcknowledged] = useState(false);

  const key = exportQuery(opts, false);
  const set = <K extends keyof ExportOpts>(k: K, v: ExportOpts[K]) => {
    setOpts((o) => ({ ...o, [k]: v }));
    setReviewed(null);
    setAcknowledged(false);
  };

  const preview = useMutation({
    mutationFn: () => api.get<ExportManifest>(`${API}/workspace/export/preview?${key}`),
    onSuccess: (manifest) => setReviewed({ key, manifest }),
  });

  const manifest = reviewed?.key === key ? reviewed.manifest : null;
  const withheldCount = manifest?.withheld.length ?? 0;
  const elsewhereCount =
    manifest?.datasets.filter((d) => d.data_state !== "included").length ?? 0;
  const needsAck = !!manifest && (withheldCount > 0 || elsewhereCount > 0);
  const canDownload = !!manifest && (!needsAck || acknowledged);

  return (
    <div className="card" style={{ marginBottom: 20 }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>Export this workspace</div>
      <p className="dim" style={{ fontSize: 12.5, marginTop: 0 }}>
        A POSIX tar stream: governance rows as JSONL, ontology and pipelines byte-faithful,
        and — unless you ask for metadata only — the Parquet parts of every managed dataset.
        Inspect it anywhere with <code>tar tvf</code>; nothing here is a proprietary blob.
      </p>

      <div style={{ marginTop: 12 }}>
        <Check
          checked={opts.metadata_only}
          onChange={(v) => set("metadata_only", v)}
          label="Metadata only"
          hint="Governance, ontology and pipelines without the rows. A few hundred KB instead of a terabyte — and the way to audit what a full export would carry."
        />
        <Check
          checked={opts.include_audit}
          onChange={(v) => set("include_audit", v)}
          label="Include the audit log"
          hint="History travels by default. Audit ids are reassigned at the destination; ordering survives."
        />
        <Check
          checked={opts.fingerprint}
          onChange={(v) => set("fingerprint", v)}
          label="Embed the governance fingerprint"
          hint="Writes the source's decision matrix into the manifest so the destination can be proved equivalent. Reads every managed dataset once per principal — cheap on a small workspace, not on a large one."
        />
        {auth.multi && (
          <Check
            checked={opts.include_membership === true}
            onChange={(v) => set("include_membership", v)}
            label="Include workspace membership"
            hint="In multi-workspace mode a user's effective role lives in the control plane, outside this workspace. Without it the export cannot reproduce a single access decision — and reading it needs a server administrator."
          />
        )}
        <Check
          checked={opts.gzip === true}
          onChange={(v) => set("gzip", v ? true : null)}
          label="Force gzip"
          hint="On by default for a metadata-only export; off for a full one, because Parquet is already compressed."
        />
        <Check
          checked={opts.allow_remote_data_plane}
          onChange={(v) => set("allow_remote_data_plane", v)}
          label="Allow an object-store data plane"
          hint="Streaming parts out of S3/GCS/Azure has not been verified end to end, so a full export from one is opt-in rather than silently attempted."
        />
      </div>

      <div className="toolbar" style={{ marginTop: 8 }}>
        <button
          className="primary"
          disabled={preview.isPending}
          onClick={() => preview.mutate()}
        >
          {preview.isPending ? "Building manifest…" : "Preview what this carries"}
        </button>
        {manifest && (
          <span className="dim" style={{ fontSize: 12 }}>
            {fmtNum(manifest.estimated_parts ?? 0)} parts ·{" "}
            {fmtBytes(manifest.estimated_part_bytes ?? 0)}
          </span>
        )}
      </div>

      {preview.isError && <ExportRefusalBox err={preview.error} opts={opts} onSet={set} />}

      {manifest && (
        <>
          <ContentWarningBanner manifest={manifest} />
          <ManifestReport manifest={manifest} />

          {needsAck && (
            <div className="card attention" style={{ marginTop: 16 }}>
              <Check
                checked={acknowledged}
                onChange={setAcknowledged}
                label={`I have read what this archive does not contain: ${withheldCount} withheld secret${
                  withheldCount === 1 ? "" : "s"
                } and ${elsewhereCount} dataset${
                  elsewhereCount === 1 ? "" : "s"
                } whose data lives elsewhere.`}
                hint="The Parquet in this archive is pre-policy: the rows column masks and row policies hide from most principals at the source. Leaving the platform is the point; sharing it with a colleague is not."
              />
            </div>
          )}

          <div className="toolbar" style={{ marginTop: 12, justifyContent: "flex-end" }}>
            {/* A real anchor, not fetch()+Blob: the browser streams an
                attachment straight to disk, while a Blob would hold the whole
                archive in the tab's memory — which defeats the point of a
                writer that never buffers more than a megabyte. */}
            <a
              className="btn primary"
              href={canDownload ? `${API}/workspace/export?${exportQuery(opts, true)}` : undefined}
              aria-disabled={!canDownload}
              onClick={(e) => {
                if (!canDownload) e.preventDefault();
              }}
            >
              Download archive
            </a>
          </div>
        </>
      )}

      {!manifest && !preview.isPending && (
        <p className="dim" style={{ fontSize: 12, marginTop: 12 }}>
          Preview first. The archive omits every stored credential and cannot carry the rows
          of a dataset Laurelin does not hold — a download button offered before you have
          seen that list would be claiming a completeness it does not have.
        </p>
      )}
    </div>
  );
}

/**
 * A refusal is a decision nobody has made yet, so it is rendered with the
 * switch that makes it, not just as red text.
 */
function ExportRefusalBox({
  err,
  opts,
  onSet,
}: {
  err: unknown;
  opts: ExportOpts;
  onSet: <K extends keyof ExportOpts>(k: K, v: ExportOpts[K]) => void;
}) {
  const detail = errDetail(err);
  const isRefusal = err instanceof ApiError && err.status === 409;
  if (!isRefusal) return <InlineError err={err} />;

  const pipelines = detail.includes("authored content");
  const remote = detail.includes("object store");
  const membership = detail.includes("multi-workspace mode");

  return (
    <div className="error-box" style={{ marginTop: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>The export stopped.</div>
      <div style={{ fontSize: 12.5 }}>{detail}</div>
      <div className="toolbar" style={{ marginTop: 10 }}>
        {pipelines && !opts.allow_content_warnings && (
          <button className="small" onClick={() => onSet("allow_content_warnings", true)}>
            Carry the flagged content as it is
          </button>
        )}
        {remote && !opts.allow_remote_data_plane && (
          <button className="small" onClick={() => onSet("allow_remote_data_plane", true)}>
            Attempt the object-store export
          </button>
        )}
        {membership && (
          <>
            <button className="small" onClick={() => onSet("include_membership", true)}>
              Include membership
            </button>
            <button className="small" onClick={() => onSet("include_membership", false)}>
              Accept a governance-incomplete export
            </button>
          </>
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------- import panel

function CollisionTable({ collisions }: { collisions: Collision[] }) {
  if (collisions.length === 0) return <EmptyState>No collisions.</EmptyState>;
  const tone = (s: Collision["severity"]) =>
    s === "refusal" ? "red" : s === "widening-risk" ? "gold" : "neutral";
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Severity</th>
            <th>What</th>
            <th>Detail</th>
            <th>Resolution</th>
          </tr>
        </thead>
        <tbody>
          {collisions.map((c, i) => (
            <tr key={i}>
              <td>
                <Badge tone={tone(c.severity)}>{c.severity}</Badge>
              </td>
              <td className="mono">
                {c.kind} {c.name}
              </td>
              <td className="dim" style={{ fontSize: 12 }}>
                {c.detail}
              </td>
              <td className="dim" style={{ fontSize: 12 }}>
                {c.resolution || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CountPills({ counts }: { counts: Record<string, number> }) {
  const entries = Object.entries(counts).sort((a, b) => a[0].localeCompare(b[0]));
  if (entries.length === 0) return <span className="faint">none</span>;
  return (
    <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
      {entries.map(([k, v]) => (
        <span key={k} className="mono dim" style={{ fontSize: 12 }}>
          {k}={fmtNum(v)}
        </span>
      ))}
    </span>
  );
}

function ImportPanel({ onImported }: { onImported: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  // A browser upload is the wrong shape for a real migration: the archive is
  // the size of the data plane, and a multipart POST of half a terabyte through
  // a tab is not a plan. So a path on the server is a first-class option — the
  // route behind it is superadmin, which in single-workspace mode is the
  // workspace admin, and in multi mode stops one tenant naming another's files.
  const [path, setPath] = useState("");
  const [dragging, setDragging] = useState(false);
  const [merge, setMerge] = useState(false);
  const [renamePrefix, setRenamePrefix] = useState("");
  const [metadataOnly, setMetadataOnly] = useState(false);
  const [report, setReport] = useState<ImportReport | null>(null);

  const source: "file" | "path" | null = file ? "file" : path.trim() ? "path" : null;

  const params = (dryRun: boolean, confirm?: string) => {
    const q = new URLSearchParams();
    q.set("dry_run", String(dryRun));
    q.set("merge", String(merge));
    q.set("metadata_only", String(metadataOnly));
    if (renamePrefix.trim()) q.set("rename_prefix", renamePrefix.trim());
    if (confirm) q.set("confirm", confirm);
    return q.toString();
  };

  const send = (dryRun: boolean, confirm?: string) => {
    const query = params(dryRun, confirm);
    if (file) {
      const form = new FormData();
      form.append("file", file);
      return api.upload<ImportReport>(`${API}/workspace/import?${query}`, form);
    }
    return api.post<ImportReport>(`${API}/workspace/import/from-path?${query}`, {
      path: path.trim(),
    });
  };

  const dry = useMutation({
    mutationFn: () => send(true),
    onSuccess: setReport,
  });
  const apply = useMutation({
    // The merge contract is unchanged by the UI: phase two quotes back the
    // digest of the report phase one produced, so a report that changed between
    // being read and being applied refuses.
    mutationFn: () => send(false, merge ? report?.report_sha256 : undefined),
    onSuccess: (r) => {
      setReport(r);
      onImported();
    },
  });

  const reset = () => {
    setFile(null);
    setReport(null);
    dry.reset();
    apply.reset();
  };

  const choose = (f: File | undefined | null) => {
    if (!f) return;
    setFile(f);
    setPath("");
    setReport(null);
    dry.reset();
    apply.reset();
  };

  const refusals = (report?.collisions ?? []).filter((c) => c.severity === "refusal");
  const canApply = !!report && !report.applied && refusals.length === 0;

  return (
    <div className="card" style={{ marginBottom: 20 }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>Import an archive</div>
      <p className="dim" style={{ fontSize: 12.5, marginTop: 0 }}>
        Every governance rule lands verbatim and <strong>no principal is bound</strong>:
        users are not created, imported groups start empty, clearances are quarantined. The
        reconstructed workspace can only be narrower than the original until an admin
        rebinds people deliberately.
      </p>

      {!file && (
        <>
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
            <p>Drop a Laurelin export (.tar or .tar.gz) here</p>
            <label className="link-button">
              or choose a file
              <input
                type="file"
                accept=".tar,.tgz,.gz"
                style={{ display: "none" }}
                onChange={(e) => choose(e.target.files?.[0])}
              />
            </label>
          </div>
          <div className="field" style={{ marginTop: 12 }}>
            <label>…or a path on the server</label>
            <input
              className="mono"
              value={path}
              placeholder="/srv/laurelin/acme-prod-20260810.tar"
              onChange={(e) => {
                setPath(e.target.value);
                setReport(null);
                dry.reset();
                apply.reset();
              }}
            />
            <div className="hint">
              For an archive that is already on this host. Reading it in place avoids
              pushing the whole data plane back through a browser — and needs a server
              administrator, because the path is on the host rather than in this workspace.
            </div>
          </div>
        </>
      )}

      {source && (
        <>
          <div className="toolbar" style={{ marginBottom: 12, marginTop: 12 }}>
            <span className="mono">{file ? file.name : path.trim()}</span>
            {file && <span className="dim">{fmtBytes(file.size)}</span>}
            {file && (
              <button className="small" onClick={reset}>
                Choose another
              </button>
            )}
          </div>

          <Check
            checked={merge}
            onChange={(v) => {
              setMerge(v);
              setReport(null);
            }}
            label="Merge into this workspace (it is not empty)"
            hint="With username and marking name as the only join keys, an existing group starts matching the imported grants the instant the rows land. Review the collision report first."
          />
          <Check
            checked={metadataOnly}
            onChange={(v) => {
              setMetadataOnly(v);
              setReport(null);
            }}
            label="Skip the data"
            hint="Streams past data/** and restores governance only. Managed datasets then refuse reads rather than returning zero rows."
          />
          {merge && (
            <div className="field" style={{ maxWidth: 260 }}>
              <label>Rename prefix for colliding datasets</label>
              <input
                className="mono"
                value={renamePrefix}
                placeholder="imported_"
                onChange={(e) => {
                  setRenamePrefix(e.target.value);
                  setReport(null);
                }}
              />
              <div className="hint">
                A dataset name collision is two different tables claiming one identity —
                there is no safe merge without one.
              </div>
            </div>
          )}

          <div className="toolbar" style={{ marginTop: 8 }}>
            <button className="primary" disabled={dry.isPending} onClick={() => dry.mutate()}>
              {dry.isPending ? "Reading the archive…" : "Preview this import"}
            </button>
            <button
              disabled={!canApply || apply.isPending}
              onClick={() => apply.mutate()}
              title={
                canApply ? undefined : "Preview first — an import rewrites governance."
              }
            >
              {apply.isPending ? "Importing…" : "Import for real"}
            </button>
          </div>
        </>
      )}

      {dry.isError && (
        <ImportRefusalBox
          err={dry.error}
          merge={merge}
          prefix={renamePrefix}
          onMerge={() => {
            setMerge(true);
            setReport(null);
            dry.reset();
          }}
        />
      )}
      {apply.isError && (
        <ImportRefusalBox err={apply.error} merge={merge} prefix={renamePrefix} />
      )}

      {report && <ImportReportView report={report} />}
    </div>
  );
}

function ImportRefusalBox({
  err,
  merge,
  prefix,
  onMerge,
}: {
  err: unknown;
  merge: boolean;
  prefix: string;
  onMerge?: () => void;
}) {
  const detail = errDetail(err);
  if (!(err instanceof ApiError) || err.status !== 409) return <InlineError err={err} />;
  const collided = detail.includes("no safe merge");
  const notEmpty = detail.includes("not empty");
  return (
    <div className="error-box" style={{ marginTop: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>The import stopped.</div>
      <div style={{ fontSize: 12.5 }}>{detail}</div>
      {notEmpty && !merge && onMerge && (
        <div className="toolbar" style={{ marginTop: 10 }}>
          {/* The message names the CLI flag, because the refusal is raised in a
              library that has no idea a browser is calling it. Offering the
              equivalent control here beats asking the operator to translate. */}
          <button className="small" onClick={onMerge}>
            Review it as a merge
          </button>
        </div>
      )}
      {collided && merge && !prefix.trim() && (
        <p style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
          Set a rename prefix above and preview again to see the full collision report. The
          server stops at the first collision it cannot resolve, so the rest of the report
          is only reachable once this one has a resolution — which means the widening risks
          further down the list stay hidden until then.
        </p>
      )}
    </div>
  );
}

function ImportReportView({ report }: { report: ImportReport }) {
  return (
    <div style={{ marginTop: 16 }}>
      <div className="toolbar" style={{ gap: 10 }}>
        <Badge tone={report.applied ? "green" : "gold"}>
          {report.applied ? "applied" : "preview — nothing written"}
        </Badge>
        {report.merge && <Badge tone="blue">merge</Badge>}
        <span className="mono faint" style={{ fontSize: 11 }}>
          {report.report_sha256.slice(0, 16)}
        </span>
      </div>

      {Object.keys(report.target_not_pristine).length > 0 && (
        <Section title="This workspace already holds">
          <CountPills counts={report.target_not_pristine} />
        </Section>
      )}

      <Section
        title={`Collisions (${report.collisions.length})`}
        hint="A group whose name already exists here will bind imported grants to THIS workspace's membership, not the source's."
      >
        <CollisionTable collisions={report.collisions} />
      </Section>

      <Section
        title="Rows"
        hint="Quarantined rows travelled in the archive and were deliberately not written: a membership or a clearance is a binding, and a binding is the only thing that can widen access."
      >
        <div style={{ fontSize: 12.5, lineHeight: 1.9 }}>
          <div>
            written: <CountPills counts={report.rows_imported} />
          </div>
          <div>
            quarantined: <CountPills counts={report.rows_quarantined} />
          </div>
          <div className="dim">
            {fmtNum(report.parts_written)} parts · {fmtBytes(report.bytes_written)} ·{" "}
            {report.files_written.length} files
          </div>
        </div>
      </Section>

      {Object.keys(report.dataset_renames).length > 0 && (
        <Section title="Datasets renamed">
          <div className="mono dim" style={{ fontSize: 12 }}>
            {Object.entries(report.dataset_renames)
              .map(([a, b]) => `${a} → ${b}`)
              .join(" · ")}
          </div>
        </Section>
      )}
      {Object.keys(report.marking_renames).length > 0 && (
        <Section
          title="Markings namespaced"
          hint="A marking of the same name with a different description is a different classification wearing the same word."
        >
          <div className="mono dim" style={{ fontSize: 12 }}>
            {Object.entries(report.marking_renames)
              .map(([a, b]) => `${a} → ${b}`)
              .join(" · ")}
          </div>
        </Section>
      )}

      {report.quarantined_clearances.length > 0 && (
        <Section
          title={`Clearances to re-grant (${report.quarantined_clearances.length})`}
          hint="A clearance is the one row type whose only possible effect is to widen, so it travels and is never written. Grant the marking named here, not the one the archive's clearances.jsonl names: a namespaced marking makes that list wrong at exactly the moment it matters."
        >
          <table className="grid">
            <thead>
              <tr>
                <th>Principal</th>
                <th>Grant this marking</th>
                <th>Named at the source</th>
              </tr>
            </thead>
            <tbody>
              {report.quarantined_clearances.map((c, i) => (
                <tr key={i}>
                  <td className="mono">{c.username}</td>
                  <td className="mono">{c.marking}</td>
                  <td className="mono dim">
                    {c.marking_at_source === c.marking ? "—" : c.marking_at_source}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}

      {report.warnings.length > 0 && (
        <Section title="Warnings">
          <ul style={{ fontSize: 12, paddingLeft: 18 }}>
            {report.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </Section>
      )}

      {report.manifest && <ManifestReport manifest={report.manifest} />}
    </div>
  );
}

// --------------------------------------------------------------- resupply panel

/** Where an operator goes to type a withheld secret back in. */
function resupplyLink(w: Withheld): { href: string; label: string } | null {
  switch (w.table) {
    case "sources":
      return { href: "#/datasets", label: "Datasets → Connectors" };
    case "engines":
      return { href: "#/admin", label: "Admin → Delegated engines" };
    case "users":
      return { href: "#/admin", label: "Admin → Users" };
    case "api_tokens":
      return { href: "#/admin", label: "Admin → API tokens" };
    case "datasets":
      return {
        href: `#/datasets/${encodeURIComponent(w.row)}`,
        label: `Datasets → ${w.row}`,
      };
    default:
      return null;
  }
}

function ResupplyPanel({ state }: { state: ImportState | undefined }) {
  const qc = useQueryClient();
  const reportQuery = useQuery({
    queryKey: ["import-report"],
    queryFn: () => api.get<ImportReport>(`${API}/workspace/import/report`),
    // A workspace that was never imported into has no report; a 404 here is the
    // normal case, not a failure worth a red box.
    retry: false,
  });
  const users = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get<User[]>(`${API}/users`),
  });
  const groups = useQuery({
    queryKey: ["groups"],
    queryFn: () => api.get<Group[]>(`${API}/groups`),
  });

  const ack = useMutation({
    mutationFn: () =>
      api.post<{ pipelines_acknowledged: boolean }>(
        `${API}/workspace/import/acknowledge-pipelines`,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["import-state"] });
    },
  });

  const report = reportQuery.data;
  const known = useMemo(() => {
    const u = new Set((users.data ?? []).map((x) => x.username.toLowerCase()));
    const g = new Map(
      (groups.data ?? []).map((x) => [x.name.toLowerCase(), x.members.length]),
    );
    return { u, g };
  }, [users.data, groups.data]);

  if (reportQuery.isLoading) return <Spinner />;
  if (!report) {
    return (
      <div className="card" style={{ marginBottom: 20 }}>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>Re-supply checklist</div>
        <EmptyState>
          Nothing imported into this workspace yet. After an import this lists every
          credential the archive withheld and every principal its rules name.
        </EmptyState>
      </div>
    );
  }

  return (
    <div className="card" style={{ marginBottom: 20 }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>Re-supply checklist</div>
      <p className="dim" style={{ fontSize: 12.5, marginTop: 0 }}>
        From the last import{report.manifest ? ` of ${report.manifest.origin.workspace_name}` : ""}
        {report.manifest ? ` (${fmtTime(report.manifest.created_at)})` : ""}.
      </p>

      {state?.imported && !state.pipelines_acknowledged && (
        <div className="error-box" style={{ marginTop: 12 }}>
          <div style={{ fontWeight: 600 }}>Imported pipelines are not running.</div>
          <p style={{ fontSize: 12.5, marginTop: 6 }}>
            Transform files are <code>exec</code>'d unsandboxed on every build, so an import
            that ran them would be a code-delivery channel wearing a data-movement costume.
            Read <code>pipelines/</code> before you clear this. Builds refuse until you do.
          </p>
          {state.content_warnings.length > 0 && (
            <ul style={{ fontSize: 12, paddingLeft: 18 }}>
              {state.content_warnings.map((w, i) => (
                <li key={i} className="mono">
                  {w.line ? `${w.file}:${w.line}` : w.file} ({w.pattern}) — {w.preview}
                </li>
              ))}
            </ul>
          )}
          <div className="toolbar" style={{ marginTop: 8 }}>
            <button
              className="small"
              disabled={ack.isPending}
              onClick={() => {
                if (
                  window.confirm(
                    "Confirm you have read every file in pipelines/. They are executed on every build.",
                  )
                ) {
                  ack.mutate();
                }
              }}
            >
              {ack.isPending ? "Acknowledging…" : "I have read the imported pipelines"}
            </button>
          </div>
          <InlineError err={ack.error} />
        </div>
      )}

      <Section
        title={`Credentials to re-enter (${report.withheld.length})`}
        hint="Each of these was omitted from the archive on purpose. Nothing here will work until it is typed back in."
      >
        {report.withheld.length === 0 ? (
          <EmptyState>Nothing was withheld.</EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Where</th>
                  <th>Field</th>
                  <th>Go to</th>
                </tr>
              </thead>
              <tbody>
                {report.withheld.map((w, i) => {
                  const link = resupplyLink(w);
                  return (
                    <tr key={i}>
                      <td className="mono">
                        {w.table}
                        {w.row !== "*" && <span className="dim"> / {w.row}</span>}
                      </td>
                      <td className="mono">{w.field}</td>
                      <td>
                        {link ? (
                          <a href={link.href}>{link.label}</a>
                        ) : (
                          <span className="mono dim" style={{ fontSize: 12 }}>
                            {w.resupply}
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section
        title={`Principals to rebind (${report.principals.length})`}
        hint="Import creates none of these. Until you do, every grant naming them is inert — which is the safe direction, and also why nothing works yet."
      >
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Principal</th>
                <th>State here</th>
                <th>Named by</th>
                <th>Go to</th>
              </tr>
            </thead>
            <tbody>
              {report.principals.map((p) => {
                const exists =
                  p.kind === "user" ? known.u.has(p.name.toLowerCase()) : known.g.has(p.name.toLowerCase());
                const members = p.kind === "group" ? known.g.get(p.name.toLowerCase()) : undefined;
                return (
                  <tr key={`${p.kind}:${p.name}`}>
                    <td className="mono">
                      {p.name} <span className="faint">({p.kind})</span>
                    </td>
                    <td>
                      {!exists ? (
                        <Badge tone="gold">absent — rules naming it are inert</Badge>
                      ) : p.kind === "group" && members === 0 ? (
                        <Badge tone="gold">empty — no members bound</Badge>
                      ) : (
                        <Badge tone="green">
                          {p.kind === "group" ? `${members} member(s)` : "exists"}
                        </Badge>
                      )}
                    </td>
                    <td className="mono dim" style={{ fontSize: 12 }}>
                      {p.referenced_by.join(", ")}
                    </td>
                    <td>
                      <a href="#/admin">{p.kind === "group" ? "Admin → Groups" : "Admin → Users"}</a>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Section>

      <Section
        title="Bindings the import refused to write"
        hint="They travelled in the archive and were reported instead of applied. Re-creating them is a separate, audited admin act."
      >
        <CountPills counts={report.rows_quarantined} />
      </Section>

      <Section
        title="Datasets whose data did not arrive"
        hint="These refuse reads with a 409 rather than returning zero rows — in a governance product an empty result is indistinguishable from a working row policy."
      >
        <DatasetStateTable
          plans={report.datasets.filter((d) => d.data_state !== "included")}
        />
      </Section>
    </div>
  );
}

// --------------------------------------------------------------- proof panel

function cellSummary(cell: GovernanceCell): string {
  const flags = `${cell.can_view ? "view" : "—"}/${cell.can_edit ? "edit" : "—"}`;
  const digest = cell.table ?? cell.arrow ?? "";
  return digest ? `${flags} ${digest.slice(0, 8)}` : flags;
}

function sameCell(a: GovernanceCell | undefined, b: GovernanceCell | undefined): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

function VerificationPanel() {
  const [selected, setSelected] = useState<string[]>([]);
  const [datasets, setDatasets] = useState<string[]>([]);
  const [result, setResult] = useState<FingerprintResponse | null>(null);

  const users = useQuery({
    queryKey: ["users"],
    queryFn: () => api.get<User[]>(`${API}/users`),
  });
  const datasetList = useQuery({
    queryKey: ["datasets"],
    queryFn: () => api.get<Dataset[]>(`${API}/datasets`),
  });
  const reportQuery = useQuery({
    queryKey: ["import-report"],
    queryFn: () => api.get<ImportReport>(`${API}/workspace/import/report`),
    retry: false,
  });

  // The source's own answer, if the archive carried one. Without it this panel
  // still computes the destination's matrix — but it can only display it, not
  // prove anything against it, and it says so.
  const baseline = useMemo<GovernanceFingerprint | null>(() => {
    const fp = reportQuery.data?.manifest?.governance_fingerprint;
    if (fp && "cells" in fp && Object.keys(fp.cells).length > 0) {
      return fp as GovernanceFingerprint;
    }
    return null;
  }, [reportQuery.data]);

  const compute = useMutation({
    mutationFn: () =>
      api.post<FingerprintResponse>(`${API}/workspace/governance/fingerprint`, {
        principals: selected,
        datasets: datasets.length > 0 ? datasets : null,
      }),
    onSuccess: setResult,
  });

  const toggle = (list: string[], set: (v: string[]) => void, value: string) =>
    set(list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);

  const principalOptions = [
    ...(users.data ?? []).map((u) => u.username),
    ANONYMOUS,
    // Principals the archive names but this workspace has not bound. Offering
    // them is the point: "unresolved" is the evidence that the import was inert.
    ...(reportQuery.data?.principals ?? [])
      .filter((p) => p.kind === "user")
      .map((p) => p.name)
      .filter((n) => !(users.data ?? []).some((u) => u.username === n)),
  ];

  const fp = result?.fingerprint;
  const rows = fp?.principals ?? [];
  const cols = fp?.datasets ?? [];

  return (
    <div className="card">
      <div style={{ fontWeight: 600, marginBottom: 4 }}>Governance verification</div>
      <p className="dim" style={{ fontSize: 12.5, marginTop: 0 }}>
        Recompute what this workspace actually <em>answers</em> for each (principal, dataset)
        pair — permissions, effective markings, the rendered row filter and column masks, and
        a digest of the rows returned through all three enforcement paths. This is the proof
        that the copy governs identically, reproducible by you rather than only by the test
        suite.
      </p>

      <Section title="Principals">
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
          {principalOptions.map((name) => (
            <label key={name} className="check-inline">
              <input
                type="checkbox"
                checked={selected.includes(name)}
                onChange={() => toggle(selected, setSelected, name)}
              />
              <span className="mono">{name}</span>
            </label>
          ))}
        </div>
        <div className="hint">
          Leave every box unticked to fingerprint every principal this workspace knows, plus
          the anonymous one.
        </div>
      </Section>

      <Section title="Datasets">
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
          {(datasetList.data ?? []).map((d) => (
            <label key={d.name} className="check-inline">
              <input
                type="checkbox"
                checked={datasets.includes(d.name)}
                onChange={() => toggle(datasets, setDatasets, d.name)}
              />
              <span className="mono">{d.name}</span>
            </label>
          ))}
        </div>
      </Section>

      <div className="toolbar" style={{ marginTop: 12 }}>
        <button className="primary" disabled={compute.isPending} onClick={() => compute.mutate()}>
          {compute.isPending ? "Computing…" : "Compute fingerprint"}
        </button>
        {baseline ? (
          <Badge tone="blue">comparing against the imported archive</Badge>
        ) : (
          <span className="dim" style={{ fontSize: 12 }}>
            No source fingerprint to compare against — re-export with “Embed the governance
            fingerprint” to get one.
          </span>
        )}
      </div>

      {compute.isError && <InlineError err={compute.error} />}

      {result && result.unresolved_principals.length > 0 && (
        <div className="error-box" style={{ marginTop: 12 }}>
          <div style={{ fontWeight: 600 }}>
            {result.unresolved_principals.length} principal(s) do not exist here:{" "}
            <span className="mono">{result.unresolved_principals.join(", ")}</span>
          </div>
          <p style={{ fontSize: 12, marginTop: 6, marginBottom: 0 }}>
            Expected immediately after an import — it is exactly what “binds no principal”
            means. Create them in Admin → Users, then compute again.
          </p>
        </div>
      )}

      {fp && rows.length > 0 && (
        <Section
          title="Decision matrix"
          hint={
            baseline
              ? "Green: this workspace answers exactly what the archive recorded. Red: it does not. Grey: the archive has no answer for this cell."
              : "view/edit and the first bytes of the result digest. Hover a cell for the rendered policy."
          }
        >
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Principal</th>
                  {cols.map((d) => (
                    <th key={d} className="mono">
                      {d}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((who) => (
                  <tr key={who}>
                    <td className="mono">{who}</td>
                    {cols.map((d) => {
                      const key = `${who}|${d}`;
                      const cell = fp.cells[key];
                      const base = baseline?.cells[key];
                      const tone = !baseline || !base
                        ? "neutral"
                        : sameCell(base, cell)
                          ? "green"
                          : "red";
                      return (
                        <td key={d}>
                          <span
                            title={
                              cell
                                ? `markings: ${cell.effective_markings.join(", ") || "none"}\n${cell.decision}\n${cell.sql}`
                                : undefined
                            }
                          >
                            <Badge tone={tone}>{cell ? cellSummary(cell) : "—"}</Badge>
                          </span>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      {fp && Object.keys(fp.object_types).length > 0 && (
        <Section title="Ontology object types">
          <div className="mono dim" style={{ fontSize: 12, lineHeight: 1.8 }}>
            {Object.entries(fp.object_types).map(([key, [view, edit]]) => (
              <div key={key}>
                {key}: {view ? "view" : "—"}/{edit ? "edit" : "—"}
              </div>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}

// --------------------------------------------------------------- the section

export function PortabilitySection() {
  const qc = useQueryClient();
  const state = useQuery({
    queryKey: ["import-state"],
    queryFn: () => api.get<ImportState>(`${API}/workspace/import/state`),
  });

  return (
    <section style={{ marginTop: 28 }} id="portability">
      <h2 style={{ fontSize: 15, marginBottom: 4 }}>Portability</h2>
      <p className="dim" style={{ fontSize: 12.5, marginTop: 0 }}>
        Take this workspace somewhere else, reconstruct it there, and prove the copy governs
        identically. Nothing here is a lock-in escape hatch bolted on afterwards — the
        archive is a plain tar of JSONL and Parquet that any machine can read.
      </p>

      {state.isError && <ErrorBox error={state.error} />}

      <ExportPanel />
      <ImportPanel
        onImported={() => {
          qc.invalidateQueries({ queryKey: ["import-state"] });
          qc.invalidateQueries({ queryKey: ["import-report"] });
          qc.invalidateQueries({ queryKey: ["datasets"] });
        }}
      />
      <ResupplyPanel state={state.data} />
      <VerificationPanel />
    </section>
  );
}
