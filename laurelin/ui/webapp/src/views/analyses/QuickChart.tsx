// Quick chart: Analyses' zero-commitment entry — point-and-click
// data-to-chart, formerly the standalone Explore screen. Pick a dataset or an
// object type, shape it by clicking (filter, group, summarise, sort, top-N),
// watch the chart update live, save it to a dashboard — no name, no server
// record, a sessionStorage draft and nothing else. "Keep going" promotes the
// shaping in place to cell 1 of a new analysis when one chart turns out to be
// step one of an investigation.
//
// Dataset source: the state synthesizes a FlowDef (views/shaping/model.ts)
// and previews through POST /explore/preview — the Flow compiler's stack,
// running as YOU: your ACL, your row policy, your column masks. What you see
// is what you may read, and nothing else.
//
// Object source: the same gestures compile to the ontology aggregate API
// (POST /ontology/objects/{type}/aggregate) — no SQL synthesis at all, and
// the result reflects edits made by actions (the edit overlay), which raw
// dataset SQL cannot see. An Objects draft cannot promote to an analysis —
// cells read datasets — and the button says so instead of greying silently.
//
// Saving writes a dashboard panel through the per-panel routes. A flow
// panel's `flow`/`top` are OPERATIONAL like `sql`: a viewer gets the chart
// from /run — the query never leaves the server.

import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api, ApiError } from "../../api";
import { useAuth } from "../../auth";
import type {
  AggregateResult,
  Analysis,
  Dashboard,
  DashboardPanel,
  Dataset,
  FlowSchemaResult,
  FlowKind,
  ObjectTypeDef,
  QueryResult,
} from "../../types";
import { Chart } from "../../charts";
import { EmptyState, ErrorBox, Modal, NAME_RULE, Note, Spinner, truncationNote } from "../../ui";
import {
  SCRATCH_KEY,
  LEGACY_SCRATCH_KEY,
  emptyExplore,
  exploreFlow,
  exploreIssues,
  explainRefusal,
  loadScratchDraft,
  resultColumns,
  serializeDraft,
  stateFromFlow,
  type ExploreState,
} from "../shaping/model";
import {
  BindingsRow,
  CHART_KINDS,
  ChartKindBar,
  ResultTable,
  SHAPING_STYLES,
  ShapingCards,
  type Bindings,
} from "../shaping/ShapingCards";
import { cellFragment, type CellShaping } from "./model";

const PREVIEW_ROWS = 200;
const NAME_RE = /^[a-z][a-z0-9_-]{0,63}$/;
// One sentence for the name gate, product-wide (ui.tsx). Re-exported because
// Analyses.tsx reads it from here.
export { NAME_RULE };

interface ExplorePreviewResult extends QueryResult {
  schema: string[];
  kinds: Record<string, FlowKind>;
  masked_columns: Record<string, string[]>;
  max_rows: number;
}

const AUTO_BINDINGS: Bindings = { chart: "bar", x: "", y: [], series: "", stacked: false };

// Object metrics vocabulary — OntologyService.AGGREGATIONS, labeled.
const OBJECT_OPS: Record<string, string> = {
  count: "Number of objects",
  sum: "Total",
  avg: "Average",
  median: "Middle value (median)",
  min: "Smallest",
  max: "Largest",
  count_distinct: "Number of different values",
};
const NUMERIC_PROP_TYPES = new Set(["integer", "long", "double", "float", "decimal", "number"]);

/** Default alias for an object metric — mirrors `defaultAlias` on the
 *  dataset side, so renaming follows the pickers until the author types. */
function defaultObjAlias(op: string, property: string): string {
  return op === "count" ? "count" : `${op} ${property}`.trim();
}

interface ObjectState {
  typeName: string;
  groupBy: string[];
  metrics: { op: string; property: string; alias: string }[];
  filters: { property: string; value: string }[];
  search: string;
}

function emptyObjectState(typeName = ""): ObjectState {
  return {
    typeName,
    groupBy: [],
    metrics: [{ op: "count", property: "", alias: "count" }],
    filters: [],
    search: "",
  };
}

function newPanelId(): string {
  return Math.random().toString(36).slice(2, 10);
}

/** A NAME_RE-safe suggestion for the analysis a quick chart promotes into. */
function suggestAnalysisName(dataset: string): string {
  const base = dataset.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^[^a-z]+/, "");
  return (base ? `${base}-analysis` : "quick-chart-analysis").slice(0, 63);
}

// ---------------------------------------------------------------------------

export function QuickChart() {
  const auth = useAuth();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const editDashboard = params.get("dashboard") ?? "";
  const editPanel = params.get("panel") ?? "";
  // `?dataset=` is the dataset detail page's "chart it" door: start shaping
  // that dataset instead of resuming the draft — the caller just told us
  // what they want to look at.
  const presetDataset = params.get("dataset") ?? "";

  // Opened plain (no edit params, no preset), the screen resumes the caller's
  // last draft: one accidental F5 used to wipe an eight-interaction shaping
  // session with nothing but an empty screen to show for it. The draft reads
  // from the merged surface's own key first, then — for one release — the
  // pre-merge Explore key, so upgrade day eats nobody's session.
  const draft = useMemo(
    () =>
      (editDashboard && editPanel) || presetDataset
        ? null
        : loadScratchDraft(sessionStorage),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  const [tab, setTab] = useState<"datasets" | "objects">(draft?.tab ?? "datasets");
  const [state, setState] = useState<ExploreState>(
    draft?.state ?? emptyExplore(presetDataset),
  );
  const [obj, setObj] = useState<ObjectState>(draft?.obj ?? emptyObjectState());
  const [bindings, setBindings] = useState<Bindings>(
    draft
      ? {
          ...AUTO_BINDINGS,
          ...draft.bindings,
          chart: CHART_KINDS.includes(draft.bindings.chart as Bindings["chart"])
            ? (draft.bindings.chart as Bindings["chart"])
            : "bar",
        }
      : { ...AUTO_BINDINGS },
  );
  const [editNote, setEditNote] = useState<string | null>(null);
  // Set when the screen was opened to edit a saved panel; save PUTs back.
  const [editing, setEditing] = useState<{ dashboard: string; panel: string } | null>(null);
  const loadedEdit = useRef(false);

  // Keep the draft current. Session storage, not local: shaping state names
  // datasets and filter values, which should die with the browser session
  // rather than persist on a shared machine. Edit mode is excluded — its
  // source of truth is the saved panel.
  useEffect(() => {
    if (editing || (editDashboard && editPanel)) return;
    try {
      sessionStorage.setItem(SCRATCH_KEY, serializeDraft({ tab, state, obj, bindings }));
    } catch {
      // Storage full or denied: losing the draft beats losing the screen.
    }
  }, [tab, state, obj, bindings, editing, editDashboard, editPanel]);

  // ------------------------------------------------------------ source lists

  const datasetsQ = useQuery({
    queryKey: ["datasets"],
    queryFn: () => api.get<Dataset[]>(`${API}/datasets`),
  });
  const typesQ = useQuery({
    queryKey: ["object-types"],
    queryFn: () => api.get<ObjectTypeDef[]>(`${API}/ontology/object-types`),
    staleTime: 60_000,
  });

  // Schema + kinds of the picked dataset, for every picker on the screen.
  const schemaQ = useQuery({
    queryKey: ["flow-schema", state.dataset],
    queryFn: () =>
      api.get<FlowSchemaResult>(
        `${API}/flows/schema?dataset=${encodeURIComponent(state.dataset)}`,
      ),
    enabled: tab === "datasets" && !!state.dataset,
    staleTime: 30_000,
  });
  const sourceColumns = schemaQ.data?.columns ?? [];
  const sourceKinds = schemaQ.data?.kinds ?? {};

  // ------------------------------------------------------- edit-mode loading

  const editQ = useQuery({
    queryKey: ["dashboard", editDashboard],
    queryFn: () => api.get<Dashboard>(`${API}/dashboards/${editDashboard}`),
    enabled: !!editDashboard && !!editPanel,
  });
  useEffect(() => {
    if (loadedEdit.current || !editQ.data || !editPanel) return;
    loadedEdit.current = true;
    const p = editQ.data.panels.find((q) => q.id === editPanel);
    if (!p) {
      setEditNote(`Panel ${editPanel} is not on dashboard “${editDashboard}”.`);
      return;
    }
    const b: Bindings = {
      chart: p.chart,
      x: p.x,
      y: p.y,
      series: p.series ?? "",
      stacked: p.stacked ?? false,
    };
    setPanelTitle(p.title);
    setPanelWidth(p.width);
    // `p.flow` is `{}` — truthy — on every SQL panel (the model's default),
    // so a truthiness check sent raw-SQL panels into the flow branch and the
    // app unmounted on `.nodes.map`. A flow panel is one with actual nodes.
    if (Array.isArray((p.flow as any)?.nodes) && (p.flow as any).nodes.length > 0) {
      const s = stateFromFlow(p.flow as any, p.top);
      if (!s) {
        setEditNote(
          "This panel's flow has a shape the quick chart cannot edit — it was " +
            "probably authored or reworked in the pipeline builder. Starting " +
            "fresh from the same dataset.",
        );
        const src = ((p.flow as any)?.nodes ?? []).find((n: any) => n.kind === "source");
        setState(emptyExplore(src?.params?.dataset ?? ""));
      } else {
        setState(s);
      }
      setTab("datasets");
      setBindings(b);
      setEditing({ dashboard: editDashboard, panel: editPanel });
    } else if (p.object_type) {
      setObj({
        typeName: p.object_type,
        groupBy: p.group_by ?? [],
        metrics: (p.metrics ?? []).map((m) => ({
          op: m.op,
          property: m.property ?? "",
          alias: m.alias ?? m.op,
        })),
        filters: Object.entries(p.filters ?? {}).map(([property, value]) => ({ property, value })),
        search: p.search ?? "",
      });
      setTab("objects");
      setBindings(b);
      setEditing({ dashboard: editDashboard, panel: editPanel });
    } else {
      setEditNote("That panel is a raw-SQL panel — edit it on the dashboard itself.");
    }
  }, [editQ.data, editPanel, editDashboard]);

  // ------------------------------------------------------------- preview (A)

  const issues = useMemo(
    () => (tab === "datasets" ? exploreIssues(state, sourceKinds) : []),
    [tab, state, sourceKinds],
  );
  const flow = useMemo(
    () =>
      tab === "datasets" && state.dataset && issues.length === 0
        ? exploreFlow(state, sourceKinds, auth.user?.username ?? "explore")
        : null,
    [tab, state, sourceKinds, issues, auth.user?.username],
  );

  // Debounced, never per keystroke: previews share the process-wide admission
  // slots with everyone's dashboards (the same reason Flows debounces).
  const previewKey = flow ? JSON.stringify(flow) : "";
  const [debouncedKey, setDebouncedKey] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebouncedKey(previewKey), 400);
    return () => clearTimeout(t);
  }, [previewKey]);
  const settled = previewKey === debouncedKey;

  const previewQ = useQuery({
    queryKey: ["explore-preview", debouncedKey],
    queryFn: ({ signal }) =>
      api.post<ExplorePreviewResult>(
        `${API}/explore/preview`,
        { flow: JSON.parse(debouncedKey), max_rows: PREVIEW_ROWS },
        signal,
      ),
    enabled: tab === "datasets" && !!debouncedKey,
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  });

  // ------------------------------------------------------------- preview (B)

  const objType = (typesQ.data ?? []).find((t) => t.api_name === obj.typeName);
  const objProps = Object.keys(objType?.properties ?? {});
  const objReady =
    tab === "objects" &&
    !!obj.typeName &&
    obj.metrics.length > 0 &&
    obj.metrics.every((m) => m.op === "count" || m.property);
  const objKey = objReady
    ? JSON.stringify({
        t: obj.typeName,
        group_by: obj.groupBy,
        metrics: obj.metrics.map((m) => ({
          op: m.op,
          property: m.op === "count" ? null : m.property,
          alias: m.alias || m.op,
        })),
        filters: Object.fromEntries(
          obj.filters.filter((f) => f.property && f.value !== "").map((f) => [f.property, f.value]),
        ),
        search: obj.search || null,
      })
    : "";
  const [debouncedObjKey, setDebouncedObjKey] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebouncedObjKey(objKey), 400);
    return () => clearTimeout(t);
  }, [objKey]);

  const objPreviewQ = useQuery({
    queryKey: ["explore-object-preview", debouncedObjKey],
    queryFn: ({ signal }) => {
      const body = JSON.parse(debouncedObjKey);
      return api
        .post<AggregateResult>(
          `${API}/ontology/objects/${encodeURIComponent(body.t)}/aggregate`,
          {
            group_by: body.group_by,
            metrics: body.metrics,
            filters: body.filters,
            search: body.search,
            limit: PREVIEW_ROWS,
          },
          signal,
        )
        .then((agg): QueryResult & { masked_properties?: string[] } => {
          // Same normalization the panel run route performs: grouping keys,
          // then one column per metric under its alias.
          const columns = [
            ...body.group_by,
            ...body.metrics.map((m: any, i: number) => m.alias || m.op || `metric_${i}`),
          ];
          return {
            columns,
            rows: agg.groups,
            row_count: agg.groups.length,
            truncated: agg.truncated,
            masked_properties: agg.masked_properties,
          };
        });
    },
    enabled: tab === "objects" && !!debouncedObjKey,
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  });

  // ------------------------------------------------------------ result shape

  // Apply Top N to the preview the same way the saved panel's run applies it:
  // the rows arrive sorted (the sort is part of the flow), so keeping the
  // first N here is exactly the bound LIMIT the panel will carry. Without
  // this the preview showed all rows and the saved panel showed N — the
  // author's first "why is the dashboard different" ticket.
  const topN =
    tab === "datasets" && state.top.trim() !== "" && Number(state.top) >= 1
      ? Math.trunc(Number(state.top))
      : null;
  const rawResult: QueryResult | undefined =
    tab === "datasets" ? previewQ.data : objPreviewQ.data;
  const topApplied = !!rawResult && topN !== null && rawResult.rows.length > topN;
  const result: QueryResult | undefined = useMemo(() => {
    if (!rawResult || !topApplied) return rawResult;
    return {
      ...rawResult,
      rows: rawResult.rows.slice(0, topN!),
      row_count: topN!,
      truncated: false,
    };
  }, [rawResult, topApplied, topN]);
  const previewError = tab === "datasets" ? previewQ.error : objPreviewQ.error;
  const previewLoading =
    tab === "datasets"
      ? previewQ.isLoading || (!!previewKey && !settled)
      : objPreviewQ.isLoading || (!!objKey && objKey !== debouncedObjKey);

  /** Kinds of the RESULT's columns, for the binding dropdowns. The dataset
   *  path gets them from the compiler; the object path infers from values. */
  const resultKinds: Record<string, FlowKind> = useMemo(() => {
    if (tab === "datasets") return previewQ.data?.kinds ?? {};
    const out: Record<string, FlowKind> = {};
    const sample = objPreviewQ.data?.rows[0];
    for (const c of objPreviewQ.data?.columns ?? []) {
      const v = sample?.[c];
      out[c] = typeof v === "number" ? "number" : typeof v === "boolean" ? "boolean" : "text";
    }
    return out;
  }, [tab, previewQ.data, objPreviewQ.data]);
  const resultCols = result?.columns ?? [];
  const numericResultCols = resultCols.filter((c) => resultKinds[c] === "number");
  const categoricalResultCols = resultCols.filter(
    (c) => resultKinds[c] === "text" || resultKinds[c] === "boolean",
  );

  // Default bindings come from the SHAPING, not from value-type inference:
  // x is the first group column, y the measures. Inference alone puts a
  // numeric group column (a histogram's bin) on the y axis as a series,
  // because all it can see is "this column holds numbers".
  const defaultX = useMemo(() => {
    const groups =
      tab === "datasets"
        ? resultColumns(state).filter((c) => !state.measures.some((m) => m.alias === c))
        : obj.groupBy;
    return groups.find((c) => resultCols.includes(c)) ?? "";
  }, [tab, state, obj.groupBy, resultCols]);
  const defaultY = useMemo(() => {
    const aliases =
      tab === "datasets"
        ? state.measures.map((m) => m.alias)
        : obj.metrics.map((m) => m.alias || m.op);
    const y = aliases.filter((a) => resultCols.includes(a));
    return y.length > 0 ? y : numericResultCols;
  }, [tab, state.measures, obj.metrics, resultCols, numericResultCols]);
  const effectiveX = bindings.x || defaultX;
  const effectiveY = bindings.y.length > 0 ? bindings.y : defaultY;

  // Bindings must only ever name columns the result has: prune when it changes.
  useEffect(() => {
    if (!result) return;
    setBindings((b) => {
      const x = b.x && resultCols.includes(b.x) ? b.x : "";
      const y = b.y.filter((c) => resultCols.includes(c));
      const series = b.series && resultCols.includes(b.series) ? b.series : "";
      if (x === b.x && series === b.series && y.length === b.y.length) return b;
      return { ...b, x, y, series };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resultCols.join("\x00")]);

  // Columns masked for this caller, to grey out of the measure pickers.
  const maskedColumns = useMemo(() => {
    const out = new Set<string>();
    for (const cols of Object.values(previewQ.data?.masked_columns ?? {}))
      for (const c of cols) out.add(c);
    return out;
  }, [previewQ.data?.masked_columns]);

  // Same courtesy on the object path: grouping by a masked property used to
  // be offered like any other and produced one inexplicable "***" bar.
  const objMaskedProps = useMemo(
    () =>
      new Set<string>(
        ((objPreviewQ.data as { masked_properties?: string[] } | undefined)
          ?.masked_properties) ?? [],
      ),
    [objPreviewQ.data],
  );

  // ------------------------------------------------------------------- save

  const [saveOpen, setSaveOpen] = useState(false);
  const [dashName, setDashName] = useState(editDashboard);
  const [panelTitle, setPanelTitle] = useState("");
  const [panelWidth, setPanelWidth] = useState(6);
  const [savedTo, setSavedTo] = useState<string | null>(null);

  const dashboardsQ = useQuery({
    queryKey: ["dashboards"],
    queryFn: () => api.get<Dashboard[]>(`${API}/dashboards`),
    enabled: saveOpen,
  });

  const save = useMutation({
    mutationFn: async () => {
      const name = dashName.trim();
      const panel: Partial<DashboardPanel> = {
        id: editing?.panel ?? newPanelId(),
        title: panelTitle.trim(),
        chart: bindings.chart,
        // The *effective* bindings, so the dashboard draws exactly what the
        // author is looking at instead of re-running inference — which puts a
        // numeric group column on the wrong axis.
        x: effectiveX,
        y: effectiveY,
        series: bindings.series,
        stacked: bindings.stacked,
        width: panelWidth,
      };
      if (tab === "datasets") {
        // `flow` is null exactly when `issues` is non-empty, so name the
        // actual problem. The generic sentence used to appear in the save
        // dialog while a finished-looking (stale) chart sat behind it, with
        // the real cause — an invalid summary name, say — never referenced.
        if (!flow) throw new Error(issues[0] ?? "The query is not finished yet.");
        panel.flow = flow as any;
        panel.top = state.top.trim() === "" ? null : Number(state.top);
      } else {
        panel.object_type = obj.typeName;
        panel.group_by = obj.groupBy;
        panel.metrics = obj.metrics.map((m) => ({
          op: m.op,
          property: m.op === "count" ? null : m.property,
          alias: m.alias || m.op,
        }));
        panel.filters = Object.fromEntries(
          obj.filters.filter((f) => f.property && f.value !== "").map((f) => [f.property, f.value]),
        );
        panel.search = obj.search;
      }
      const base = `${API}/dashboards/${encodeURIComponent(name)}`;
      if (editing && editing.dashboard === name) {
        return api.put<Dashboard>(`${base}/panels/${encodeURIComponent(editing.panel)}`, panel);
      }
      const exists = (dashboardsQ.data ?? []).some((d) => d.name === name);
      if (!exists) {
        await api.put<Dashboard>(base, { title: name, description: "", panels: [] });
      }
      return api.post<Dashboard>(`${base}/panels`, panel);
    },
    onSuccess: (d) => {
      setSaveOpen(false);
      setSavedTo(d.name);
      qc.invalidateQueries({ queryKey: ["dashboard", d.name] });
      qc.invalidateQueries({ queryKey: ["dashboards"] });
      qc.invalidateQueries({ queryKey: ["panel-run", d.name] });
    },
  });

  // -------------------------------------------------- promotion to a document

  // "Keep going": the quick chart becomes cell 1 of a new analysis, in place.
  // Dataset drafts only — a cell reads datasets (or earlier cells), and the
  // object aggregate path has no cell form yet, so the button says that
  // instead of greying silently.
  const [keepOpen, setKeepOpen] = useState(false);
  const [anaName, setAnaName] = useState("");
  const [anaTitle, setAnaTitle] = useState("");

  const analysesQ = useQuery({
    queryKey: ["analyses"],
    queryFn: () => api.get<Analysis[]>(`${API}/analyses`),
    enabled: keepOpen,
  });

  const promote = useMutation({
    mutationFn: async () => {
      const name = anaName.trim();
      if (!flow) throw new Error(issues[0] ?? "The chart is not finished yet.");
      if ((analysesQ.data ?? []).some((a) => a.name === name)) {
        throw new Error(`An analysis named “${name}” already exists — pick another name.`);
      }
      const shaping: CellShaping = {
        source: `ds:${state.dataset}`,
        filters: state.filters,
        groups: state.groups,
        measures: state.measures,
        sort: state.sort,
        top: state.top,
      };
      const frag = cellFragment(shaping, sourceKinds);
      if (!frag) throw new Error(issues[0] ?? "The chart is not finished yet.");
      const created = await api.put<Analysis>(`${API}/analyses/${encodeURIComponent(name)}`, {
        title: anaTitle.trim() || name,
        cells: [],
      });
      await api.post<Analysis>(`${API}/analyses/${encodeURIComponent(name)}/cells`, {
        title: panelTitle.trim() || `${state.dataset} quick chart`,
        chart: bindings.chart,
        x: effectiveX,
        y: effectiveY,
        series: bindings.series,
        stacked: bindings.stacked,
        width: 12,
        flow: frag.flow,
        inputs: frag.inputs,
        top: frag.top,
      });
      return created;
    },
    onSuccess: (a) => {
      // The shaping lives in the document now; a stale scratch draft would
      // resurrect the pre-promotion chart on the next visit.
      try {
        sessionStorage.removeItem(SCRATCH_KEY);
        sessionStorage.removeItem(LEGACY_SCRATCH_KEY);
      } catch {
        // Storage denied: the draft going stale beats the promotion failing.
      }
      qc.invalidateQueries({ queryKey: ["analyses"] });
      navigate(`/analyses/${a.name}`);
    },
  });

  // ------------------------------------------------------------------ render

  const canPreview = tab === "datasets" ? !!state.dataset : !!obj.typeName;
  const chartReady = !!result && !previewError && (tab !== "datasets" || issues.length === 0);

  return (
    <div className="ex-page">
      <div className="qc-head">
        <div>
          <h2 className="qc-title">Quick chart</h2>
          {/* The promise used to be unconditional — "keep going from here when
              the chart turns into a question" — and it is false for half the
              sources this page offers: an analysis cell reads datasets, so an
              object chart cannot become one. A page that promises a door half
              its readers do not have is the phantom-door problem the Datasets
              empty state already had to fix. Qualify it here rather than let
              the reader discover it on a disabled button. */}
          <p className="hint" style={{ margin: "2px 0 0" }}>
            One chart, no name, no saved record — shape it by clicking and send
            it to a dashboard. Everything runs with your own data access. A
            chart over a <strong>dataset</strong> can also keep going as a{" "}
            <strong>new analysis</strong>; a chart over <strong>objects</strong>{" "}
            goes to a dashboard.
          </p>
        </div>
        <span style={{ display: "inline-flex", gap: 8, flexShrink: 0, alignItems: "flex-start" }}>
          <span style={{ display: "inline-flex", flexDirection: "column", gap: 4 }}>
          <button
            title={
              tab === "objects"
                ? undefined
                : tab === "datasets" && issues.length > 0
                  ? issues[0]
                  : "Continue this shaping as cell 1 of a new analysis"
            }
            // The reason this is dark is a permanent product limitation, not a
            // transient state, and it lived only in a `title` — invisible to
            // touch, to keyboard (a disabled button never takes focus, so the
            // tooltip is unreachable), and to anyone who does not think to
            // hover a control that looks broken. It is stated inline below.
            aria-disabled={tab === "objects" || !chartReady ? true : undefined}
            disabled={tab === "objects" || !chartReady}
            onClick={() => {
              promote.reset();
              setAnaName(suggestAnalysisName(state.dataset));
              setAnaTitle("");
              setKeepOpen(true);
            }}
          >
            Keep going → analysis
          </button>
          {tab === "objects" && (
            <span className="hint" style={{ maxWidth: 220, fontSize: 12 }}>
              An analysis reads datasets, so an object chart can't become one
              yet. Save it to a dashboard instead.
            </span>
          )}
          </span>
          <button
            className="primary"
            // `result` alone is not enough: it can be the STALE preview of a
            // shaping that has since acquired an issue (the preview query
            // disables rather than refetches), and saving then failed with a
            // sentence that named neither the field nor the fix. Disable with
            // the issue as the tooltip instead of letting the click fail.
            disabled={!chartReady}
            title={tab === "datasets" && issues.length > 0 ? issues[0] : undefined}
            onClick={() => {
              setSavedTo(null);
              save.reset();
              setDashName(editing?.dashboard ?? dashName);
              setSaveOpen(true);
            }}
          >
            {editing ? "Save panel" : "Save to dashboard"}
          </button>
        </span>
      </div>
      {editNote && <Note tone="warn">{editNote}</Note>}
      {editing && !editNote && (
        <Note>
          Editing panel “{panelTitle || editing.panel}” on{" "}
          <Link to={`/dashboards/${editing.dashboard}`}>{editing.dashboard}</Link> — saving
          updates it in place.
        </Note>
      )}
      {savedTo && (
        <Note tone="ok">
          Panel saved — <Link to={`/dashboards/${savedTo}`}>open dashboard “{savedTo}”</Link>.
          Viewers get the chart; the query itself stays on the server.
        </Note>
      )}

      <div className="ex-grid">
        {/* ------------------------------------------------------ source rail */}
        <aside className="ex-rail">
          <div className="ex-tabs">
            <button
              className={tab === "datasets" ? "small primary" : "small"}
              onClick={() => setTab("datasets")}
            >
              Datasets
            </button>
            <button
              className={tab === "objects" ? "small primary" : "small"}
              onClick={() => setTab("objects")}
            >
              Objects
            </button>
          </div>
          {tab === "datasets" ? (
            datasetsQ.isLoading ? (
              <Spinner />
            ) : (
              <>
                <ul className="ex-src-list">
                  {/* A dataset declared but never built has no version, so
                      there is no data to shape: picking it used to fetch a
                      schema and a preview, and print `Error 404` twice on one
                      screen for a name this picker offered. Grey it and say
                      why — the reader sees the name on Datasets, so removing
                      it would only move the confusion. */}
                  {(datasetsQ.data ?? []).map((d) => {
                    const noVersion = d.latest_version == null;
                    return (
                    <li key={d.name}>
                      <button
                        className={`ex-src-item mono${state.dataset === d.name ? " active" : ""}`}
                        aria-disabled={noVersion ? true : undefined}
                        disabled={noVersion}
                        title={
                          noVersion
                            ? "No versions yet — build or import into this dataset before charting it."
                            : undefined
                        }
                        onClick={() => {
                          if (state.dataset !== d.name) {
                            // Columns belong to a dataset: carrying shaping over
                            // would group by a column that no longer exists.
                            setState(emptyExplore(d.name));
                            setBindings({ ...AUTO_BINDINGS });
                          }
                        }}
                      >
                        {d.name}
                        {noVersion && <span className="faint"> (no versions yet)</span>}
                      </button>
                    </li>
                    );
                  })}
                </ul>
                {(datasetsQ.data ?? []).length === 0 && (
                  <EmptyState>
                    No datasets you can read — import one on the{" "}
                    <Link to="/datasets">Datasets</Link> page.
                  </EmptyState>
                )}
              </>
            )
          ) : typesQ.isLoading ? (
            <Spinner />
          ) : (
            <>
              <ul className="ex-src-list">
                {(typesQ.data ?? []).map((t) => (
                  <li key={t.api_name}>
                    <button
                      className={`ex-src-item${obj.typeName === t.api_name ? " active" : ""}`}
                      onClick={() => {
                        if (obj.typeName !== t.api_name) {
                          setObj(emptyObjectState(t.api_name));
                          setBindings({ ...AUTO_BINDINGS });
                        }
                      }}
                    >
                      {t.display_name || t.api_name}
                    </button>
                  </li>
                ))}
              </ul>
              {(typesQ.data ?? []).length === 0 && (
                <EmptyState>
                  No object types yet — they are defined in{" "}
                  <span className="mono">ontology/*.yml</span>.
                </EmptyState>
              )}
            </>
          )}
          <p className="hint" style={{ marginTop: 10 }}>
            {tab === "datasets"
              ? "Only datasets you can read are listed. The chart shows your view of the data — filters, masks and all."
              : "Aggregates objects, so the chart includes edits made by actions."}
          </p>
        </aside>

        {/* --------------------------------------------------------- shaping */}
        <section className="ex-shape">
          {!canPreview ? (
            <EmptyState>
              Pick {tab === "datasets" ? "a dataset" : "an object type"} on the left to start.
            </EmptyState>
          ) : tab === "datasets" ? (
            schemaQ.error != null ? (
              <ErrorBox error={schemaQ.error} />
            ) : (
              <ShapingCards
                state={state}
                set={(fn) => setState((s) => ({ ...s, ...fn(s) }))}
                columns={sourceColumns}
                kinds={sourceKinds}
                masked={maskedColumns}
                measuresRequired
                suggestDataset={state.dataset}
                idPrefix="qc"
              />
            )
          ) : (
            <ObjectShaping
              obj={obj}
              setObj={setObj}
              type={objType}
              properties={objProps}
              maskedProps={objMaskedProps}
            />
          )}
        </section>

        {/* ----------------------------------------------------------- chart */}
        <section className="ex-chart">
          <ChartKindBar
            value={bindings.chart}
            onChange={(k) => setBindings((b) => ({ ...b, chart: k }))}
          />

          {result && bindings.chart !== "table" && bindings.chart !== "stat" && (
            <BindingsRow
              bindings={bindings}
              setBindings={setBindings}
              columns={resultCols}
              numericCols={numericResultCols}
              categoricalCols={categoricalResultCols}
              idPrefix="qc"
            />
          )}

          {issues.length > 0 && canPreview && (
            <Note>{issues[0]}</Note>
          )}
          {previewError != null && (
            <Note tone="bad">
              {/* Refusals arrive in compiler vocabulary ("step 's3'"); rewrite
                  them into this screen's cards before an analyst reads them. */}
              {previewError instanceof ApiError
                ? explainRefusal(
                    previewError.detail,
                    // The flow the failing preview actually ran, not the one
                    // being typed now.
                    tab === "datasets" && debouncedKey ? JSON.parse(debouncedKey) : null,
                  )
                : String(previewError)}
            </Note>
          )}
          {previewLoading && <Spinner label="Running…" />}

          {result && !previewError && (
            <div className="card" style={{ marginTop: 8 }}>
              {result.truncated && (
                <div className="ex-truncated">
                  Showing the {truncationNote(result.row_count)} — add a filter or a
                  Top&nbsp;N to see a definite set.
                </div>
              )}
              {topApplied && (
                <div className="faint" style={{ fontSize: 11.5, marginBottom: 6 }}>
                  Top {topN} kept{state.sort ? "" : " — arbitrary without an order"}.
                </div>
              )}
              {result.rows.length === 0 && tab === "datasets" && state.filters.length > 0 && (
                // An empty result behind an active filter is ambiguous: bad
                // filter value, or genuinely empty data? Say which check to
                // make — "South" for "south" used to just render nothing.
                <Note>
                  No rows matched your filters. Values must match the data exactly,
                  including capital letters — pick from the suggestions in the value
                  box to be sure.
                </Note>
              )}
              {bindings.chart === "table" ? (
                <ResultTable result={result} maxHeight={420} />
              ) : (
                <Chart
                  data={result}
                  kind={bindings.chart}
                  x={effectiveX}
                  y={effectiveY}
                  series={bindings.series}
                  stacked={bindings.stacked}
                />
              )}
              <div className="faint" style={{ fontSize: 11.5, marginTop: 8 }}>
                {result.row_count.toLocaleString("en-US")} row
                {result.row_count === 1 ? "" : "s"} · computed with your data access
              </div>
            </div>
          )}
        </section>
      </div>

      {saveOpen && (
        <Modal
          label={editing ? "Save panel" : "Save to dashboard"}
          onClose={() => !save.isPending && setSaveOpen(false)}
        >
          <div className="card-title">{editing ? "Save panel" : "Save to dashboard"}</div>
          <p className="dim" style={{ fontSize: 12.5, marginTop: 4 }}>
            Viewers of the dashboard get the chart, computed with <em>their</em> data
            access. The shaping itself is never sent to them.
          </p>
          <div className="field" style={{ marginTop: 12 }}>
            <label htmlFor="qc-save-dash">Dashboard</label>
            <input
              id="qc-save-dash"
              className="mono"
              autoFocus
              list="qc-dash-list"
              placeholder="revenue"
              value={dashName}
              onChange={(e) => setDashName(e.target.value)}
              disabled={!!editing}
            />
            <datalist id="qc-dash-list">
              {(dashboardsQ.data ?? []).map((d) => (
                <option key={d.name} value={d.name}>{d.title || d.name}</option>
              ))}
            </datalist>
            {!editing && (
              <div className="hint">
                {NAME_RULE} Type a new name to create a dashboard.
              </div>
            )}
          </div>
          <div className="field">
            <label htmlFor="qc-save-title">Panel title</label>
            <input
              id="qc-save-title"
              value={panelTitle}
              onChange={(e) => setPanelTitle(e.target.value)}
              placeholder="Revenue by region"
            />
          </div>
          <div className="field">
            <label htmlFor="qc-save-width">Width (1–12)</label>
            <input
              id="qc-save-width"
              type="number"
              min={1}
              max={12}
              value={panelWidth}
              onChange={(e) => setPanelWidth(Math.max(1, Math.min(12, Number(e.target.value) || 6)))}
              style={{ width: 80 }}
            />
          </div>
          {save.error != null && <ErrorBox error={save.error} />}
          <div className="toolbar" style={{ marginTop: 16, justifyContent: "flex-end" }}>
            <button disabled={save.isPending} onClick={() => setSaveOpen(false)}>
              Cancel
            </button>
            <button
              className="primary"
              disabled={save.isPending || !NAME_RE.test(dashName.trim())}
              onClick={() => save.mutate()}
            >
              {save.isPending ? "Saving…" : editing ? "Save changes" : "Save panel"}
            </button>
          </div>
        </Modal>
      )}

      {keepOpen && (
        <Modal
          label="Keep going in an analysis"
          onClose={() => !promote.isPending && setKeepOpen(false)}
        >
          <div className="card-title">Keep going in an analysis</div>
          <p className="dim" style={{ fontSize: 12.5, marginTop: 4 }}>
            This shaping becomes cell&nbsp;1 of a new analysis — a multi-step
            document later cells can build on, shared by URL. The quick chart
            here starts fresh.
          </p>
          <div className="field" style={{ marginTop: 12 }}>
            <label htmlFor="qc-keep-name">Name</label>
            <input
              id="qc-keep-name"
              className="mono"
              autoFocus
              value={anaName}
              onChange={(e) => setAnaName(e.target.value)}
            />
            <div className="hint">{NAME_RULE}</div>
          </div>
          <div className="field">
            <label htmlFor="qc-keep-title">Title</label>
            <input
              id="qc-keep-title"
              value={anaTitle}
              onChange={(e) => setAnaTitle(e.target.value)}
              placeholder={`${state.dataset} investigation`}
            />
          </div>
          {promote.error != null && <ErrorBox error={promote.error} />}
          <div className="toolbar" style={{ marginTop: 16, justifyContent: "flex-end" }}>
            <button disabled={promote.isPending} onClick={() => setKeepOpen(false)}>
              Cancel
            </button>
            <button
              className="primary"
              disabled={promote.isPending || !NAME_RE.test(anaName.trim())}
              onClick={() => promote.mutate()}
            >
              {promote.isPending ? "Creating…" : "Create analysis"}
            </button>
          </div>
        </Modal>
      )}

      <style>{QUICK_CHART_STYLES + SHAPING_STYLES}</style>
    </div>
  );
}

// ---------------------------------------------------------- object shaping

function ObjectShaping({
  obj,
  setObj,
  type,
  properties,
  maskedProps,
}: {
  obj: ObjectState;
  setObj: (fn: (s: ObjectState) => ObjectState) => void;
  type: ObjectTypeDef | undefined;
  properties: string[];
  /** Properties the caller's column masks cover. Grouping by one collapses
   *  every object into a single "***" group — governance holding, but
   *  inexplicable unless the picker says so, the way the dataset path's
   *  pickers already do. */
  maskedProps: Set<string>;
}) {
  const set = setObj;
  const numericProps = properties.filter((p) =>
    NUMERIC_PROP_TYPES.has((type?.properties?.[p]?.type ?? "").toLowerCase()),
  );

  return (
    <>
      <div className="ex-card">
        {/* Card titles are the same four words on both source tabs — Filter,
            Group by, Summarise, Order & top N — because switching tabs must
            change the *nouns* the data has ("rows" vs "objects"), never the
            names of the controls. A reader who learned the stack on datasets
            was previously handed a differently-titled stack on objects and had
            to re-learn a UI they already knew. */}
        <div className="ex-card-title">Filter</div>
        {obj.filters.map((f, i) => (
          <div key={i} className="ex-row">
            <select
              aria-label="Filter property"
              value={f.property}
              onChange={(e) =>
                set((s) => ({
                  ...s,
                  filters: s.filters.map((x, j) => (j === i ? { ...x, property: e.target.value } : x)),
                }))
              }
            >
              <option value="">Pick a property…</option>
              {properties.map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
            <span className="ex-kw">is</span>
            <input
              aria-label="Filter value"
              value={f.value}
              placeholder="value"
              onChange={(e) =>
                set((s) => ({
                  ...s,
                  filters: s.filters.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)),
                }))
              }
            />
            <button
              className="ex-x"
              aria-label="Remove this filter"
              onClick={() => set((s) => ({ ...s, filters: s.filters.filter((_, j) => j !== i) }))}
            >
              ×
            </button>
          </div>
        ))}
        <button
          className="ex-add"
          onClick={() => set((s) => ({ ...s, filters: [...s.filters, { property: "", value: "" }] }))}
        >
          {/* Same sentence shape as the dataset tab's "+ keep only rows
              where…"; only the noun changes, because the thing being kept
              genuinely is an object and not a row. */}
          + keep only objects where…
        </button>
        <div className="ex-row" style={{ marginTop: 6 }}>
          <span className="ex-kw">search</span>
          <input
            aria-label="Free-text search over the objects"
            value={obj.search}
            placeholder="free-text search over the objects"
            onChange={(e) => set((s) => ({ ...s, search: e.target.value }))}
          />
        </div>
      </div>

      <div className="ex-card">
        <div className="ex-card-title">Group by</div>
        <div className="ex-checklist">
          {properties.map((p, i) => {
            const isMasked = maskedProps.has(p);
            return (
              <label
                key={p}
                htmlFor={`qc-obj-group-${i}`}
                className="check-inline"
                title={
                  isMasked
                    ? "Masked for you — every object would land in one “***” group."
                    : undefined
                }
                style={isMasked ? { opacity: 0.55 } : undefined}
              >
                <input
                  id={`qc-obj-group-${i}`}
                  type="checkbox"
                  disabled={isMasked && !obj.groupBy.includes(p)}
                  checked={obj.groupBy.includes(p)}
                  onChange={(e) =>
                    set((s) => ({
                      ...s,
                      groupBy: e.target.checked
                        ? [...s.groupBy.filter((x) => x !== p), p]
                        : s.groupBy.filter((x) => x !== p),
                    }))
                  }
                />
                <span>{p}{isMasked ? " (masked for you)" : ""}</span>
              </label>
            );
          })}
        </div>
        {/* The dataset tab has said this since the shaping cards merged; the
            object tab did not, so an author who ticked nothing had no way to
            know an aggregate over everything was what they were about to get.
            Same sentence, so the fact has one voice. */}
        {obj.groupBy.length === 0 && (
          <div className="hint">No grouping = one summary row over everything.</div>
        )}
      </div>

      <div className="ex-card">
        <div className="ex-card-title">Summarise</div>
        {obj.metrics.map((m, i) => (
          <div key={i} className="ex-row">
            <select
              aria-label="Summary function"
              value={m.op}
              onChange={(e) => {
                const op = e.target.value;
                set((s) => ({
                  ...s,
                  metrics: s.metrics.map((x, j) =>
                    j === i
                      ? {
                          op,
                          property: op === "count" ? "" : x.property,
                          alias:
                            x.alias === defaultObjAlias(x.op, x.property) || /^metric_\d+$/.test(x.alias)
                              ? defaultObjAlias(op, op === "count" ? "" : x.property)
                              : x.alias,
                        }
                      : x,
                  ),
                }));
              }}
            >
              {Object.entries(OBJECT_OPS).map(([op, label]) => (
                <option key={op} value={op}>{label}</option>
              ))}
            </select>
            {m.op !== "count" && (
              <>
                <span className="ex-kw">of</span>
                <select
                  aria-label="Property to summarise"
                  value={m.property}
                  onChange={(e) => {
                    const property = e.target.value;
                    set((s) => ({
                      ...s,
                      metrics: s.metrics.map((x, j) =>
                        j === i
                          ? {
                              ...x,
                              property,
                              alias:
                                x.alias === defaultObjAlias(x.op, x.property) || /^metric_\d+$/.test(x.alias)
                                  ? defaultObjAlias(x.op, property)
                                  : x.alias,
                            }
                          : x,
                      ),
                    }));
                  }}
                >
                  <option value="">Pick a property…</option>
                  {(["sum", "avg", "median"].includes(m.op) ? numericProps : properties).map((p) =>
                    maskedProps.has(p) ? (
                      <option
                        key={p}
                        value={p}
                        disabled
                        title="Masked for you — you would only ever summarise '***'."
                      >
                        {p} (masked for you)
                      </option>
                    ) : (
                      <option key={p} value={p}>{p}</option>
                    ),
                  )}
                </select>
              </>
            )}
            <span className="ex-kw">called</span>
            <input
              aria-label="Name in the result"
              style={{ width: 120 }}
              value={m.alias}
              onChange={(e) =>
                set((s) => ({
                  ...s,
                  metrics: s.metrics.map((x, j) => (j === i ? { ...x, alias: e.target.value } : x)),
                }))
              }
            />
            <button
              className="ex-x"
              disabled={obj.metrics.length === 1}
              aria-label="Remove this summary"
              onClick={() => set((s) => ({ ...s, metrics: s.metrics.filter((_, j) => j !== i) }))}
            >
              ×
            </button>
          </div>
        ))}
        <button
          className="ex-add"
          onClick={() =>
            set((s) => ({ ...s, metrics: [...s.metrics, { op: "count", property: "", alias: `metric_${s.metrics.length + 1}` }] }))
          }
        >
          + add a summary
        </button>
      </div>

      {/* The dataset tab's fourth card is "Order & top N". The object
          aggregate endpoint (POST /ontology/objects/{type}/aggregate) has no
          ordering or limit, so there is no card to render — and the object tab
          simply ended one card early, which reads as "you missed something"
          rather than "this does not exist here". A capability gap the reader
          can see is a limitation; a capability gap they cannot is a bug they
          will hunt for. State it in the card's place. */}
      <div className="ex-card">
        <div className="ex-card-title">Order &amp; top N</div>
        <div className="hint">
          Ordering and top-N aren't available for object charts yet. Charting a
          dataset gives you both.
        </div>
      </div>
    </>
  );
}

// ------------------------------------------------------------------- styles

const QUICK_CHART_STYLES = `
.ex-page { display: flex; flex-direction: column; gap: 12px; }
.qc-head { display: flex; gap: 16px; justify-content: space-between; align-items: flex-start; }
.qc-title { font-size: 15px; margin: 0; }
.ex-grid {
  display: grid; grid-template-columns: 190px 360px minmax(0, 1fr);
  gap: 16px; align-items: start;
}
@media (max-width: 1150px) { .ex-grid { grid-template-columns: 190px minmax(0, 1fr); } .ex-chart { grid-column: 1 / -1; } }
.ex-rail {
  background: var(--bg-1); border: 1px solid var(--border);
  border-radius: 10px; padding: 12px;
}
.ex-tabs { display: flex; gap: 6px; margin-bottom: 8px; }
.ex-src-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 1px; max-height: 50vh; overflow-y: auto; }
.ex-src-item {
  width: 100%; text-align: left; background: transparent; border: none;
  color: var(--text-dim); padding: 5px 8px; border-radius: 6px;
  font-size: 12.5px; cursor: pointer;
}
.ex-src-item:hover { background: var(--bg-2); color: var(--text); }
.ex-src-item.active { background: var(--bg-3); color: var(--gold); }
.ex-checklist { display: flex; flex-direction: column; gap: 3px; max-height: 180px; overflow-y: auto; }
.ex-chart { min-width: 0; }
`;
