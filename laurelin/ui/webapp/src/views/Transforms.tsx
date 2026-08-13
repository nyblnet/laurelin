// Transforms — the pipeline code editor. Author `pipelines/*.py` transform code
// (@transform / @sql_transform) from the browser. Left: file list; right: a
// CodeMirror 6 Python editor. Editing is gated on the editor role.

import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { EditorView, keymap } from "@codemirror/view";
import { EditorState } from "@codemirror/state";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { python } from "@codemirror/lang-python";
import { oneDark } from "@codemirror/theme-one-dark";
import { ApiError, api, API } from "../api";
import type { PipelineFileContent, PipelineFileInfo, PipelineWriteResult } from "../types";
import { useAuth } from "../auth";
import { ErrorBox, FailureNote, PageHeader, Spinner } from "../ui";
import { ImportedPipelinesNotice } from "./ImportedPipelinesNotice";

const NAME_RE = /^[a-z][a-z0-9_]*$/;

const NEW_FILE_TEMPLATE = `from laurelin.transforms import transform, sql_transform, Input, Output


`;

// A buffer being edited. `isNew` files don't exist on the server until saved.
interface Buffer {
  name: string;
  isNew: boolean;
}

export function TransformsView() {
  const auth = useAuth();
  const canEdit = auth.can("editor");
  const qc = useQueryClient();

  const [buffer, setBuffer] = useState<Buffer | null>(null);
  // Result of the most recent save (declared transforms + any DAG collect error).
  const [saveResult, setSaveResult] = useState<PipelineWriteResult | null>(null);

  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const saveRef = useRef<() => void>(() => {});
  const [ready, setReady] = useState(false);

  const filesQ = useQuery({
    queryKey: ["pipelines"],
    queryFn: () => api.get<PipelineFileInfo[]>(`${API}/pipelines`),
    // Editor-gated on the server; asking as a viewer buys a 403 and nothing else.
    enabled: canEdit,
  });

  // Load a file's content on demand (only for existing files).
  const contentQ = useQuery({
    queryKey: ["pipeline", buffer?.name],
    queryFn: () =>
      api.get<PipelineFileContent>(`${API}/pipelines/${encodeURIComponent(buffer!.name)}`),
    enabled: !!buffer && !buffer.isNew,
  });

  const saveMut = useMutation<PipelineWriteResult, unknown, { name: string; content: string }>({
    mutationFn: ({ name, content }) => putPipeline(name, content),
    onSuccess: (res) => {
      setSaveResult(res);
      setBuffer({ name: res.name, isNew: false });
      qc.invalidateQueries({ queryKey: ["pipelines"] });
      qc.invalidateQueries({ queryKey: ["pipeline", res.name] });
    },
  });

  const delMut = useMutation<unknown, unknown, string>({
    mutationFn: (name) => api.del(`${API}/pipelines/${encodeURIComponent(name)}`),
    onSuccess: () => {
      setBuffer(null);
      setSaveResult(null);
      setDoc("");
      qc.invalidateQueries({ queryKey: ["pipelines"] });
    },
  });

  function doSave() {
    if (!canEdit || !buffer) return;
    const content = viewRef.current?.state.doc.toString() ?? "";
    saveMut.mutate({ name: buffer.name, content });
  }
  saveRef.current = doSave;

  // Mount the editor exactly once.
  useEffect(() => {
    if (!hostRef.current) return;
    const view = new EditorView({
      parent: hostRef.current,
      state: EditorState.create({
        doc: "",
        extensions: [
          history(),
          python(),
          oneDark,
          EditorView.editable.of(canEdit),
          keymap.of([
            {
              key: "Mod-s",
              preventDefault: true,
              run: () => {
                saveRef.current();
                return true;
              },
            },
            indentWithTab,
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
    // canEdit is captured once; role is stable for a session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function setDoc(text: string) {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
  }

  // When an existing file's content arrives, load it into the editor.
  useEffect(() => {
    if (ready && buffer && !buffer.isNew && contentQ.data) {
      setDoc(contentQ.data.content);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, contentQ.data, buffer?.name]);

  function openFile(info: PipelineFileInfo) {
    setSaveResult(null);
    saveMut.reset();
    setBuffer({ name: info.name, isNew: false });
    // content loads via contentQ; clear the buffer meanwhile.
    setDoc("");
  }

  function newFile() {
    if (!canEdit) return;
    const raw = window.prompt("New pipeline file name (without .py):");
    if (raw == null) return;
    const base = raw.trim().replace(/\.py$/, "");
    if (!NAME_RE.test(base)) {
      window.alert("Invalid name. Use lowercase letters, digits and underscores, starting with a letter (e.g. aviation).");
      return;
    }
    // Compare stems, not filenames. The API returns `name` as the bare module
    // name; this used to compare it against `${base}.py`, so the guard never
    // fired and "New file" over an existing name silently overwrote it on save.
    if ((filesQ.data ?? []).some((f) => f.name === base)) {
      window.alert(`A file named ${base}.py already exists.`);
      return;
    }
    setSaveResult(null);
    saveMut.reset();
    setBuffer({ name: base, isNew: true });
    setDoc(NEW_FILE_TEMPLATE);
    viewRef.current?.focus();
  }

  const files = filesQ.data ?? [];
  const declared = saveResult?.transforms ?? null;

  return (
    <div>
      <PageHeader
        title="Transforms"
        subtitle="Author pipeline code. Transforms are Python — use @transform / @sql_transform. Build them from the Pipeline tab."
        actions={
          canEdit ? (
            <button type="button" className="primary" onClick={newFile}>
              + New file
            </button>
          ) : undefined
        }
      />

      <ImportedPipelinesNotice />

      {/* R2 raised the *read* here to editor, and the page has to say so
          instead of showing a 403 in a red box. A pipeline file is exec'd on
          every build, so writing one is code-execution-equivalent — and reading
          one hands you the same authored text. The viewer's real need is
          lineage, which is on the Pipeline tab and still theirs. */}
      {!canEdit ? (
        <div className="withheld-box">
          <div className="withheld-head">Pipeline files are not shown to your role</div>
          <p>
            They reach an editor and above — the level that can write them. A
            transform file is Python that Laurelin executes, so being able to
            read one is the same disclosure as being able to write one.
          </p>
          <p style={{ marginTop: 8 }}>
            What every transform produces, what it reads, and how a build went
            are on the <Link to="/pipeline">Pipeline</Link> tab, which is yours.
          </p>
        </div>
      ) : (
      <div className="tf-layout">
        <aside className="tf-sidebar">
          <div className="tf-head faint">Pipeline files</div>
          {filesQ.isLoading ? (
            <Spinner />
          ) : filesQ.error ? (
            <ErrorBox error={filesQ.error} />
          ) : files.length === 0 ? (
            <div className="tf-empty dim">
              <div>No pipeline files yet.</div>
              {canEdit && (
                <button type="button" className="small" style={{ marginTop: 10 }} onClick={newFile}>
                  + New file
                </button>
              )}
            </div>
          ) : (
            <ul className="tf-list">
              {files.map((f) => (
                <li key={f.name}>
                  <button
                    type="button"
                    className={`tf-item${buffer?.name === f.name ? " selected" : ""}`}
                    onClick={() => openFile(f)}
                  >
                    <span className="tf-item-name mono">{f.name}</span>
                    <span className="tf-item-sub">
                      {/* R1: the listing carries a boolean, not the exception
                          text — the file is exec'd, so that text was whatever
                          an arbitrary library said while importing it. Open the
                          file to see the classified reason. */}
                      {f.failed ? (
                        <span
                          className="badge badge-red"
                          title="This file does not import. Open it to see why."
                        >
                          will not import
                        </span>
                      ) : f.transforms.length > 0 ? (
                        <span className="dim">{f.transforms.join(", ")}</span>
                      ) : (
                        <span className="faint">no transforms</span>
                      )}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>

        <main className="tf-main">
          <div className="toolbar tf-toolbar">
            <span className="tf-filename mono">
              {buffer ? (
                <>
                  {buffer.name}
                  {buffer.isNew && <span className="faint" style={{ marginLeft: 8 }}>(new)</span>}
                </>
              ) : (
                <span className="faint">No file selected</span>
              )}
            </span>
            <span style={{ flex: 1 }} />
            {canEdit && (
              <>
                <button
                  type="button"
                  className="primary"
                  disabled={!ready || !buffer || saveMut.isPending}
                  onClick={doSave}
                >
                  {saveMut.isPending ? "Saving…" : "Save"}
                </button>
                <button
                  type="button"
                  className="danger"
                  disabled={!buffer || buffer.isNew || delMut.isPending}
                  onClick={() => {
                    if (buffer && window.confirm(`Delete ${buffer.name}? This cannot be undone.`)) {
                      delMut.mutate(buffer.name);
                    }
                  }}
                >
                  {delMut.isPending ? "Deleting…" : "Delete"}
                </button>
              </>
            )}
          </div>

          {saveMut.isSuccess && !saveMut.isPending && (
            <div className="tf-note ok">
              Saved{declared && declared.length > 0 ? ` — transforms: ${declared.join(", ")}` : " — no transforms declared"}
            </div>
          )}
          {saveResult?.collect_error && (
            <>
              <div className="tf-note bad">
                File saved, but the pipeline DAG will not collect.
              </div>
              <FailureNote failure={saveResult.collect_error} />
            </>
          )}
          {/* Why the file that is *open* will not import — the detail the
              listing deliberately does not carry. */}
          {contentQ.data?.failure && <FailureNote failure={contentQ.data.failure} />}
          {saveMut.error != null && !saveMut.isPending && <ErrorBox error={saveMut.error} />}
          {delMut.error != null && !delMut.isPending && <ErrorBox error={delMut.error} />}

          <div className="cm-host tf-cm" ref={hostRef} />

          {buffer && !buffer.isNew && contentQ.isLoading && (
            <div className="dim" style={{ fontSize: 12.5, marginTop: 8 }}>Loading file…</div>
          )}
          {buffer && !buffer.isNew && contentQ.error != null && <ErrorBox error={contentQ.error} />}

          {!buffer && (
            <div className="dim" style={{ fontSize: 12.5, marginTop: 10 }}>
              Select a file from the left to view or edit it{canEdit ? ", or create a new one." : "."}
            </div>
          )}

          <div className="tf-hint faint">
            Transforms are Python. Use <span className="mono">@transform</span> /{" "}
            <span className="mono">@sql_transform</span>. Build them from the Pipeline tab.
          </div>
        </main>
      </div>
      )}

      <style>{`
        .tf-layout {
          display: grid;
          grid-template-columns: 240px minmax(0, 1fr);
          gap: 18px;
          align-items: start;
        }
        .tf-sidebar {
          background: var(--bg-1);
          border: 1px solid var(--border);
          border-radius: 10px;
          padding: 14px 12px;
        }
        .tf-head {
          font-size: 10.5px;
          letter-spacing: 0.12em;
          text-transform: uppercase;
        }
        .tf-empty { font-size: 12.5px; padding: 8px 2px; }
        .tf-list {
          list-style: none;
          margin: 10px 0 0;
          padding: 0;
          display: flex;
          flex-direction: column;
          gap: 2px;
        }
        .tf-item {
          width: 100%;
          text-align: left;
          background: transparent;
          border: 1px solid transparent;
          border-radius: 7px;
          padding: 7px 9px;
          cursor: pointer;
          display: flex;
          flex-direction: column;
          gap: 3px;
        }
        .tf-item:hover { background: var(--bg-2); }
        .tf-item.selected {
          background: var(--bg-2);
          border-color: var(--border-2);
        }
        .tf-item-name { font-size: 12.5px; color: var(--text); }
        .tf-item-sub { font-size: 11.5px; line-height: 1.3; word-break: break-word; }
        .tf-main { min-width: 0; }
        .tf-toolbar { margin-bottom: 10px; }
        .tf-filename { font-size: 13px; }
        .tf-note {
          font-size: 12.5px;
          padding: 8px 11px;
          border-radius: 8px;
          margin-bottom: 10px;
          border: 1px solid var(--border);
        }
        .tf-note.ok { color: var(--text-dim); background: var(--bg-1); }
        .tf-note.bad { color: var(--red); background: rgba(224,102,95,0.08); border-color: #6b3330; }
        .tf-cm .cm-editor { height: 60vh; min-height: 360px; }
        .tf-hint {
          margin-top: 12px;
          font-size: 11.5px;
        }
        @media (max-width: 860px) {
          .tf-layout { grid-template-columns: 1fr; }
        }
      `}</style>
    </div>
  );
}

// The shared api client exposes get/post/patch/del/upload but no PUT, and
// pipeline writes are PUT-only — so this typed wrapper mirrors the client's
// error handling (surfacing ApiError so ErrorBox / the auth layer can react).
async function putPipeline(name: string, content: string): Promise<PipelineWriteResult> {
  let res: Response;
  try {
    res = await fetch(`${API}/pipelines/${encodeURIComponent(name)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
      credentials: "same-origin",
    });
  } catch (e) {
    throw new ApiError(0, `Network error: ${(e as Error).message}`);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      if (data && data.detail) {
        detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
      }
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<PipelineWriteResult>;
}
