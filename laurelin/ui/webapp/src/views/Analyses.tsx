// Analyses: ONE door from a question to a shared answer.
//
// The landing page offers the two ways in, in order of commitment:
//
//   * QUICK CHART (views/analyses/QuickChart.tsx) — point-and-click
//     data-to-chart, no name, no server record, a sessionStorage draft.
//     Formerly the standalone Explore screen; /explore redirects here. Save
//     to a dashboard, or "keep going" into cell 1 of a new analysis.
//   * NEW ANALYSIS — the multi-cell governed document (Code Workbook parity,
//     no code). An analyst adds cells top to bottom: each is EITHER a SQL
//     query OR a point-and-click shaping step (the same shared card stack the
//     quick chart uses), and a shaping cell's SOURCE picker offers datasets
//     *and* earlier shaping cells — that one picker is the entire chaining
//     UX. Each cell shows its result table and, optionally, a chart.
//
// There is deliberately no code cell. That is Foundry's actual "Code"
// workbook and the RCE surface --lock-pipelines exists to close; everything
// here compiles to governed SQL through paths already built and attacked.
//
// Sharing is the permission model, as with dashboards: a viewer opening the
// same URL receives the layout (titles, chart bindings) and gets each cell's
// ROWS from POST /analyses/{name}/cells/{id}/run, executed server-side as
// THEMSELVES — their ACL, row policy and masks, over the whole chain, in one
// compiled statement. The query text is never in their payload; "no query
// text is renderable" is the server's projection, not CSS.
//
// Display order is cosmetic; *execution* order is the DAG in each cell's
// inputs. Reordering cells never edits inputs.

import { useEffect, useMemo, useState } from "react";
import { Link, Route, Routes, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api, ApiError } from "../api";
import { useAuth } from "../auth";
import type {
  Analysis,
  AnalysisCell,
  CellPreviewResult,
  Dashboard,
  DashboardPanel,
  Dataset,
  FlowKind,
  FlowSchemaResult,
  QueryResult,
} from "../types";
import { cellIsWhole } from "../types";
import {
  Column,
  DataTable,
  EmptyState,
  ErrorBox,
  Modal,
  Note,
  PageHeader,
  Spinner,
  Withheld,
  fmtTime,
} from "../ui";
import {
  docDraftKey,
  parseDocDraft,
  serializeDocDraft,
  shapedResultColumns,
  type StoredDraftCell,
} from "./shaping/model";
import {
  BindingsRow,
  CHART_KINDS,
  CellResult,
  ChartKindBar,
  SHAPING_STYLES,
  ShapingCards,
  type Bindings,
} from "./shaping/ShapingCards";
import {
  cellFragment,
  emptyShaping,
  explainCellRefusal,
  shapingFromCell,
  shapingIssues,
  sourceCell,
  sourceDataset,
  type CellShaping,
  type RefusalContext,
} from "./analyses/model";
import { NAME_RULE, QuickChart } from "./analyses/QuickChart";

const NAME_RE = /^[a-z][a-z0-9_-]{0,63}$/;
const PREVIEW_ROWS = 200;

export function AnalysesView() {
  return (
    <Routes>
      <Route path="/" element={<AnalysesLanding />} />
      <Route path=":name" element={<AnalysisPage />} />
    </Routes>
  );
}

// ---------------------------------------------------------------- landing

function AnalysesLanding() {
  const auth = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [params] = useSearchParams();
  // The SQL page's "save as analysis cell" hand-off: ?mode=doc&sql=<encoded>
  // prefills the new-analysis form with a ready SQL cell, so a quick query
  // that turned out to matter gets a lightweight home without retyping.
  const prefillSql = params.get("mode") === "doc" ? params.get("sql") ?? "" : "";
  const [newName, setNewName] = useState("");
  const [newTitle, setNewTitle] = useState("");

  const listQ = useQuery({
    queryKey: ["analyses"],
    queryFn: () => api.get<Analysis[]>(`${API}/analyses`),
  });

  const create = useMutation({
    mutationFn: async () => {
      const name = newName.trim();
      const created = await api.put<Analysis>(`${API}/analyses/${name}`, {
        title: newTitle.trim() || name,
        cells: [],
      });
      if (prefillSql.trim()) {
        await api.post<Analysis>(`${API}/analyses/${encodeURIComponent(name)}/cells`, {
          title: "Query",
          sql: prefillSql,
          chart: "table",
          x: "",
          y: [],
          series: "",
          stacked: false,
          width: 12,
        });
      }
      return created;
    },
    onSuccess: (a) => {
      qc.invalidateQueries({ queryKey: ["analyses"] });
      navigate(`/analyses/${a.name}`);
    },
  });

  const columns: Column<Analysis>[] = [
    {
      label: "Analysis",
      render: (a) => <Link to={`/analyses/${a.name}`}>{a.title || a.name}</Link>,
    },
    { label: "Name", className: "mono dim", render: (a) => a.name },
    { label: "Cells", className: "num", render: (a) => String(a.cells.length) },
    { label: "Updated", render: (a) => <span className="dim">{fmtTime(a.updated_at)}</span> },
  ];

  const nameOk = NAME_RE.test(newName.trim());

  return (
    <div>
      <PageHeader
        title="Analyses"
        subtitle="From a question to a shared answer: chart something quickly, or build a multi-step analysis — every result respects the reader's data access."
      />
      {/* The quick chart is the default, zero-commitment entry; the document
          is the committed one. Both live behind this one door so "clean this
          dataset and chart the result" is never a coin flip between two
          near-identical screens. Editor-only — the choice between authoring
          entries only exists for someone who can author. */}
      {auth.can("editor") && (
        <div style={{ marginBottom: 22 }}>
          <QuickChart />
        </div>
      )}
      {auth.can("editor") && (
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-title">New analysis</div>
          <p className="hint" style={{ marginTop: 2, marginBottom: 8 }}>
            A multi-step document: cells that build on each other — shaped by
            clicking or written in SQL — shared as one URL.
          </p>
          {prefillSql.trim() && (
            <Note>
              Your SQL query from the SQL page will be added as the first cell.
            </Note>
          )}
          <div className="toolbar" style={{ marginBottom: 0, gap: 12, flexWrap: "wrap" }}>
            <div className="field">
              <label htmlFor="an-new-name">Name</label>
              <input
                id="an-new-name"
                className="mono"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="churn-investigation"
                autoComplete="off"
              />
              {/* The create button used to disable silently on a name the
                  gate refused, with nothing saying why. */}
              <div className="hint">{NAME_RULE}</div>
            </div>
            <div className="field" style={{ flex: "1 1 220px" }}>
              <label htmlFor="an-new-title">Title</label>
              <input
                id="an-new-title"
                value={newTitle}
                onChange={(e) => setNewTitle(e.target.value)}
                placeholder="Churn investigation"
                autoComplete="off"
              />
            </div>
            <button
              className="primary"
              disabled={!nameOk || create.isPending}
              title={!nameOk && newName.trim() !== "" ? NAME_RULE : undefined}
              onClick={() => create.mutate()}
            >
              {create.isPending ? "Creating…" : "New analysis"}
            </button>
          </div>
          {create.isError && <ErrorBox error={create.error} />}
        </div>
      )}
      {listQ.isLoading ? (
        <Spinner />
      ) : listQ.isError ? (
        <ErrorBox error={listQ.error} />
      ) : listQ.data!.length === 0 ? (
        <EmptyState>
          No analyses yet{auth.can("editor") ? " — create one above." : "."}
        </EmptyState>
      ) : (
        <DataTable
          columns={columns}
          rows={listQ.data!}
          rowKey={(a) => a.name}
          onRowClick={(a) => navigate(`/analyses/${a.name}`)}
        />
      )}
    </div>
  );
}

// ------------------------------------------------------------- viewer cell

/** A viewer's cell: title + rows/chart from the run route. The payload this
 *  screen holds contains no query text at all — there is nothing to hide,
 *  because nothing arrived. */
function ViewerCell({ analysis, cell }: { analysis: string; cell: AnalysisCell }) {
  const q = useQuery({
    // Keyed on identity, not contents — a viewer has no contents to key on.
    queryKey: ["cell-run", analysis, cell.id],
    queryFn: () =>
      api.post<QueryResult>(
        `${API}/analyses/${encodeURIComponent(analysis)}/cells/${encodeURIComponent(cell.id)}/run`,
        { max_rows: 1000 },
      ),
    staleTime: 30_000,
  });

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 8 }}>
        <div style={{ fontWeight: 600, fontSize: 13.5 }}>{cell.title}</div>
        <button className="small" disabled={q.isFetching} onClick={() => q.refetch()}>
          {q.isFetching ? "Running…" : "Run again"}
        </button>
      </div>
      {q.isLoading ? (
        <Spinner label="Running…" />
      ) : q.isError ? (
        <ErrorBox error={q.error} onRetry={() => q.refetch()} />
      ) : (
        <CellResult
          result={q.data!}
          chart={cell.chart}
          x={cell.x}
          y={cell.y}
          series={cell.series ?? ""}
          stacked={cell.stacked ?? false}
        />
      )}
    </div>
  );
}

// -------------------------------------------------------------- draft model

const AUTO_BINDINGS: Bindings = { chart: "table", x: "", y: [], series: "", stacked: false };

interface DraftCell {
  /** Stable local key (never reused; survives id assignment on save). */
  key: string;
  /** Server id once saved; null for a brand-new cell. */
  id: string | null;
  title: string;
  kind: "sql" | "shaping";
  sql: string;
  /** Parsed shaping state; null when the saved fragment is not a shape these
   *  cards emit (e.g. hand-written IR with a join) — shown, not editable. */
  shaping: CellShaping | null;
  /** The saved record, kept whole for reorder PUTs and unparseable cells. */
  raw: AnalysisCell | null;
  bindings: Bindings;
  width: number;
  dirty: boolean;
}

let keyCounter = 0;
function nextKey(): string {
  return `k${++keyCounter}`;
}

function draftFromCell(c: AnalysisCell): DraftCell {
  const isSql = (c.sql ?? "") !== "";
  return {
    key: nextKey(),
    id: c.id,
    title: c.title,
    kind: isSql ? "sql" : "shaping",
    sql: c.sql ?? "",
    shaping: isSql ? null : shapingFromCell(c.flow, c.inputs, c.top),
    raw: c,
    bindings: {
      chart: c.chart,
      x: c.x,
      y: c.y,
      series: c.series ?? "",
      stacked: c.stacked ?? false,
    },
    width: c.width,
    dirty: false,
  };
}

/** The chart bindings this cell should DRAW and SAVE: explicit choices win,
 *  and the gaps fill from the SHAPING, not from value-type inference — x is
 *  the first group column, y the measures. Inference alone puts a numeric
 *  group column (a histogram's bin) on the y axis as a series, because all
 *  it can see is "this column holds numbers". */
function effectiveCellBindings(d: DraftCell): { x: string; y: string[] } {
  const b = d.bindings;
  if (
    d.kind !== "shaping" || !d.shaping || d.shaping.measures.length === 0 ||
    b.chart === "table" || b.chart === "stat"
  ) {
    return { x: b.x, y: b.y };
  }
  const cols = shapedResultColumns(d.shaping, []);
  const groups = cols.filter((c) => !d.shaping!.measures.some((m) => m.alias === c));
  return {
    x: b.x || (groups[0] ?? ""),
    y: b.y.length > 0 ? b.y : d.shaping.measures.map((m) => m.alias).filter(Boolean),
  };
}

/** The wire dict for one draft cell — presentation + whichever source it is. */
function cellPayload(d: DraftCell, kinds: Record<string, FlowKind>): Record<string, unknown> | null {
  const eff = effectiveCellBindings(d);
  const base = {
    title: d.title,
    chart: d.bindings.chart,
    x: eff.x,
    y: eff.y,
    series: d.bindings.series,
    stacked: d.bindings.stacked,
    width: d.width,
  };
  if (d.kind === "sql") {
    if (!d.sql.trim()) return null;
    return { ...base, sql: d.sql };
  }
  if (d.shaping) {
    const frag = cellFragment(d.shaping, kinds);
    if (!frag) return null;
    return { ...base, flow: frag.flow, inputs: frag.inputs, top: frag.top };
  }
  // Unparseable saved shaping cell: carry the stored instruction unchanged.
  if (d.raw && cellIsWhole(d.raw)) {
    return { ...base, flow: d.raw.flow, inputs: d.raw.inputs ?? [], top: d.raw.top ?? null };
  }
  return null;
}

// ---------------------------------------------------- doc draft persistence

function toStored(d: DraftCell): StoredDraftCell {
  return {
    id: d.id,
    title: d.title,
    kind: d.kind,
    sql: d.sql,
    shaping: d.shaping,
    bindings: d.bindings,
    width: d.width,
    dirty: d.dirty,
  };
}

/** The editor's cells, seeded from the server record and overlaid with the
 *  session's stored draft: edited saved cells resume dirty, unsaved cells
 *  come back whole. useState-only drafts meant one refresh destroyed every
 *  unsaved cell — the worst data-loss cliff on the documents side. */
function restoreDrafts(ana: Analysis): DraftCell[] {
  const base = ana.cells.map(draftFromCell);
  let stored: StoredDraftCell[] | null = null;
  try {
    stored = parseDocDraft(sessionStorage.getItem(docDraftKey(ana.name)));
  } catch {
    stored = null;
  }
  if (!stored) return base;
  const sanitizeBindings = (b: StoredDraftCell["bindings"]): Bindings => ({
    chart: CHART_KINDS.includes(b.chart as Bindings["chart"])
      ? (b.chart as Bindings["chart"])
      : "table",
    x: b.x,
    y: b.y,
    series: b.series,
    stacked: b.stacked,
  });
  const out = base.map((d) => {
    const s = stored!.find((c) => c.id !== null && c.id === d.id && c.dirty);
    if (!s || s.kind !== d.kind) return d;
    return {
      ...d,
      title: s.title,
      sql: s.sql,
      shaping: d.kind === "shaping" ? (s.shaping ?? d.shaping) : d.shaping,
      bindings: sanitizeBindings(s.bindings),
      width: s.width,
      dirty: true,
    };
  });
  for (const s of stored) {
    if (s.id !== null) continue;
    out.push({
      key: nextKey(),
      id: null,
      title: s.title,
      kind: s.kind,
      sql: s.sql,
      shaping: s.kind === "shaping" ? (s.shaping ?? emptyShaping()) : null,
      raw: null,
      bindings: sanitizeBindings(s.bindings),
      width: s.width,
      dirty: true,
    });
  }
  return out;
}

// -------------------------------------------------------------- editor cell

function EditorCell({
  analysis,
  draft,
  position,
  drafts,
  update,
  onSave,
  onDelete,
  onMove,
  reportMeta,
  cellMeta,
  saving,
}: {
  analysis: string;
  draft: DraftCell;
  position: number;
  drafts: DraftCell[];
  update: (fn: (d: DraftCell) => DraftCell) => void;
  onSave: () => void;
  onDelete: () => void;
  onMove: (dir: -1 | 1) => void;
  /** Report this cell's compiled result schema, for downstream pickers. */
  reportMeta: (cellId: string, meta: { schema: string[]; kinds: Record<string, FlowKind> }) => void;
  cellMeta: Record<string, { schema: string[]; kinds: Record<string, FlowKind> }>;
  saving: boolean;
}) {
  // ------------------------------------------------- source columns + kinds
  const ds = draft.shaping ? sourceDataset(draft.shaping.source) : null;
  const upCell = draft.shaping ? sourceCell(draft.shaping.source) : null;

  const schemaQ = useQuery({
    queryKey: ["flow-schema", ds],
    queryFn: () => api.get<FlowSchemaResult>(`${API}/flows/schema?dataset=${encodeURIComponent(ds!)}`),
    enabled: draft.kind === "shaping" && !!ds,
    staleTime: 30_000,
  });
  const upMeta = upCell ? cellMeta[upCell] : undefined;
  const sourceColumns = ds ? schemaQ.data?.columns ?? [] : upMeta?.schema ?? [];
  const sourceKinds: Record<string, FlowKind> = ds ? schemaQ.data?.kinds ?? {} : upMeta?.kinds ?? {};

  // --------------------------------------------------------------- preview
  // The draft closure this cell's preview runs: its ancestors (as their
  // CURRENT drafts, so the preview reflects the screen) plus itself. Only
  // the ancestors need to be serializable — unrelated half-finished cells
  // must not block this cell's preview.
  const qc = useQueryClient();
  const previewBody = useMemo(() => {
    if (draft.kind === "sql") return null;
    const byId = new Map(drafts.filter((d) => d.id).map((d) => [d.id!, d]));
    // Literal-typing kinds for a cell's synthesis: its dataset's schema (from
    // the query cache its own card already populated) or its upstream cell's
    // compiled kinds.
    const kindsFor = (d: DraftCell): Record<string, FlowKind> => {
      if (!d.shaping) return {};
      const dds = sourceDataset(d.shaping.source);
      if (dds) {
        return qc.getQueryData<FlowSchemaResult>(["flow-schema", dds])?.kinds ?? {};
      }
      const up = sourceCell(d.shaping.source);
      return (up && cellMeta[up]?.kinds) || {};
    };
    const chain: Record<string, unknown>[] = [];
    const visit = (id: string): boolean => {
      const d = byId.get(id);
      if (!d) return false;
      if (chain.some((c) => c.id === id)) return true;
      const up = d.shaping ? sourceCell(d.shaping.source) : null;
      if (up && !visit(up)) return false;
      // An unedited saved ancestor rides as stored; an edited one reflects
      // the screen, so what previews is what the analyst is looking at.
      const payload =
        !d.dirty && d.raw && cellIsWhole(d.raw)
          ? { flow: d.raw.flow, inputs: d.raw.inputs ?? [], top: d.raw.top ?? null }
          : cellPayload(d, kindsFor(d));
      if (!payload || (payload as any).sql !== undefined || !(payload as any).flow) return false;
      chain.push({ id: d.id, title: d.title || "cell", ...payload });
      return true;
    };
    if (upCell && !visit(upCell)) return null;
    const selfPayload = cellPayload(draft, sourceKinds);
    if (!selfPayload || (selfPayload as any).sql !== undefined) return null;
    const selfId = draft.id ?? "draft";
    return { cells: [...chain, { id: selfId, ...selfPayload }], cell_id: selfId, max_rows: PREVIEW_ROWS };
  }, [draft, drafts, cellMeta, sourceKinds, upCell, qc]);

  const previewKey = previewBody ? JSON.stringify(previewBody) : "";
  const [debouncedKey, setDebouncedKey] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebouncedKey(previewKey), 400);
    return () => clearTimeout(t);
  }, [previewKey]);

  const previewQ = useQuery({
    queryKey: ["cell-preview", analysis, draft.key, debouncedKey],
    queryFn: ({ signal }) =>
      api.post<CellPreviewResult>(`${API}/analyses/preview`, JSON.parse(debouncedKey), signal),
    enabled: draft.kind === "shaping" && !!debouncedKey,
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  });

  // Downstream cells build their pickers from this cell's compiled schema.
  useEffect(() => {
    if (draft.id && previewQ.data) {
      reportMeta(draft.id, { schema: previewQ.data.schema, kinds: previewQ.data.kinds });
    }
  }, [draft.id, previewQ.data, reportMeta]);

  // Columns masked for this caller, greyed in the measure pickers — the data
  // already arrives on every preview; the cells just never used it.
  const maskedColumns = useMemo(() => {
    const out = new Set<string>();
    for (const cols of Object.values(previewQ.data?.masked_columns ?? {}))
      for (const c of cols) out.add(c);
    return out;
  }, [previewQ.data?.masked_columns]);

  // ---------------------------------------------------------- SQL run (200)
  const sqlRun = useMutation({
    mutationFn: () => api.post<QueryResult>(`${API}/query`, { sql: draft.sql, max_rows: PREVIEW_ROWS }),
  });

  const result: QueryResult | undefined = draft.kind === "sql" ? sqlRun.data : previewQ.data;
  const resultKinds: Record<string, FlowKind> = useMemo(() => {
    if (draft.kind === "shaping") return previewQ.data?.kinds ?? {};
    const out: Record<string, FlowKind> = {};
    const sample = sqlRun.data?.rows[0];
    for (const c of sqlRun.data?.columns ?? []) {
      const v = sample?.[c];
      out[c] = typeof v === "number" ? "number" : typeof v === "boolean" ? "boolean" : "text";
    }
    return out;
  }, [draft.kind, previewQ.data, sqlRun.data]);

  const issues = draft.shaping ? shapingIssues(draft.shaping, sourceKinds) : [];

  const refusalCtx: RefusalContext = useMemo(() => {
    const position: Record<string, number> = {};
    const fragments: Record<string, { id: string; kind: string }[]> = {};
    drafts.forEach((d, i) => {
      if (!d.id) return;
      position[d.id] = i + 1;
      const frag = d.shaping ? cellFragment(d.shaping, {})?.flow : d.raw?.flow;
      if (frag?.nodes) {
        fragments[d.id] = (frag.nodes as any[]).map((n) => ({ id: n.id, kind: n.kind }));
      }
    });
    return { position, fragments };
  }, [drafts]);

  const canSave =
    draft.kind === "sql"
      ? draft.sql.trim() !== ""
      : draft.shaping
        ? issues.length === 0
        : draft.raw !== null; // unparseable but whole: presentation-only saves

  // What the source picker offers: datasets + earlier shaping cells. An
  // earlier UNSAVED shaping cell is listed too — disabled, saying what to do
  // — because "only saved cells chain" was otherwise discoverable only by
  // noticing an absence, and chaining is the product's whole reason to have
  // cells at all.
  const datasetsQ = useQuery({
    queryKey: ["datasets"],
    queryFn: () => api.get<Dataset[]>(`${API}/datasets`),
    enabled: draft.kind === "shaping",
  });
  const sources = useMemo(() => {
    const out: { value: string; label: string; hint?: string; disabled?: boolean }[] = [];
    for (let i = 0; i < drafts.length; i++) {
      const d = drafts[i];
      if (d === draft) break; // only EARLIER cells: execution reads upward
      if (d.kind !== "shaping") continue;
      if (!d.id) {
        out.push({
          value: `unsaved:${d.key}`,
          label: `Cell ${i + 1} — save it to read from it here`,
          hint: "Save that cell first; chaining reads saved cells.",
          disabled: true,
        });
        continue;
      }
      out.push({ value: `cell:${d.id}`, label: `Cell ${i + 1} — ${d.title || d.id}` });
    }
    for (const d of datasetsQ.data ?? []) {
      out.push({ value: `ds:${d.name}`, label: d.name });
    }
    return out;
  }, [drafts, draft, datasetsQ.data]);

  // ------------------------------------------------------ save to dashboard

  // A self-contained aggregated shaping cell can become a dashboard panel —
  // the same panel the quick chart saves, so the quick chart can reopen it.
  const eff = effectiveCellBindings(draft);
  const dashboardable =
    draft.kind === "shaping" && !!draft.shaping && !!ds &&
    draft.shaping.measures.length > 0 && issues.length === 0;
  const dashboardDisabledWhy =
    draft.kind !== "shaping" || !draft.shaping
      ? null // no button at all
      : upCell
        ? "This cell reads from another cell; dashboards hold self-contained panels."
        : draft.shaping.measures.length === 0
          ? "Add a summary first — a dashboard panel charts an aggregated result."
          : issues.length > 0
            ? issues[0]
            : null;
  const [dashOpen, setDashOpen] = useState(false);
  const auth = useAuth();
  const panelFlow = () => {
    const frag = cellFragment(draft.shaping!, sourceKinds);
    if (!frag) return null;
    return {
      flow: {
        name: "explore",
        output: "explore",
        author: auth.user?.username ?? "explore",
        description: "",
        terminal: frag.flow.terminal,
        nodes: frag.flow.nodes,
        expectations: [],
      },
      top: frag.top,
    };
  };

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
        <span className="badge">{position}</span>
        <input
          aria-label={`Cell ${position} title`}
          value={draft.title}
          placeholder={`Cell ${position}`}
          onChange={(e) => update((d) => ({ ...d, title: e.target.value, dirty: true }))}
          style={{ flex: "1 1 200px", fontWeight: 600 }}
        />
        <span className={`badge ${draft.kind === "sql" ? "badge-gold" : ""}`}
              title={draft.kind === "sql"
                ? "A SQL cell can't be referenced by later cells — only shaping cells chain."
                : "A shaping cell — later cells can read its output."}>
          {draft.kind === "sql" ? "SQL — can't be referenced by later cells" : "shaping"}
        </span>
        <span style={{ flexShrink: 0, display: "inline-flex", gap: 6 }}>
          {draft.kind === "shaping" && draft.shaping && (
            <button
              className="small"
              disabled={!dashboardable}
              title={dashboardDisabledWhy ?? "Save this cell's chart as a dashboard panel"}
              onClick={() => setDashOpen(true)}
            >
              Save to dashboard
            </button>
          )}
          <button className="small" aria-label="Move up" title="Move up (display order only — execution follows the cell links)" onClick={() => onMove(-1)} disabled={position === 1}>↑</button>
          <button className="small" aria-label="Move down" title="Move down (display order only)" onClick={() => onMove(1)}>↓</button>
          <button className="small primary" disabled={!canSave || saving} onClick={onSave}>
            {saving ? "Saving…" : draft.id ? (draft.dirty ? "Save" : "Saved") : "Save cell"}
          </button>
          <button className="small danger" aria-label="Delete this cell" onClick={onDelete}>✕</button>
        </span>
      </div>

      {draft.kind === "sql" ? (
        <div>
          <textarea
            className="mono"
            rows={4}
            value={draft.sql}
            onChange={(e) => update((d) => ({ ...d, sql: e.target.value, dirty: true }))}
            placeholder="SELECT region, sum(amount) AS total FROM sales GROUP BY region"
            style={{ width: "100%", resize: "vertical" }}
          />
          <div className="toolbar" style={{ marginTop: 6 }}>
            <button
              className="small"
              disabled={!draft.sql.trim() || sqlRun.isPending}
              onClick={() => sqlRun.mutate()}
            >
              {sqlRun.isPending ? "Running…" : "Run"}
            </button>
          </div>
          {sqlRun.isError && <ErrorBox error={sqlRun.error} />}
        </div>
      ) : draft.shaping ? (
        <div>
          <div className="toolbar" style={{ gap: 10, flexWrap: "wrap" }}>
            <div className="field">
              <label htmlFor={`an-src-${draft.key}`}>Reads from</label>
              <select
                id={`an-src-${draft.key}`}
                value={draft.shaping.source}
                onChange={(e) => {
                  const source = e.target.value;
                  // Columns belong to a source; shaping does not survive a swap.
                  update((d) => ({ ...d, shaping: emptyShaping(source), dirty: true }));
                }}
              >
                <option value="">Pick a source…</option>
                {sources.map((s) => (
                  <option key={s.value} value={s.value} title={s.hint} disabled={s.disabled}>
                    {s.label}
                  </option>
                ))}
              </select>
            </div>
          </div>
          {draft.shaping.source && (
            <ShapingCards
              state={draft.shaping}
              set={(fn) =>
                update((d) => ({ ...d, shaping: { ...d.shaping!, ...fn(d.shaping!) }, dirty: true }))
              }
              columns={sourceColumns}
              kinds={sourceKinds}
              masked={maskedColumns}
              measuresRequired={false}
              suggestDataset={ds}
              idPrefix={`an-${draft.key}`}
            />
          )}
          {upCell && !upMeta && (
            <div className="hint">Waiting for Cell above to preview — its columns feed these pickers.</div>
          )}
          {issues.length > 0 && draft.shaping.source && (
            <Note style={{ marginTop: 8 }}>{issues[0]}</Note>
          )}
          {previewQ.isError && (
            <Note tone="bad" style={{ marginTop: 8 }}>
              {previewQ.error instanceof ApiError
                ? explainCellRefusal(previewQ.error.detail, refusalCtx)
                : String(previewQ.error)}
            </Note>
          )}
        </div>
      ) : (
        <Note style={{ marginTop: 8 }}>
          This cell's shaping has a form these cards can't edit (it was likely
          written through the API). Its title, chart and layout can still be
          changed; the shaping itself is preserved as saved.
        </Note>
      )}

      {result && (
        <div style={{ marginTop: 10 }}>
          <ChartKindBar
            value={draft.bindings.chart}
            onChange={(k) => update((d) => ({ ...d, bindings: { ...d.bindings, chart: k }, dirty: true }))}
          />
          {draft.bindings.chart !== "table" && draft.bindings.chart !== "stat" && (
            <BindingsRow
              bindings={draft.bindings}
              setBindings={(fn) => update((d) => ({ ...d, bindings: fn(d.bindings), dirty: true }))}
              columns={result.columns}
              numericCols={result.columns.filter((c) => resultKinds[c] === "number")}
              categoricalCols={result.columns.filter(
                (c) => resultKinds[c] === "text" || resultKinds[c] === "boolean",
              )}
              idPrefix={`an-${draft.key}`}
            />
          )}
          {result.rows.length === 0 && draft.kind === "shaping" &&
            (draft.shaping?.filters.length ?? 0) > 0 && (
              // An empty result behind an active filter is ambiguous: bad
              // filter value, or genuinely empty data? Say which check to make.
              <Note>
                No rows matched your filters. Values must match the data exactly,
                including capital letters — pick from the suggestions in the value
                box to be sure.
              </Note>
            )}
          <div style={{ marginTop: 8 }}>
            <CellResult
              result={result}
              chart={draft.bindings.chart}
              x={eff.x}
              y={eff.y}
              series={draft.bindings.series}
              stacked={draft.bindings.stacked}
            />
          </div>
          <div className="faint" style={{ fontSize: 11.5, marginTop: 6 }}>
            preview · computed with your data access
            {draft.kind === "sql" && " · viewers will see up to 1000 rows"}
          </div>
        </div>
      )}
      {draft.kind === "shaping" && previewQ.isFetching && <Spinner label="Running…" />}

      {dashOpen && (
        <SaveCellPanelModal
          cellTitle={draft.title || `Cell ${position}`}
          bindings={draft.bindings}
          effX={eff.x}
          effY={eff.y}
          panelFlow={panelFlow}
          onClose={() => setDashOpen(false)}
        />
      )}
    </div>
  );
}

// ------------------------------------------------- cell → dashboard panel

/** The quick chart's save dialog, for a cell: same panel routes, same
 *  inline dashboard creation, same success link. Only self-contained
 *  aggregated cells get here (the button gates), so the saved panel is
 *  exactly one the quick chart can reopen for editing. */
function SaveCellPanelModal({
  cellTitle,
  bindings,
  effX,
  effY,
  panelFlow,
  onClose,
}: {
  cellTitle: string;
  bindings: Bindings;
  effX: string;
  effY: string[];
  panelFlow: () => { flow: unknown; top: number | null } | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [dashName, setDashName] = useState("");
  const [panelTitle, setPanelTitle] = useState(cellTitle);
  const [savedTo, setSavedTo] = useState<string | null>(null);
  const dashboardsQ = useQuery({
    queryKey: ["dashboards"],
    queryFn: () => api.get<Dashboard[]>(`${API}/dashboards`),
  });
  const save = useMutation({
    mutationFn: async () => {
      const pf = panelFlow();
      if (!pf) throw new Error("This cell isn't finished yet.");
      const name = dashName.trim();
      const panel: Partial<DashboardPanel> = {
        id: Math.random().toString(36).slice(2, 10),
        title: panelTitle.trim(),
        chart: bindings.chart,
        x: effX,
        y: effY,
        series: bindings.series,
        stacked: bindings.stacked,
        width: 6,
        flow: pf.flow as any,
        top: pf.top,
      };
      const base = `${API}/dashboards/${encodeURIComponent(name)}`;
      const exists = (dashboardsQ.data ?? []).some((d) => d.name === name);
      if (!exists) {
        await api.put<Dashboard>(base, { title: name, description: "", panels: [] });
      }
      return api.post<Dashboard>(`${base}/panels`, panel);
    },
    onSuccess: (d) => {
      setSavedTo(d.name);
      qc.invalidateQueries({ queryKey: ["dashboard", d.name] });
      qc.invalidateQueries({ queryKey: ["dashboards"] });
      qc.invalidateQueries({ queryKey: ["panel-run", d.name] });
    },
  });

  return (
    <Modal label="Save cell to dashboard" onClose={() => !save.isPending && onClose()}>
      <div className="card-title">Save to dashboard</div>
      <p className="dim" style={{ fontSize: 12.5, marginTop: 4 }}>
        The panel is a copy of this cell's shaping: viewers of the dashboard
        get the chart, computed with <em>their</em> data access. Later edits to
        the cell do not change the panel.
      </p>
      {savedTo ? (
        <>
          <Note tone="ok">
            Panel saved — <Link to={`/dashboards/${savedTo}`}>open dashboard “{savedTo}”</Link>.
          </Note>
          <div className="toolbar" style={{ marginTop: 12, justifyContent: "flex-end" }}>
            <button onClick={onClose}>Close</button>
          </div>
        </>
      ) : (
        <>
          <div className="field" style={{ marginTop: 12 }}>
            <label htmlFor="an-cell-dash">Dashboard</label>
            <input
              id="an-cell-dash"
              className="mono"
              autoFocus
              list="an-cell-dash-list"
              placeholder="revenue"
              value={dashName}
              onChange={(e) => setDashName(e.target.value)}
            />
            <datalist id="an-cell-dash-list">
              {(dashboardsQ.data ?? []).map((d) => (
                <option key={d.name} value={d.name}>{d.title || d.name}</option>
              ))}
            </datalist>
            <div className="hint">{NAME_RULE} Type a new name to create a dashboard.</div>
          </div>
          <div className="field">
            <label htmlFor="an-cell-panel-title">Panel title</label>
            <input
              id="an-cell-panel-title"
              value={panelTitle}
              onChange={(e) => setPanelTitle(e.target.value)}
            />
          </div>
          {save.error != null && <ErrorBox error={save.error} />}
          <div className="toolbar" style={{ marginTop: 16, justifyContent: "flex-end" }}>
            <button disabled={save.isPending} onClick={onClose}>Cancel</button>
            <button
              className="primary"
              disabled={save.isPending || !NAME_RE.test(dashName.trim())}
              onClick={() => save.mutate()}
            >
              {save.isPending ? "Saving…" : "Save panel"}
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}

// ------------------------------------------------------------------ detail

function AnalysisPage() {
  const { name = "" } = useParams();
  const auth = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();

  const anaQ = useQuery({
    queryKey: ["analysis", name],
    queryFn: () => api.get<Analysis>(`${API}/analyses/${name}`),
  });

  if (anaQ.isLoading) return <Spinner />;
  if (anaQ.isError) return <ErrorBox error={anaQ.error} />;
  const ana = anaQ.data!;

  if (!auth.can("editor")) {
    return (
      <div>
        <div style={{ marginBottom: 12 }}>
          <Link to="/analyses">← Analyses</Link>
        </div>
        <PageHeader title={ana.title || ana.name} subtitle={ana.description || undefined} />
        {ana.cells.length === 0 ? (
          <EmptyState>This analysis has no cells yet.</EmptyState>
        ) : (
          ana.cells.map((c) => <ViewerCell key={c.id} analysis={ana.name} cell={c} />)
        )}
        <p className="hint">
          Every result above was computed with your data access — the analysis's
          instructions run on the server, as you.
        </p>
      </div>
    );
  }

  return (
    <AnalysisEditor
      key={ana.name}
      ana={ana}
      onDeleted={() => {
        qc.invalidateQueries({ queryKey: ["analyses"] });
        navigate("/analyses");
      }}
    />
  );
}

function AnalysisEditor({ ana, onDeleted }: { ana: Analysis; onDeleted: () => void }) {
  const qc = useQueryClient();
  const [drafts, setDrafts] = useState<DraftCell[]>(() => restoreDrafts(ana));
  const [cellMeta, setCellMeta] = useState<Record<string, { schema: string[]; kinds: Record<string, FlowKind> }>>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  // Unsaved work survives a refresh: dirty and unsaved cells persist per
  // analysis, in sessionStorage; a clean editor clears its key so a stale
  // draft can never shadow the server's record later.
  useEffect(() => {
    try {
      const unsaved = drafts.filter((d) => d.dirty || !d.id);
      if (unsaved.length === 0) {
        sessionStorage.removeItem(docDraftKey(ana.name));
      } else {
        sessionStorage.setItem(docDraftKey(ana.name), serializeDocDraft(unsaved.map(toStored)));
      }
    } catch {
      // Storage full or denied: losing the draft beats losing the screen.
    }
  }, [drafts, ana.name]);

  const reportMeta = useMemo(
    () => (cellId: string, meta: { schema: string[]; kinds: Record<string, FlowKind> }) =>
      setCellMeta((m) => {
        const prev = m[cellId];
        if (prev && JSON.stringify(prev) === JSON.stringify(meta)) return m;
        return { ...m, [cellId]: meta };
      }),
    [],
  );

  const afterWrite = (a: Analysis) => {
    qc.setQueryData(["analysis", ana.name], a);
    qc.invalidateQueries({ queryKey: ["analyses"] });
    // The instructions changed; every viewer-style cached run is stale.
    qc.invalidateQueries({ queryKey: ["cell-run", ana.name] });
  };

  // Reconcile drafts with the server's record after a save: adopt ids and
  // server-filled titles for the saved cell, keep other drafts' edits.
  const reconcile = (a: Analysis, savedKey: string, savedId: string | null) => {
    setDrafts((ds) =>
      ds.map((d) => {
        if (d.key !== savedKey) return d;
        const server = savedId
          ? a.cells.find((c) => c.id === savedId)
          : a.cells[a.cells.length - 1];
        if (!server) return d;
        return { ...d, id: server.id, title: server.title, raw: server, dirty: false };
      }),
    );
  };

  const saveCell = async (d: DraftCell) => {
    setError(null);
    setSavingKey(d.key);
    try {
      // Literal kinds for synthesis: dataset schema or upstream cell schema.
      const kinds: Record<string, FlowKind> = await (async () => {
        if (!d.shaping) return {};
        const ds = sourceDataset(d.shaping.source);
        if (ds) {
          const s = await api.get<FlowSchemaResult>(`${API}/flows/schema?dataset=${encodeURIComponent(ds)}`);
          return s.kinds;
        }
        const up = sourceCell(d.shaping.source);
        return (up && cellMeta[up]?.kinds) || {};
      })();
      const payload = cellPayload(d, kinds);
      if (!payload) throw new Error("This cell isn't finished yet.");
      const base = `${API}/analyses/${encodeURIComponent(ana.name)}/cells`;
      const result = d.id
        ? await api.put<Analysis>(`${base}/${encodeURIComponent(d.id)}`, payload)
        : await api.post<Analysis>(base, payload);
      afterWrite(result);
      reconcile(result, d.key, d.id);
    } catch (e) {
      setError(translated(e));
    } finally {
      setSavingKey(null);
    }
  };

  const deleteCell = async (d: DraftCell) => {
    setError(null);
    if (!d.id) {
      setDrafts((ds) => ds.filter((x) => x.key !== d.key));
      return;
    }
    try {
      const result = await api.del<Analysis>(
        `${API}/analyses/${encodeURIComponent(ana.name)}/cells/${encodeURIComponent(d.id)}`,
      );
      afterWrite(result);
      setDrafts((ds) => ds.filter((x) => x.key !== d.key));
    } catch (e) {
      setError(translated(e));
    }
  };

  // Reorder: display order only — a whole-record PUT carrying every saved
  // cell WHOLE (the editor holds the full records), never a projection.
  const moveCell = async (d: DraftCell, dir: -1 | 1) => {
    setError(null);
    const i = drafts.findIndex((x) => x.key === d.key);
    const j = i + dir;
    if (j < 0 || j >= drafts.length) return;
    const next = [...drafts];
    [next[i], next[j]] = [next[j], next[i]];
    setDrafts(next);
    if (next.every((x) => x.id && x.raw)) {
      try {
        const result = await api.put<Analysis>(`${API}/analyses/${encodeURIComponent(ana.name)}`, {
          title: ana.title,
          description: ana.description,
          cells: next.map((x) => x.raw),
        });
        afterWrite(result);
      } catch (e) {
        setError(translated(e));
        setDrafts(drafts); // roll the visual order back
      }
    }
  };

  const del = useMutation({
    mutationFn: () => api.del(`${API}/analyses/${ana.name}`),
    onSuccess: () => {
      try {
        sessionStorage.removeItem(docDraftKey(ana.name));
      } catch {
        // A stale draft for a deleted analysis is harmless; it just lingers.
      }
      onDeleted();
    },
  });

  // The server now rewrites compiler refusals into cell + card vocabulary
  // itself ("Cell 2's Summarise card refers to…"), so this client-side
  // rewrite is normally a no-op. It stays as the backstop for any message
  // shape the server-side map has not met — the same net the preview hangs
  // under its errors.
  const translated = (e: unknown): unknown => {
    if (!(e instanceof ApiError)) return e;
    const position: Record<string, number> = {};
    const fragments: RefusalContext["fragments"] = {};
    drafts.forEach((d, i) => {
      if (!d.id) return;
      position[d.id] = i + 1;
      const frag = d.raw?.flow ?? (d.shaping ? cellFragment(d.shaping, {})?.flow : undefined);
      if (frag?.nodes) {
        fragments[d.id] = (frag.nodes as any[]).map((n) => ({ id: n.id, kind: n.kind }));
      }
    });
    const detail = explainCellRefusal(e.detail, { position, fragments });
    return detail === e.detail ? e : new ApiError(e.status, detail);
  };

  const addCell = (kind: "sql" | "shaping") => {
    setDrafts((ds) => [
      ...ds,
      {
        key: nextKey(),
        id: null,
        title: "",
        kind,
        sql: "",
        shaping: kind === "shaping" ? emptyShaping() : null,
        raw: null,
        bindings: { ...AUTO_BINDINGS },
        width: 12,
        dirty: true,
      },
    ]);
  };

  return (
    <div>
      <div style={{ marginBottom: 12 }}>
        <Link to="/analyses">← Analyses</Link>
      </div>
      <PageHeader
        title={ana.title || ana.name}
        subtitle={ana.description || "Viewers of this URL see results and charts, computed with their own data access — never the queries."}
        actions={
          <span style={{ display: "inline-flex", gap: 8 }}>
            <button className="small" onClick={() => addCell("shaping")}>+ Shaping cell</button>
            <button className="small" onClick={() => addCell("sql")}>+ SQL cell</button>
            <button
              className="small danger"
              onClick={() => {
                if (window.confirm(`Delete analysis "${ana.name}"?`)) del.mutate();
              }}
            >
              Delete
            </button>
          </span>
        }
      />
      {error != null &&
        (error instanceof ApiError && error.status === 400 ? (
          // A 400 here is a first-party refusal already written for the
          // analyst ("Cell 2's Summarise card refers to…") — the HTTP status
          // is not part of the sentence, so no "Error 400:" prefix.
          <Note tone="bad" style={{ marginTop: 8 }}>{error.detail}</Note>
        ) : (
          <ErrorBox error={error} />
        ))}
      {del.isError && <ErrorBox error={del.error} />}

      {drafts.length === 0 ? (
        <EmptyState>
          No cells yet — add a shaping cell (point-and-click, chainable) or a
          SQL cell.
        </EmptyState>
      ) : (
        drafts.map((d, i) => (
          <EditorCell
            key={d.key}
            analysis={ana.name}
            draft={d}
            position={i + 1}
            drafts={drafts}
            update={(fn) => setDrafts((ds) => ds.map((x) => (x.key === d.key ? fn(x) : x)))}
            onSave={() => void saveCell(d)}
            onDelete={() => void deleteCell(d)}
            onMove={(dir) => void moveCell(d, dir)}
            reportMeta={reportMeta}
            cellMeta={cellMeta}
            saving={savingKey === d.key}
          />
        ))
      )}

      {/* Can happen to an editor only in an odd state (a demoted session, a
          stale tab): a cell that arrived WITHOUT its operational half must
          not be edited — a save would write the hole back as if it were
          data. */}
      {drafts.some((d) => d.raw !== null && !cellIsWhole(d.raw)) && (
        <Withheld
          what="Some cells' instructions"
          role="editor"
          label="not editable"
          why="Reload the page; if it persists, your session's role changed."
        />
      )}

      <style>{SHAPING_STYLES}</style>
    </div>
  );
}
