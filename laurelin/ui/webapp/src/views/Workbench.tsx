// SQL Workbench — run read-only DuckDB queries over the workspace datasets.
// Each dataset is exposed as a view named after the dataset.

import { useEffect, useRef, useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { EditorView, keymap } from "@codemirror/view";
import { EditorState } from "@codemirror/state";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { sql } from "@codemirror/lang-sql";
import { oneDark } from "@codemirror/theme-one-dark";
import { api, API } from "../api";
import type { Dataset, QueryResult } from "../types";
import { PageHeader, Spinner, ErrorBox, fmtValue } from "../ui";

const MAX_ROWS = 1000;
const PLACEHOLDER = "-- Write SQL over your datasets. Ctrl+Enter to run.\n";

export function WorkbenchView() {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const runRef = useRef<() => void>(() => {});
  // Set once the editor is mounted, so sidebar-click handlers can update the doc.
  const [ready, setReady] = useState(false);

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
              <div className="result-meta">
                {result.row_count.toLocaleString("en-US")} rows
                {result.truncated ? ` · truncated at ${MAX_ROWS}` : ""}
              </div>
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
