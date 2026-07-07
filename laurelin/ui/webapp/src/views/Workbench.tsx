// SQL Workbench — run read-only DuckDB queries over the workspace datasets.
// Each dataset is exposed as a view named after the dataset.

import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation } from "@tanstack/react-query";
import { EditorView, keymap } from "@codemirror/view";
import { EditorState } from "@codemirror/state";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { sql } from "@codemirror/lang-sql";
import { oneDark } from "@codemirror/theme-one-dark";
import { api, API, ApiError } from "../api";
import type { Dataset, QueryResult, PipelineWriteResult } from "../types";
import { PageHeader, Spinner, ErrorBox, fmtValue } from "../ui";
import { useAuth } from "../auth";

const MAX_ROWS = 1000;
const PLACEHOLDER = "-- Write SQL over your datasets. Ctrl+Enter to run.\n";
const NAME_RE = /^[a-z][a-z0-9_]*$/;

function saveErrorMessage(err: unknown, fileName: string): string {
  if (err instanceof ApiError) {
    if (err.status === 409) {
      return `A transform file named ${fileName} already exists.`;
    }
    if (err.status === 403) {
      return "You don't have permission to create transforms (editor role required, or pipeline editing is locked).";
    }
    if (err.status === 400) {
      return err.detail || "Invalid request.";
    }
    return err.detail || "Failed to save transform.";
  }
  return "Failed to save transform.";
}

export function WorkbenchView() {
  const auth = useAuth();
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const runRef = useRef<() => void>(() => {});
  // Set once the editor is mounted, so sidebar-click handlers can update the doc.
  const [ready, setReady] = useState(false);

  // "Save as transform" form state.
  const [saveOpen, setSaveOpen] = useState(false);
  const [outName, setOutName] = useState("");
  const [fileName, setFileName] = useState("");
  const [nameErr, setNameErr] = useState<string | null>(null);
  const [saved, setSaved] = useState<PipelineWriteResult | null>(null);

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
      setNameErr("Must match ^[a-z][a-z0-9_]*$ (lowercase, digits, underscore).");
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

  const runMut = useMutation<QueryResult, unknown, string>({
    mutationFn: (sqlText: string) =>
      api.post<QueryResult>(`${API}/query`, { sql: sqlText, max_rows: MAX_ROWS }),
  });

  // Keep the run callback fresh so the Ctrl-Enter keymap never calls a stale closure.
  runRef.current = () => {
    const text = viewRef.current?.state.doc.toString() ?? "";
    if (text.trim()) runMut.mutate(text);
  };

  // Mount the editor exactly once.
  useEffect(() => {
    if (!hostRef.current) return;
    const view = new EditorView({
      parent: hostRef.current,
      state: EditorState.create({
        doc: PLACEHOLDER,
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
        ],
      }),
    });
    viewRef.current = view;
    setReady(true);
    return () => {
      view.destroy();
      viewRef.current = null;
    };
  }, []);

  function setDoc(text: string) {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: text },
    });
    view.focus();
  }

  const datasets = datasetsQ.data ?? [];
  const result = runMut.data;

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
            <ErrorBox error={datasetsQ.error} />
          ) : datasets.length === 0 ? (
            <div className="dim" style={{ fontSize: 12.5, padding: "8px 0" }}>
              No datasets yet.
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
            <span className="faint" style={{ fontSize: 12 }}>
              ⌘/Ctrl+Enter
            </span>
          </div>

          {runMut.isPending && <Spinner label="Running query…" />}

          {runMut.error != null && !runMut.isPending && (
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
                <span>
                  {result.row_count.toLocaleString("en-US")} rows
                  {result.truncated ? ` · truncated at ${MAX_ROWS}` : ""}
                </span>
                {auth.can("editor") && (
                  <button
                    type="button"
                    className="button small"
                    onClick={openSaveForm}
                  >
                    Save as transform
                  </button>
                )}
              </div>

              {saved && (
                <div
                  style={{
                    marginBottom: 10,
                    fontSize: 12.5,
                    color: "var(--green)",
                  }}
                >
                  Created transform file '{saved.name}.py' —{" "}
                  <Link to="/pipeline">build it from the Pipeline tab.</Link>
                  {saved.collect_error ? (
                    <span
                      style={{
                        display: "block",
                        color: "var(--red)",
                        marginTop: 4,
                      }}
                    >
                      Note: {saved.collect_error}
                    </span>
                  ) : null}
                </div>
              )}
              {result.columns.length === 0 ? (
                <div className="dim" style={{ fontSize: 13, padding: "8px 0" }}>
                  No result set.
                </div>
              ) : (
                <div className="table-wrap">
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

          {saveOpen && (
            <div
              className="modal-backdrop"
              onClick={() => {
                if (!saveMut.isPending) setSaveOpen(false);
              }}
            >
              <div className="modal" onClick={(e) => e.stopPropagation()}>
                <div className="card-title">Save as transform</div>
                <p className="dim" style={{ fontSize: 12.5, marginTop: 4 }}>
                  Generate a Python transform from the current query. Build it
                  from the Pipeline tab afterward.
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
                    <div className="hint">
                      Lowercase letters, digits, and underscores.
                    </div>
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
                    {saveErrorMessage(saveMut.error, fileName.trim() || outName.trim())}
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
              </div>
            </div>
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
      `}</style>
    </div>
  );
}
