// Explore: point-and-click data-to-chart. Pick a dataset or an object type,
// shape it by clicking (filter, group, summarise, sort, top-N), watch the
// chart update live, save it to a dashboard.
//
// The audience is an analyst who does not write SQL. Every control is a
// dropdown over a closed vocabulary or a column picker fed by the live
// schema; the only free text is values (typed by column kind and *bound*,
// never spliced) and invented names (aliases, panel titles).
//
// Dataset source: the state synthesizes a FlowDef (views/explore/model.ts)
// and previews through POST /explore/preview — the Flow compiler's stack,
// running as YOU: your ACL, your row policy, your column masks. What you see
// is what you may read, and nothing else.
//
// Object source: the same gestures compile to the ontology aggregate API
// (POST /ontology/objects/{type}/aggregate) — no SQL synthesis at all, and
// the result reflects edits made by actions (the edit overlay), which raw
// dataset SQL cannot see.
//
// Saving writes a dashboard panel through the per-panel routes. A flow
// panel's `flow`/`top` are OPERATIONAL like `sql`: a viewer gets the chart
// from /run — the query never leaves the server.

import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api, ApiError } from "../api";
import { useAuth } from "../auth";
import { Chart } from "../charts";
import type {
  AggregateResult,
  ChartKind,
  Dashboard,
  DashboardPanel,
  Dataset,
  FlowAggFn,
  FlowKind,
  FlowSchemaResult,
  ObjectTypeDef,
  QueryResult,
} from "../types";
import { EmptyState, ErrorBox, Note, PageHeader, Spinner, fmtValue } from "../ui";
import {
  DATE_BUCKETS,
  DRAFT_KEY,
  FILTER_OPS,
  MEASURE_FNS,
  NUMERIC_FNS,
  defaultAlias,
  distinctValuesFlow,
  emptyExplore,
  exploreFlow,
  exploreIssues,
  explainRefusal,
  groupResultName,
  parseDraft,
  resultColumnKind,
  resultColumns,
  serializeDraft,
  stateFromFlow,
  type ExploreBucket,
  type ExploreFilter,
  type ExploreFilterOp,
  type ExploreState,
} from "./explore/model";

const CHART_KINDS: ChartKind[] = ["table", "bar", "line", "area", "stat", "pie", "scatter"];
const PREVIEW_ROWS = 200;

interface ExplorePreviewResult extends QueryResult {
  schema: string[];
  kinds: Record<string, FlowKind>;
  masked_columns: Record<string, string[]>;
  max_rows: number;
}

/** Presentation bindings, shared by both sources. */
interface Bindings {
  chart: ChartKind;
  x: string;
  y: string[];
  series: string;
  stacked: boolean;
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

// ---------------------------------------------------------------------------

export function ExploreView() {
  const auth = useAuth();
  if (!auth.can("editor")) {
    return (
      <div>
        <PageHeader title="Explore" subtitle="Shape a dataset into a chart by clicking." />
        <EmptyState>
          Explore is an authoring surface — it needs the editor role. Dashboards
          your editors saved are on the <Link to="/dashboards">Dashboards</Link> page.
        </EmptyState>
      </div>
    );
  }
  return <ExploreScreen />;
}

function ExploreScreen() {
  const auth = useAuth();
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const editDashboard = params.get("dashboard") ?? "";
  const editPanel = params.get("panel") ?? "";
  // `?dataset=` is the dataset detail page's "Open in Explore" door: start
  // shaping that dataset instead of resuming the draft — the caller just told
  // us what they want to look at.
  const presetDataset = params.get("dataset") ?? "";

  // Opened plain (no edit params, no preset), the screen resumes the caller's
  // last draft: one accidental F5 used to wipe an eight-interaction shaping
  // session with nothing but an empty screen to show for it.
  const draft = useMemo(
    () =>
      (editDashboard && editPanel) || presetDataset
        ? null
        : parseDraft(sessionStorage.getItem(DRAFT_KEY)),
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
          chart: CHART_KINDS.includes(draft.bindings.chart as ChartKind)
            ? (draft.bindings.chart as ChartKind)
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
      sessionStorage.setItem(DRAFT_KEY, serializeDraft({ tab, state, obj, bindings }));
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
          "This panel's flow has a shape Explore cannot edit — it was probably " +
            "authored or reworked in the Flow builder. Starting fresh from the same dataset.",
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

  // ------------------------------------------------------------------ render

  const canPreview = tab === "datasets" ? !!state.dataset : !!obj.typeName;

  return (
    <div className="ex-page">
      <PageHeader
        title="Explore"
        subtitle="Pick data, shape it by clicking, chart it, save it to a dashboard. Everything runs with your own data access."
        actions={
          <button
            className="primary"
            // `result` alone is not enough: it can be the STALE preview of a
            // shaping that has since acquired an issue (the preview query
            // disables rather than refetches), and saving then failed with a
            // sentence that named neither the field nor the fix. Disable with
            // the issue as the tooltip instead of letting the click fail.
            disabled={
              !result || !!previewError || (tab === "datasets" && issues.length > 0)
            }
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
        }
      />
      {/* The one-sentence answer to "why are there two click-to-chart
          screens": this one makes a dashboard panel, Analyses makes a
          multi-step document. Said here and on Analyses, at the moment of
          choosing, because the only other way to learn it is to build the
          same chart twice. */}
      <p className="hint" style={{ marginTop: -8, marginBottom: 12 }}>
        Explore makes <strong>one chart for a dashboard</strong>. For multi-step
        work — cells that build on each other, shared as a document — use{" "}
        <Link to="/analyses">Analyses</Link> instead.
      </p>
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
              <ul className="ex-src-list">
                {(datasetsQ.data ?? []).map((d) => (
                  <li key={d.name}>
                    <button
                      className={`ex-src-item mono${state.dataset === d.name ? " active" : ""}`}
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
                    </button>
                  </li>
                ))}
                {(datasetsQ.data ?? []).length === 0 && (
                  <li className="faint" style={{ fontSize: 12.5, padding: "6px 8px" }}>
                    No datasets you can read.
                  </li>
                )}
              </ul>
            )
          ) : typesQ.isLoading ? (
            <Spinner />
          ) : (
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
              {(typesQ.data ?? []).length === 0 && (
                <li className="faint" style={{ fontSize: 12.5, padding: "6px 8px" }}>
                  No object types yet.
                </li>
              )}
            </ul>
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
            <DatasetShaping
              state={state}
              setState={setState}
              dataset={state.dataset}
              columns={sourceColumns}
              kinds={sourceKinds}
              masked={maskedColumns}
              schemaError={schemaQ.error}
            />
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
          <div className="ex-kinds">
            {CHART_KINDS.map((k) => (
              <button
                key={k}
                className={`small${bindings.chart === k ? " primary" : ""}`}
                onClick={() => setBindings((b) => ({ ...b, chart: k }))}
              >
                {k}
              </button>
            ))}
          </div>

          {result && bindings.chart !== "table" && bindings.chart !== "stat" && (
            <BindingsRow
              bindings={bindings}
              setBindings={setBindings}
              columns={resultCols}
              numericCols={numericResultCols}
              categoricalCols={categoricalResultCols}
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
                  Showing the first {result.row_count.toLocaleString("en-US")} rows — the
                  result is larger. Add a filter or a Top&nbsp;N to see a definite set.
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
                <div className="table-wrap" style={{ maxHeight: 420, overflowY: "auto" }}>
                  <table>
                    <thead>
                      <tr>
                        {result.columns.map((c) => (
                          <th key={c} className="mono">{c}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {result.rows.map((row, i) => (
                        <tr key={i}>
                          {result.columns.map((c) => (
                            <td key={c} className="mono">{fmtValue(row[c])}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
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
        <div className="modal-backdrop" onClick={() => !save.isPending && setSaveOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="card-title">{editing ? "Save panel" : "Save to dashboard"}</div>
            <p className="dim" style={{ fontSize: 12.5, marginTop: 4 }}>
              Viewers of the dashboard get the chart, computed with <em>their</em> data
              access. The shaping itself is never sent to them.
            </p>
            <div className="field" style={{ marginTop: 12 }}>
              <label>Dashboard</label>
              <input
                className="mono"
                autoFocus
                list="ex-dash-list"
                placeholder="revenue"
                value={dashName}
                onChange={(e) => setDashName(e.target.value)}
                disabled={!!editing}
              />
              <datalist id="ex-dash-list">
                {(dashboardsQ.data ?? []).map((d) => (
                  <option key={d.name} value={d.name}>{d.title || d.name}</option>
                ))}
              </datalist>
              {!editing && (
                <div className="hint">
                  Lowercase letters, digits, _ and -. Type a new name to create a
                  dashboard.
                </div>
              )}
            </div>
            <div className="field">
              <label>Panel title</label>
              <input
                value={panelTitle}
                onChange={(e) => setPanelTitle(e.target.value)}
                placeholder="Revenue by region"
              />
            </div>
            <div className="field">
              <label>Width (1–12)</label>
              <input
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
                disabled={save.isPending || !/^[a-z][a-z0-9_-]{0,63}$/.test(dashName.trim())}
                onClick={() => save.mutate()}
              >
                {save.isPending ? "Saving…" : editing ? "Save changes" : "Save panel"}
              </button>
            </div>
          </div>
        </div>
      )}

      <style>{EXPLORE_STYLES}</style>
    </div>
  );
}

// --------------------------------------------------------- dataset shaping

function ColumnOptions({
  columns,
  masked,
}: {
  columns: string[];
  masked?: Set<string>;
}) {
  return (
    <>
      {columns.map((c) =>
        masked?.has(c) ? (
          <option key={c} value={c} disabled title="Masked for you — you would only ever summarise '***'.">
            {c} (masked for you)
          </option>
        ) : (
          <option key={c} value={c}>{c}</option>
        ),
      )}
    </>
  );
}

/** Distinct values of a text column, served by the same governed preview
 *  path as every other Explore query — so the suggestions are exactly the
 *  values the caller's own policy lets them see. */
function ValueSuggestions({ dataset, column, id }: { dataset: string; column: string; id: string }) {
  const auth = useAuth();
  const q = useQuery({
    queryKey: ["explore-values", dataset, column],
    queryFn: ({ signal }) =>
      api.post<ExplorePreviewResult>(
        `${API}/explore/preview`,
        { flow: distinctValuesFlow(dataset, column, auth.user?.username ?? "explore"), max_rows: 50 },
        signal,
      ),
    staleTime: 300_000,
    enabled: !!dataset && !!column,
  });
  return (
    <datalist id={id}>
      {(q.data?.rows ?? []).map((r, i) => (
        <option key={i} value={String(r[column] ?? "")} />
      ))}
    </datalist>
  );
}

function DatasetShaping({
  state,
  setState,
  dataset,
  columns,
  kinds,
  masked,
  schemaError,
}: {
  state: ExploreState;
  setState: (fn: (s: ExploreState) => ExploreState) => void;
  dataset: string;
  columns: string[];
  kinds: Record<string, FlowKind>;
  masked: Set<string>;
  schemaError: unknown;
}) {
  const numericColumns = columns.filter((c) => !kinds[c] || kinds[c] === "number");
  const set = setState;

  if (schemaError != null) return <ErrorBox error={schemaError} />;

  // Text columns whose filters could use value suggestions, one datalist each.
  const suggestColumns = [
    ...new Set(
      state.filters
        .filter((f) => f.column && (kinds[f.column] ?? "text") === "text")
        .map((f) => f.column),
    ),
  ];

  return (
    <>
      {/* ------------------------------------------------------------ filter */}
      <div className="ex-card">
        <div className="ex-card-title">Filter</div>
        {state.filters.map((f, i) => {
          const kind = kinds[f.column] ?? "";
          const needsValue = f.op !== "is_null" && f.op !== "is_not_null";
          const isList = f.op === "in" || f.op === "not_in";
          return (
            <div key={i} className="ex-row">
              <select
                value={f.column}
                onChange={(e) =>
                  set((s) => ({
                    ...s,
                    filters: s.filters.map((x, j) => (j === i ? { ...x, column: e.target.value } : x)),
                  }))
                }
              >
                <option value="">Pick a column…</option>
                <ColumnOptions columns={columns} />
              </select>
              <select
                value={f.op}
                onChange={(e) =>
                  set((s) => ({
                    ...s,
                    filters: s.filters.map((x, j) =>
                      j === i ? { ...x, op: e.target.value as ExploreFilterOp } : x,
                    ),
                  }))
                }
              >
                {(Object.keys(FILTER_OPS) as ExploreFilterOp[]).map((op) => (
                  <option key={op} value={op}>{FILTER_OPS[op]}</option>
                ))}
              </select>
              {needsValue && !isList && (
                <input
                  value={f.value}
                  // Suggest the column's real values: without them the analyst
                  // must already know exact spelling and casing, and a typo
                  // silently matches nothing.
                  list={
                    kind !== "number" && kind !== "time" && kind !== "boolean" && f.column
                      ? `ex-vals-${f.column}`
                      : undefined
                  }
                  placeholder={
                    kind === "time" ? "YYYY-MM-DD" : kind === "number" ? "e.g. 100" : kind === "boolean" ? "true / false" : "value"
                  }
                  onChange={(e) =>
                    set((s) => ({
                      ...s,
                      filters: s.filters.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)),
                    }))
                  }
                />
              )}
              {isList && (
                <input
                  value={f.values.join(", ")}
                  placeholder="value, value, value"
                  onChange={(e) =>
                    set((s) => ({
                      ...s,
                      filters: s.filters.map((x, j) =>
                        j === i
                          ? { ...x, values: e.target.value.split(",").map((v) => v.trim()).filter(Boolean) }
                          : x,
                      ),
                    }))
                  }
                />
              )}
              <button
                className="ex-x"
                title="Remove this filter"
                onClick={() => set((s) => ({ ...s, filters: s.filters.filter((_, j) => j !== i) }))}
              >
                ×
              </button>
            </div>
          );
        })}
        <button
          className="ex-add"
          onClick={() =>
            set((s) => ({
              ...s,
              filters: [...s.filters, { column: "", op: "eq", value: "", values: [] } as ExploreFilter],
            }))
          }
        >
          + keep only rows where…
        </button>
        {suggestColumns.map((c) => (
          <ValueSuggestions key={c} dataset={dataset} column={c} id={`ex-vals-${c}`} />
        ))}
      </div>

      {/* ---------------------------------------------------------- group by */}
      <div className="ex-card">
        <div className="ex-card-title">Group by</div>
        {state.groups.map((g, i) => {
          const kind = kinds[g.column] ?? "";
          // A column already grouped plain must not be offered again: the
          // duplicate compiles to "GROUP BY x, x", which the server refuses
          // in step vocabulary the analyst has never seen.
          const usedPlain = new Set(
            state.groups
              .filter((x, j) => j !== i && x.column && !x.bucket)
              .map((x) => x.column),
          );
          return (
            <div key={i} className="ex-row">
              <select
                value={g.column}
                onChange={(e) => {
                  const column = e.target.value;
                  set((s) => ({
                    ...s,
                    groups: s.groups.map((x, j) =>
                      // Buckets belong to a column's kind; reset on change.
                      j === i ? { column, bucket: "", binWidth: "", parse: false } : x,
                    ),
                    // Grouped-and-unordered charts in whatever order the
                    // engine returns — plausible-looking noise. Default every
                    // fresh grouping to its own ascending order, visibly, in
                    // the Order card where the author can change it.
                    sort: column && !s.sort ? { column, dir: "asc" as const } : s.sort,
                  }));
                }}
              >
                <option value="">Pick a column…</option>
                <ColumnOptions columns={columns.filter((c) => c === g.column || !usedPlain.has(c))} />
              </select>
              {(kind === "time" || kind === "text") && (
                <select
                  value={g.bucket === "bin" ? "" : g.bucket}
                  onChange={(e) => {
                    const bucket = e.target.value as ExploreBucket;
                    // A text column bucketed by date needs reading as one
                    // first — the compiler's own cast step, synthesized for
                    // the analyst. Datasets constantly arrive with ISO
                    // timestamps typed as text, and without this the entire
                    // class of time-series questions was silently impossible.
                    const parse = kind === "text" && !!bucket;
                    set((s) => {
                      const groups = s.groups.map((x, j) =>
                        j === i ? { ...x, bucket, parse } : x,
                      );
                      // A time series nobody ordered charts in whatever order
                      // the engine grouped it. Default to chronological the
                      // moment a date bucket is chosen — visibly, in the sort
                      // card, where the author can change it — unless the
                      // author's own sort still names a real result column.
                      const keep =
                        s.sort && resultColumns({ ...s, groups }).includes(s.sort.column);
                      const sort =
                        bucket && !keep
                          ? { column: groupResultName(groups[i], new Set<string>()), dir: "asc" as const }
                          : s.sort;
                      return { ...s, groups, sort };
                    });
                  }}
                >
                  <option value="">exact values</option>
                  {Object.entries(DATE_BUCKETS).map(([k, v]) => (
                    <option key={k} value={k}>
                      {kind === "text" ? `read as dates, by ${v}` : `by ${v}`}
                    </option>
                  ))}
                </select>
              )}
              {kind === "number" && (
                <>
                  <select
                    value={g.bucket === "bin" ? "bin" : ""}
                    onChange={(e) => {
                      const bin = e.target.value === "bin";
                      set((s) => {
                        const groups = s.groups.map((x, j) =>
                          j === i
                            ? {
                                ...x,
                                bucket: (bin ? "bin" : "") as ExploreBucket,
                                binWidth: bin ? x.binWidth || "10" : "",
                                parse: false,
                              }
                            : x,
                        );
                        // Same rule as the date buckets: a histogram whose
                        // bins render in arbitrary order is a shuffled
                        // distribution that looks like a valid chart.
                        const keep =
                          s.sort && resultColumns({ ...s, groups }).includes(s.sort.column);
                        const sort =
                          bin && !keep
                            ? { column: groupResultName(groups[i], new Set<string>()), dir: "asc" as const }
                            : s.sort;
                        return { ...s, groups, sort };
                      });
                    }}
                  >
                    <option value="">exact values</option>
                    <option value="bin">in ranges of…</option>
                  </select>
                  {g.bucket === "bin" && (
                    <input
                      style={{ width: 76 }}
                      value={g.binWidth}
                      placeholder="10"
                      onChange={(e) =>
                        set((s) => ({
                          ...s,
                          groups: s.groups.map((x, j) => (j === i ? { ...x, binWidth: e.target.value } : x)),
                        }))
                      }
                    />
                  )}
                </>
              )}
              <button
                className="ex-x"
                title="Remove this grouping"
                onClick={() => set((s) => ({ ...s, groups: s.groups.filter((_, j) => j !== i) }))}
              >
                ×
              </button>
            </div>
          );
        })}
        <button
          className="ex-add"
          onClick={() =>
            set((s) => ({
              ...s,
              groups: [...s.groups, { column: "", bucket: "", binWidth: "", parse: false }],
            }))
          }
        >
          + one row per…
        </button>
        {state.groups.length === 0 && (
          <div className="hint">No grouping = one summary row over everything.</div>
        )}
      </div>

      {/* ---------------------------------------------------------- measures */}
      <div className="ex-card">
        <div className="ex-card-title">Summarise</div>
        {state.measures.map((m, i) => (
          <div key={i} className="ex-row">
            <select
              value={m.fn}
              onChange={(e) => {
                const fn = e.target.value as FlowAggFn;
                set((s) => ({
                  ...s,
                  measures: s.measures.map((x, j) =>
                    j === i
                      ? {
                          fn,
                          column: fn === "count_star" ? "" : x.column,
                          // Follow the default alias unless the author renamed
                          // it. An empty alias follows too: a summary that
                          // starts blank should self-name like the first one
                          // does, not block preview with "give it a name".
                          alias:
                            x.alias === defaultAlias(x.fn, x.column) || x.alias.trim() === ""
                              ? defaultAlias(fn, fn === "count_star" ? "" : x.column)
                              : x.alias,
                        }
                      : x,
                  ),
                }));
              }}
            >
              {Object.entries(MEASURE_FNS).map(([fn, label]) => (
                <option key={fn} value={fn}>{label}</option>
              ))}
            </select>
            {m.fn !== "count_star" && (
              <>
                <span className="ex-kw">of</span>
                <select
                  value={m.column}
                  onChange={(e) => {
                    const column = e.target.value;
                    set((s) => ({
                      ...s,
                      measures: s.measures.map((x, j) =>
                        j === i
                          ? {
                              ...x,
                              column,
                              alias:
                                x.alias === defaultAlias(x.fn, x.column) || x.alias.trim() === ""
                                  ? defaultAlias(x.fn, column)
                                  : x.alias,
                            }
                          : x,
                      ),
                    }));
                  }}
                >
                  <option value="">Pick a column…</option>
                  <ColumnOptions
                    columns={NUMERIC_FNS.has(m.fn) ? numericColumns : columns}
                    masked={masked}
                  />
                </select>
              </>
            )}
            <span className="ex-kw">called</span>
            <input
              style={{ width: 130 }}
              value={m.alias}
              onChange={(e) =>
                set((s) => ({
                  ...s,
                  measures: s.measures.map((x, j) => (j === i ? { ...x, alias: e.target.value } : x)),
                }))
              }
            />
            <button
              className="ex-x"
              disabled={state.measures.length === 1}
              title={state.measures.length === 1 ? "a chart needs at least one summary" : "Remove"}
              onClick={() => set((s) => ({ ...s, measures: s.measures.filter((_, j) => j !== i) }))}
            >
              ×
            </button>
          </div>
        ))}
        <button
          className="ex-add"
          onClick={() =>
            set((s) => ({
              ...s,
              // Born with the default alias so it self-names as the pickers
              // change, exactly like the first summary — a blank one used to
              // block preview until the analyst typed a name by hand.
              measures: [...s.measures, { fn: "sum", column: "", alias: defaultAlias("sum", "") }],
            }))
          }
        >
          + another summary
        </button>
      </div>

      {/* ------------------------------------------------------- sort + top */}
      <div className="ex-card">
        <div className="ex-card-title">Order and Top N</div>
        <div className="ex-row">
          <select
            // A sort whose column the result no longer produces reads as "no
            // particular order", matching the synthesis (which drops it).
            value={
              state.sort && resultColumns(state).includes(state.sort.column)
                ? state.sort.column
                : ""
            }
            onChange={(e) => {
              const column = e.target.value;
              set((s) => {
                if (!column) return { ...s, sort: null };
                // Direction defaults by what the column *is*: a grouping
                // ascends (a time column sorted "largest first" is a time
                // series running backwards), a measure descends (biggest
                // first is what "sort by the count" means).
                const isMeasure = s.measures.some((m) => m.alias === column);
                return { ...s, sort: { column, dir: isMeasure ? "desc" : "asc" } };
              });
            }}
          >
            <option value="">No particular order</option>
            {resultColumns(state).map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
          {state.sort && (
            <select
              value={state.sort.dir}
              onChange={(e) =>
                set((s) => ({ ...s, sort: s.sort ? { ...s.sort, dir: e.target.value as "asc" | "desc" } : null }))
              }
            >
              {/* Time columns get time words: nobody maps "smallest first"
                  to "oldest first" without stopping to think. */}
              {resultColumnKind(state, kinds, state.sort.column) === "time" ? (
                <>
                  <option value="asc">oldest first</option>
                  <option value="desc">newest first</option>
                </>
              ) : (
                <>
                  <option value="asc">smallest first</option>
                  <option value="desc">largest first</option>
                </>
              )}
            </select>
          )}
        </div>
        <div className="ex-row">
          <span className="ex-kw">keep the top</span>
          <input
            style={{ width: 76 }}
            value={state.top}
            placeholder="all"
            onChange={(e) => set((s) => ({ ...s, top: e.target.value }))}
          />
          <span className="ex-kw">rows</span>
        </div>
        {state.top.trim() !== "" && !state.sort && (
          <div className="hint">
            Top N without an order keeps an <em>arbitrary</em> N — pick an order above to
            make it “the biggest N”.
          </div>
        )}
      </div>
    </>
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
        <div className="ex-card-title">Narrow the objects</div>
        {obj.filters.map((f, i) => (
          <div key={i} className="ex-row">
            <select
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
          + only objects where…
        </button>
        <div className="ex-row" style={{ marginTop: 6 }}>
          <span className="ex-kw">search</span>
          <input
            value={obj.search}
            placeholder="free-text search over the objects"
            onChange={(e) => set((s) => ({ ...s, search: e.target.value }))}
          />
        </div>
      </div>

      <div className="ex-card">
        <div className="ex-card-title">Group by</div>
        <div className="ex-checklist">
          {properties.map((p) => {
            const isMasked = maskedProps.has(p);
            return (
              <label
                key={p}
                className="check-inline"
                title={
                  isMasked
                    ? "Masked for you — every object would land in one “***” group."
                    : undefined
                }
                style={isMasked ? { opacity: 0.55 } : undefined}
              >
                <input
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
      </div>

      <div className="ex-card">
        <div className="ex-card-title">Summarise</div>
        {obj.metrics.map((m, i) => (
          <div key={i} className="ex-row">
            <select
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
          + another summary
        </button>
      </div>
    </>
  );
}

// ------------------------------------------------------------ bindings row

function BindingsRow({
  bindings,
  setBindings,
  columns,
  numericCols,
  categoricalCols,
}: {
  bindings: Bindings;
  setBindings: (fn: (b: Bindings) => Bindings) => void;
  columns: string[];
  numericCols: string[];
  categoricalCols: string[];
}) {
  const xOptions = bindings.chart === "scatter" ? numericCols : columns;
  return (
    <div className="ex-bindings">
      <label>
        x
        <select
          value={bindings.x}
          onChange={(e) => setBindings((b) => ({ ...b, x: e.target.value }))}
        >
          <option value="">(auto)</option>
          {xOptions.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
      </label>
      <label>
        y
        <span className="ex-ychecks">
          {numericCols.length === 0 && <span className="faint">no number columns</span>}
          {numericCols.map((c) => (
            <label key={c} className="check-inline">
              <input
                type="checkbox"
                checked={bindings.y.includes(c)}
                onChange={(e) =>
                  setBindings((b) => ({
                    ...b,
                    y: e.target.checked
                      ? [...b.y.filter((x) => x !== c), c]
                      : b.y.filter((x) => x !== c),
                  }))
                }
              />
              <span className="mono" style={{ fontSize: 11.5 }}>{c}</span>
            </label>
          ))}
          {bindings.y.length === 0 && numericCols.length > 0 && (
            <span className="faint">(all)</span>
          )}
        </span>
      </label>
      {bindings.chart !== "pie" && bindings.chart !== "scatter" && (
        <label>
          split by
          <select
            value={bindings.series}
            onChange={(e) => setBindings((b) => ({ ...b, series: e.target.value }))}
          >
            <option value="">(none)</option>
            {categoricalCols.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
          {/* Split-by can only offer columns the result carries. That is a
              long-vs-wide fact no analyst should need to know, so say the
              road: the column must be grouped first. */}
          {!bindings.series && (
            <span className="faint" style={{ fontSize: 11 }}>
              — to split by a column, add it under Group&nbsp;by first
            </span>
          )}
        </label>
      )}
      {bindings.chart === "bar" && (
        <label className="check-inline">
          <input
            type="checkbox"
            checked={bindings.stacked}
            onChange={(e) => setBindings((b) => ({ ...b, stacked: e.target.checked }))}
          />
          <span>stacked</span>
        </label>
      )}
    </div>
  );
}

// ------------------------------------------------------------------- styles

const EXPLORE_STYLES = `
.ex-page { display: flex; flex-direction: column; gap: 12px; }
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
.ex-shape { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.ex-card {
  background: var(--bg-1); border: 1px solid var(--border);
  border-radius: 10px; padding: 12px 14px;
}
.ex-card-title {
  font-size: 10.5px; letter-spacing: 0.12em; text-transform: uppercase;
  color: var(--text-faint); margin-bottom: 8px;
}
.ex-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
.ex-row select, .ex-row input {
  background: var(--bg-2); color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; padding: 4px 7px; font-size: 12.5px; min-width: 0;
}
.ex-row input { flex: 1 1 90px; }
.ex-row select { max-width: 200px; }
.ex-kw { color: var(--text-faint); font-size: 12px; }
.ex-x {
  background: transparent; border: none; color: var(--text-faint);
  cursor: pointer; font-size: 14px; padding: 2px 6px; border-radius: 5px;
}
.ex-x:hover:not(:disabled) { color: var(--red); background: var(--bg-2); }
.ex-add {
  background: transparent; border: 1px dashed var(--border-2); color: var(--text-dim);
  border-radius: 6px; padding: 4px 10px; font-size: 12px; cursor: pointer; margin-top: 2px;
}
.ex-add:hover { color: var(--gold); border-color: var(--gold-dim); }
.ex-checklist { display: flex; flex-direction: column; gap: 3px; max-height: 180px; overflow-y: auto; }
.ex-chart { min-width: 0; }
.ex-kinds { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 8px; }
.ex-bindings {
  display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
  font-size: 11.5px; color: var(--text-faint); margin-bottom: 4px;
}
.ex-bindings > label { display: inline-flex; align-items: center; gap: 6px; }
.ex-bindings select {
  background: var(--bg-2); color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; padding: 3px 6px; font-size: 12px;
}
.ex-ychecks { display: inline-flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.ex-truncated {
  font-size: 11.5px; color: var(--gold); background: var(--gold-tint-bg);
  border: 1px solid var(--gold-dim); border-radius: 6px; padding: 5px 9px; margin-bottom: 8px;
}
`;
