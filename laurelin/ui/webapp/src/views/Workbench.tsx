// The SQL page (file keeps its historical code name) — run read-only DuckDB
// queries over the workspace datasets.
// Each dataset is exposed as a view named after the dataset.

import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useMutation } from "@tanstack/react-query";
import { EditorView, keymap } from "@codemirror/view";
import { EditorState } from "@codemirror/state";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { sql } from "@codemirror/lang-sql";
import { oneDark } from "@codemirror/theme-one-dark";
import { api, API, ApiError } from "../api";
import type { Dashboard, Dataset, QueryResult, PipelineWriteResult } from "../types";
import type { ChartKind } from "../charts";
import { Chart } from "../charts";
import {
  ErrorBox,
  FailureNote,
  LiveStatus,
  Modal,
  NAME_RULE,
  NAME_RULE_NO_HYPHEN,
  PageHeader,
  Spinner,
  WarningBox,
  fmtValue,
  truncationNote,
} from "../ui";
import { useAuth } from "../auth";

const RESULT_VIEWS: ChartKind[] = ["table", "bar", "line", "area", "stat", "pie", "scatter"];

const MAX_ROWS = 1000;
const PLACEHOLDER = "-- Write SQL over your datasets. Ctrl+Enter to run.\n";
const NAME_RE = /^[a-z][a-z0-9_]*$/;

// ------------------------------------------------------------------ draft
//
// Authored text survives navigation (rule L4): the editor doc, the chosen
// result view and the last result persist in sessionStorage — sessionStorage
// deliberately, not localStorage, for the same shared-machine-leakage reason
// as the quick-chart draft (a query's text can embed filter values).

export const WORKBENCH_DRAFT_KEY = "laurelin.workbench.draft.v1";

// A stored result larger than this is dropped rather than persisted: a
// 1000-row result can approach the sessionStorage quota, and losing the
// cached ROWS on navigation is a shrug — losing the authored SQL is not.
const MAX_PERSISTED_RESULT_BYTES = 900_000;

export interface WorkbenchDraft {
  sql: string;
  view?: ChartKind;
  result?: QueryResult;
}

/** Tolerant parser: any unexpected shape degrades to null, never a crash. */
export function parseWorkbenchDraft(raw: string | null): WorkbenchDraft | null {
  if (!raw) return null;
  try {
    const d = JSON.parse(raw) as Record<string, unknown>;
    if (!d || typeof d !== "object" || typeof d.sql !== "string") return null;
    const out: WorkbenchDraft = { sql: d.sql };
    if (typeof d.view === "string" && RESULT_VIEWS.includes(d.view as ChartKind)) {
      out.view = d.view as ChartKind;
    }
    const r = d.result as QueryResult | undefined;
    if (
      r &&
      typeof r === "object" &&
      Array.isArray(r.columns) &&
      Array.isArray(r.rows) &&
      typeof r.row_count === "number"
    ) {
      out.result = r;
    }
    return out;
  } catch {
    return null;
  }
}

function readDraft(): WorkbenchDraft | null {
  try {
    return parseWorkbenchDraft(sessionStorage.getItem(WORKBENCH_DRAFT_KEY));
  } catch {
    return null;
  }
}

function writeDraft(draft: WorkbenchDraft): void {
  try {
    let payload = JSON.stringify(draft);
    if (draft.result && payload.length > MAX_PERSISTED_RESULT_BYTES) {
      payload = JSON.stringify({ sql: draft.sql, view: draft.view });
    }
    sessionStorage.setItem(WORKBENCH_DRAFT_KEY, payload);
  } catch {
    // Quota or privacy mode: the draft is a courtesy, never a requirement.
  }
}

/**
 * True when the text contains something DuckDB could execute. The pristine
 * editor holds only the placeholder comment, and running it used to travel
 * all the way to the engine and come back as a classified failure blaming a
 * "remote system" (spec F9). A comment is refused here, before the POST,
 * with a sentence about the actual situation.
 */
export function sqlHasExecutableStatement(text: string): boolean {
  const stripped = text
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/--[^\n]*/g, " ");
  return /[^\s;]/.test(stripped);
}

export const COMMENT_ONLY_REFUSAL =
  "Nothing to run — the editor only contains a comment.";

function saveErrorMessage(err: unknown, fileName: string, pipelinesLocked: boolean): string {
  if (err instanceof ApiError) {
    if (err.status === 409) {
      return `A pipeline file named ${fileName} already exists.`;
    }
    if (err.status === 403) {
      // Two different diagnoses used to share one hedged sentence ("editor
      // role required, or pipeline editing is locked"), which sent authors
      // in the wrong direction: a role denial is fixed by an administrator,
      // a lock only by a server restart. /auth/status now says which.
      return pipelinesLocked
        ? "Python authoring is locked on this server (--lock-pipelines), so a query cannot " +
            "be saved as a pipeline file here. Visual pipelines and the quick chart remain available."
        : "You don't have permission to create pipelines (editor role required).";
    }
    if (err.status === 400) {
      return err.detail || "Invalid request.";
    }
    return err.detail || "Failed to save the pipeline.";
  }
  return "Failed to save the pipeline.";
}

export function WorkbenchView() {
  const auth = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const runRef = useRef<() => void>(() => {});
  // Set once the editor is mounted, so sidebar-click handlers can update the doc.
  const [ready, setReady] = useState(false);

  // The draft is read once per mount; every later change writes through.
  const draft = useMemo(() => readDraft(), []);

  // "Save as pipeline" form state.
  const [saveOpen, setSaveOpen] = useState(false);
  const [outName, setOutName] = useState("");
  const [fileName, setFileName] = useState("");
  const [nameErr, setNameErr] = useState<string | null>(null);
  const [saved, setSaved] = useState<PipelineWriteResult | null>(null);

  // Result presentation + "Add to dashboard" state.
  const [view, setView] = useState<ChartKind>(draft?.view ?? "table");
  const [result, setResult] = useState<QueryResult | null>(draft?.result ?? null);
  const [dashOpen, setDashOpen] = useState(false);
  const [dashName, setDashName] = useState("");
  const [panelTitle, setPanelTitle] = useState("");
  // Chart bindings for the saved panel, offered from the just-run result's
  // columns — the panel used to be saved with x:"", y:[] unconditionally,
  // leaving every workbench panel on inference forever.
  const [panelX, setPanelX] = useState("");
  const [panelY, setPanelY] = useState<string[]>([]);
  const [dashDone, setDashDone] = useState<string | null>(null);

  // Client-side refusal (comment-only run) and cancellation state.
  const [refusal, setRefusal] = useState<string | null>(null);
  const [cancelled, setCancelled] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const persistTimer = useRef<number | null>(null);

  const dashboardsQ = useQuery({
    queryKey: ["dashboards"],
    queryFn: () => api.get<Dashboard[]>(`${API}/dashboards`),
    enabled: dashOpen,
  });

  const addToDash = useMutation({
    mutationFn: async () => {
      const name = dashName.trim();
      const sqlText = viewRef.current?.state.doc.toString() ?? "";
      const existing = (dashboardsQ.data ?? []).find((d) => d.name === name);
      const panel = {
        id: Math.random().toString(36).slice(2, 10),
        title: panelTitle.trim(),
        sql: sqlText,
        chart: view === "table" ? ("table" as const) : view,
        x: panelX,
        y: panelY,
        width: 6,
      };
      // Append through the per-panel route instead of re-PUTting the board.
      // The old shape sent back every panel from the *listing* — which under R2
      // may be a projection with no `sql` — and would have blanked the queries
      // of every other panel on the board the moment an editor's session was
      // anything less than an editor's session.
      if (!existing) {
        await api.put<Dashboard>(`${API}/dashboards/${encodeURIComponent(name)}`, {
          title: name,
          description: "",
          panels: [],
        });
      }
      return api.post<Dashboard>(
        `${API}/dashboards/${encodeURIComponent(name)}/panels`,
        panel,
      );
    },
    onSuccess: (d) => {
      setDashOpen(false);
      setDashDone(d.name);
    },
  });

  const saveMut = useMutation<
    PipelineWriteResult,
    unknown,
    { sql: string; output: string; name?: string }
  >({
    mutationFn: (body) =>
      api.post<PipelineWriteResult>(`${API}/pipelines/from-query`, body),
    onSuccess: (data) => {
      setSaved(data);
      setSaveOpen(false);
    },
  });

  function openSaveForm() {
    setOutName("");
    setFileName("");
    setNameErr(null);
    saveMut.reset();
    setSaved(null);
    setSaveOpen(true);
  }

  function submitSave() {
    const output = outName.trim();
    if (!NAME_RE.test(output)) {
      setNameErr(NAME_RULE_NO_HYPHEN);
      return;
    }
    setNameErr(null);
    const file = fileName.trim();
    const sqlText = viewRef.current?.state.doc.toString() ?? "";
    saveMut.mutate({ sql: sqlText, output, ...(file ? { name: file } : {}) });
  }

  const datasetsQ = useQuery({
    queryKey: ["datasets"],
    queryFn: () => api.get<Dataset[]>(`${API}/datasets`),
  });

  const persistNow = (patch?: Partial<WorkbenchDraft>) => {
    writeDraft({
      sql: viewRef.current?.state.doc.toString() ?? "",
      view,
      result: result ?? undefined,
      ...patch,
    });
  };
  // Keep the persist closure fresh for the debounced editor listener.
  const persistRef = useRef(persistNow);
  persistRef.current = persistNow;

  const runMut = useMutation<QueryResult, unknown, string>({
    mutationFn: (sqlText: string) => {
      const ctl = new AbortController();
      abortRef.current = ctl;
      return api.post<QueryResult>(
        `${API}/query`,
        { sql: sqlText, max_rows: MAX_ROWS },
        ctl.signal,
      );
    },
    onSuccess: (data) => {
      setResult(data);
      persistRef.current({ result: data });
    },
    onSettled: () => {
      abortRef.current = null;
    },
  });

  // Keep the run callback fresh so the Ctrl-Enter keymap never calls a stale closure.
  runRef.current = () => {
    const text = viewRef.current?.state.doc.toString() ?? "";
    setCancelled(false);
    if (!text.trim()) return;
    if (!sqlHasExecutableStatement(text)) {
      // Refused before the POST: the gray placeholder is a hint, not a query.
      setRefusal(COMMENT_ONLY_REFUSAL);
      return;
    }
    setRefusal(null);
    runMut.mutate(text);
  };

  // Cancel frees the admission slot the running query holds (the server caps
  // concurrent queries process-wide) instead of leaving it pinned until the
  // statement finishes for a reader who has already given up on it.
  function cancelRun() {
    abortRef.current?.abort();
    abortRef.current = null;
    setCancelled(true);
    runMut.reset();
  }

  // `?dataset=` is the dataset detail page's "Open in SQL" door: seed the
  // editor with the same starter query the sidebar buttons insert, so the
  // caller lands one keystroke (Ctrl+Enter) from rows. It outranks the
  // resumed draft — the caller named what they want to look at.
  const presetDataset = searchParams.get("dataset") ?? "";

  // Mount the editor exactly once.
  useEffect(() => {
    if (!hostRef.current) return;
    const view = new EditorView({
      parent: hostRef.current,
      state: EditorState.create({
        doc: presetDataset
          ? `SELECT * FROM ${presetDataset} LIMIT 100`
          : draft?.sql || PLACEHOLDER,
        extensions: [
          history(),
          sql(),
          oneDark,
          keymap.of([
            {
              key: "Mod-Enter",
              preventDefault: true,
              run: () => {
                runRef.current();
                return true;
              },
            },
            ...defaultKeymap,
            ...historyKeymap,
          ]),
          EditorView.updateListener.of((u) => {
            if (!u.docChanged) return;
            if (persistTimer.current !== null) {
              window.clearTimeout(persistTimer.current);
            }
            persistTimer.current = window.setTimeout(() => {
              persistTimer.current = null;
              persistRef.current();
            }, 400);
          }),
        ],
      }),
    });
    viewRef.current = view;
    setReady(true);
    return () => {
      if (persistTimer.current !== null) {
        window.clearTimeout(persistTimer.current);
        persistTimer.current = null;
      }
      view.destroy();
      viewRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The chosen result view is part of the draft too.
  useEffect(() => {
    if (ready) persistRef.current();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view]);

  function setDoc(text: string) {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: text },
    });
    view.focus();
  }

  function saveAsAnalysisCell() {
    const sqlText = viewRef.current?.state.doc.toString() ?? "";
    persistRef.current();
    navigate(`/analyses?mode=doc&sql=${encodeURIComponent(sqlText)}`);
  }

  const datasets = datasetsQ.data ?? [];

  return (
    <div>
      <PageHeader
        title="SQL"
        subtitle="Query your datasets with read-only DuckDB. Each dataset is a view."
      />

      <div className="wb-layout">
        <aside className="wb-sidebar">
          <div className="wb-ds-head faint">Datasets</div>
          {datasetsQ.isLoading ? (
            <Spinner />
          ) : datasetsQ.error ? (
            <ErrorBox error={datasetsQ.error} onRetry={() => datasetsQ.refetch()} />
          ) : datasets.length === 0 ? (
            <div className="dim" style={{ fontSize: 12.5, padding: "8px 0" }}>
              No datasets yet — <Link to="/datasets">add one on the Datasets page</Link>.
            </div>
          ) : (
            <ul className="wb-ds-list">
              {datasets.map((d) => (
                <li key={d.name}>
                  <button
                    type="button"
                    className="wb-ds-item mono"
                    title={`SELECT * FROM ${d.name} LIMIT 100`}
                    disabled={!ready}
                    onClick={() => setDoc(`SELECT * FROM ${d.name} LIMIT 100`)}
                  >
                    {d.name}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>

        <main className="wb-main">
          <div className="cm-host" ref={hostRef} />

          <div className="toolbar" style={{ marginTop: 12 }}>
            <button
              type="button"
              className="primary"
              disabled={!ready || runMut.isPending}
              onClick={() => runRef.current()}
            >
              {runMut.isPending ? "Running…" : "Run"}
            </button>
            {runMut.isPending && (
              <button type="button" onClick={cancelRun}>
                Cancel
              </button>
            )}
            <span className="faint" style={{ fontSize: 12 }}>
              ⌘/Ctrl+Enter
            </span>
          </div>

          {refusal && !runMut.isPending && (
            <div className="warn-box" style={{ marginTop: 10 }}>
              {refusal}
            </div>
          )}

          {runMut.isPending && <Spinner label="Running query…" />}

          {cancelled && !runMut.isPending && (
            <div className="dim" style={{ fontSize: 12.5, marginTop: 10 }}>
              Query cancelled.
            </div>
          )}

          {runMut.error != null && !runMut.isPending && !cancelled && (
            <ErrorBox error={runMut.error} />
          )}

          {result && !runMut.isPending && (
            <div>
              <div
                className="result-meta"
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  flexWrap: "wrap",
                }}
              >
                {/* The row count is the async outcome a screen reader must
                    hear without re-reading the page (rule A6). */}
                <LiveStatus style={{ display: "inline" }}>
                  {result.row_count.toLocaleString("en-US")} rows
                  {result.truncated ? ` · ${truncationNote(MAX_ROWS)}` : ""}
                </LiveStatus>
                <span className="wb-viewtabs">
                  {RESULT_VIEWS.map((k) => (
                    <button
                      key={k}
                      type="button"
                      className={`small${view === k ? " primary" : ""}`}
                      onClick={() => setView(k)}
                    >
                      {k}
                    </button>
                  ))}
                </span>
                {auth.can("editor") && (
                  <>
                    <button
                      type="button"
                      className="button small"
                      onClick={openSaveForm}
                      disabled={auth.pipelinesLocked}
                      title={
                        auth.pipelinesLocked
                          ? "Python authoring is locked on this server (--lock-pipelines); saving a query writes a pipeline file."
                          : undefined
                      }
                    >
                      Save as pipeline
                    </button>
                    <button
                      type="button"
                      className="button small"
                      onClick={() => {
                        setDashDone(null);
                        addToDash.reset();
                        setPanelX("");
                        setPanelY([]);
                        setDashOpen(true);
                      }}
                    >
                      Add to dashboard
                    </button>
                    {/* A quick query's lightweight home: an analysis cell is
                        already SQL, so the handoff is a navigation, not a new
                        server surface. */}
                    <button
                      type="button"
                      className="button small"
                      onClick={saveAsAnalysisCell}
                    >
                      Save as analysis cell
                    </button>
                  </>
                )}
              </div>

              {dashDone && (
                <div style={{ marginBottom: 10, fontSize: 12.5, color: "var(--green)" }}>
                  Panel added —{" "}
                  <Link to={`/dashboards/${dashDone}`}>open dashboard '{dashDone}'.</Link>
                </div>
              )}
              {/* The save already happened: this is an authoring hint, not a
                  refusal. It used to be a 400 that lost the editor their work. */}
              <WarningBox warnings={addToDash.data?.warnings} />

              {saved && (
                <div
                  style={{
                    marginBottom: 10,
                    fontSize: 12.5,
                    color: "var(--green)",
                  }}
                >
                  Created pipeline file '{saved.name}.py' —{" "}
                  <Link to="/builds">build it from the Builds page.</Link>
                  {/* The file was written; the DAG just does not collect.
                      R1 turned this from the collector's own sentence into a
                      classified record, so it renders like every other
                      failure rather than as a bare string. */}
                  {saved.collect_error ? (
                    <FailureNote failure={saved.collect_error} role={auth.role} />
                  ) : null}
                </div>
              )}
              {result.columns.length === 0 ? (
                <div className="dim" style={{ fontSize: 13, padding: "8px 0" }}>
                  No result set.
                </div>
              ) : view !== "table" ? (
                <div className="card" style={{ maxWidth: 760 }}>
                  <Chart data={result} kind={view} />
                </div>
              ) : (
                // Capped height: a 1000-row result must scroll inside its own
                // pane, not turn the page into a 60,000px wall.
                <div className="table-wrap" style={{ maxHeight: 440, overflowY: "auto" }}>
                  <table>
                    <thead>
                      <tr>
                        {result.columns.map((c) => (
                          <th key={c} className="mono">
                            {c}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {result.rows.map((row, i) => (
                        <tr key={i}>
                          {result.columns.map((c) => (
                            <td key={c} className="mono">
                              {fmtValue(row[c])}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {dashOpen && (
            <Modal
              label="Add to dashboard"
              onClose={() => {
                if (!addToDash.isPending) setDashOpen(false);
              }}
            >
              <div className="card-title">Add to dashboard</div>
              <p className="dim" style={{ fontSize: 12.5, marginTop: 4 }}>
                Saves the current query as a{" "}
                <span className="mono">{view === "table" ? "table" : view}</span> panel.
                Type a new name to create a dashboard.
              </p>
              <div className="field" style={{ marginTop: 12 }}>
                <label htmlFor="wb-dash-name">Dashboard</label>
                <input
                  id="wb-dash-name"
                  className="mono"
                  autoFocus
                  list="wb-dash-list"
                  placeholder="revenue"
                  value={dashName}
                  onChange={(e) => setDashName(e.target.value)}
                />
                <datalist id="wb-dash-list">
                  {(dashboardsQ.data ?? []).map((d) => (
                    <option key={d.name} value={d.name}>
                      {d.title || d.name}
                    </option>
                  ))}
                </datalist>
                <div className="hint">{NAME_RULE}</div>
              </div>
              <div className="field">
                <label htmlFor="wb-panel-title">Panel title</label>
                <input
                  id="wb-panel-title"
                  value={panelTitle}
                  onChange={(e) => setPanelTitle(e.target.value)}
                  placeholder="Revenue by region"
                />
              </div>
              {view !== "table" && result && result.columns.length > 0 && (
                <>
                  <div className="field">
                    <label htmlFor="wb-panel-x">X column</label>
                    <select
                      id="wb-panel-x"
                      value={panelX}
                      onChange={(e) => setPanelX(e.target.value)}
                    >
                      <option value="">(infer)</option>
                      {result.columns.map((c) => (
                        <option key={c} value={c}>{c}</option>
                      ))}
                    </select>
                  </div>
                  <div className="field">
                    <label>Y columns (none = all numeric)</label>
                    <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                      {result.columns.map((c) => (
                        <label key={c} className="check-inline">
                          <input
                            type="checkbox"
                            checked={panelY.includes(c)}
                            onChange={(e) =>
                              setPanelY((y) =>
                                e.target.checked
                                  ? [...y.filter((x) => x !== c), c]
                                  : y.filter((x) => x !== c),
                              )
                            }
                          />
                          <span className="mono" style={{ fontSize: 11.5 }}>{c}</span>
                        </label>
                      ))}
                    </div>
                  </div>
                </>
              )}
              {addToDash.error != null && <ErrorBox error={addToDash.error} />}
              {/* No silent disable: the moment the typed name breaks the
                  rule, say the rule. */}
              {dashName.trim() !== "" && !/^[a-z][a-z0-9_-]{0,63}$/.test(dashName.trim()) && (
                <p className="hint">{NAME_RULE}</p>
              )}
              <div className="toolbar" style={{ marginTop: 16, justifyContent: "flex-end" }}>
                <button
                  type="button"
                  disabled={addToDash.isPending}
                  onClick={() => setDashOpen(false)}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="primary"
                  disabled={addToDash.isPending || !/^[a-z][a-z0-9_-]{0,63}$/.test(dashName.trim())}
                  onClick={() => addToDash.mutate()}
                >
                  {addToDash.isPending ? "Adding…" : "Add panel"}
                </button>
              </div>
            </Modal>
          )}

          {saveOpen && (
            <Modal
              label="Save as pipeline"
              onClose={() => {
                if (!saveMut.isPending) setSaveOpen(false);
              }}
            >
              <div className="card-title">Save as pipeline</div>
              <p className="dim" style={{ fontSize: 12.5, marginTop: 4 }}>
                Generate a Python pipeline file from the current query. Build
                it from the Builds page afterward.
              </p>

              <div className="field" style={{ marginTop: 12 }}>
                <label htmlFor="wb-out-name">Output dataset name</label>
                <input
                  id="wb-out-name"
                  className="mono"
                  autoFocus
                  placeholder="e.g. daily_sales"
                  value={outName}
                  onChange={(e) => {
                    setOutName(e.target.value);
                    if (nameErr) setNameErr(null);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") submitSave();
                  }}
                />
                {nameErr ? (
                  <div className="hint bad">{nameErr}</div>
                ) : (
                  <div className="hint">{NAME_RULE_NO_HYPHEN}</div>
                )}
              </div>

              <div className="field" style={{ marginTop: 4 }}>
                <label htmlFor="wb-file-name">File name (optional)</label>
                <input
                  id="wb-file-name"
                  className="mono"
                  placeholder={outName.trim() || "defaults to output name"}
                  value={fileName}
                  onChange={(e) => setFileName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") submitSave();
                  }}
                />
                <div className="hint">Defaults to the output name.</div>
              </div>

              {saveMut.error != null && (
                <div
                  style={{
                    marginTop: 8,
                    fontSize: 12,
                    color: "var(--red)",
                  }}
                >
                  {saveErrorMessage(saveMut.error, fileName.trim() || outName.trim(), auth.pipelinesLocked)}
                </div>
              )}

              <div
                className="toolbar"
                style={{ marginTop: 16, justifyContent: "flex-end" }}
              >
                <button
                  type="button"
                  className="button"
                  disabled={saveMut.isPending}
                  onClick={() => setSaveOpen(false)}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="primary"
                  disabled={saveMut.isPending}
                  onClick={submitSave}
                >
                  {saveMut.isPending ? "Saving…" : "Save"}
                </button>
              </div>
            </Modal>
          )}
        </main>
      </div>

      <style>{`
        .wb-layout {
          display: grid;
          grid-template-columns: 200px 1fr;
          gap: 18px;
          align-items: start;
        }
        .wb-sidebar {
          background: var(--bg-1);
          border: 1px solid var(--border);
          border-radius: 10px;
          padding: 14px 12px;
        }
        .wb-ds-head {
          font-size: 10.5px;
          letter-spacing: 0.12em;
          text-transform: uppercase;
        }
        .wb-ds-list {
          list-style: none;
          margin: 8px 0 0;
          padding: 0;
          display: flex;
          flex-direction: column;
          gap: 1px;
        }
        .wb-ds-item {
          width: 100%;
          text-align: left;
          background: transparent;
          border: none;
          color: var(--text-dim);
          padding: 5px 8px;
          border-radius: 6px;
          font-size: 12.5px;
          cursor: pointer;
        }
        .wb-ds-item:hover:not(:disabled) {
          background: var(--bg-2);
          color: var(--text);
        }
        .wb-main { min-width: 0; }
        .wb-viewtabs { display: inline-flex; gap: 4px; }
      `}</style>
    </div>
  );
}
