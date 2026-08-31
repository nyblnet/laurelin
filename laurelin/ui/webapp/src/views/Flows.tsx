// Pipelines · Visual tab — build a pipeline without writing code.
// (Formerly the Flows screen; the /flows routes redirect here.)
//
// This screen is the product. Everything under `laurelin/transforms/flow_*.py`
// is plumbing for it: the audience is an analyst who cannot write Python, and a
// no-code builder that is a JSON editor with extra steps has failed however
// correct its compiler is. So the vocabulary here is "Filter rows", "Combine
// with…", "Group and summarise" — never `WHERE`, `JOIN`, `GROUP BY` — and every
// control is a dropdown over a closed list or a column picked from the live
// schema.
//
// Three things the screen has to say out loud, because they are surprising and
// because discovering them afterwards is worse:
//
//   * a preview runs as YOU, with your row policy and column masks; the build
//     runs as the system and will see at least as many rows;
//   * ejecting to Python is ONE WAY, and it is said before the click, not after;
//   * if a source dataset is restricted, the dataset this flow produces will be
//     restricted to you until an admin grants access.

import { useEffect, useMemo, useState } from "react";
import { Link, Route, Routes, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, ApiError, api } from "../api";
import { useAuth } from "../auth";
import type {
  Build,
  Dataset,
  FlowDef,
  FlowEjectResult,
  FlowKind,
  FlowListEntry,
  FlowLitType,
  FlowNode,
  FlowNodeKind,
  FlowPreviewResult,
  FlowReadResult,
  FlowSchemaResult,
  FlowCompiledSql,
  FlowWriteResult,
  PipelineFileInfo,
} from "../types";
import {
  Badge,
  DataTable,
  EmptyState,
  ErrorBox,
  FailureNote,
  LiveStatus,
  Modal,
  PageHeader,
  Spinner,
  fmtNum,
  fmtValue,
  truncationNote,
} from "../ui";
import { ImportedPipelinesNotice } from "./ImportedPipelinesNotice";
import { PIPELINES_SUBTITLE, PipelinesTabs } from "./Pipelines";
import { NameDialog } from "./flow/NameDialog";
import { StepForm, ExpectationsForm } from "./flow/StepForm";
import type { TypeHints } from "./flow/ExprEditor";
import {
  consumerOf,
  emptyFlow,
  flowIssues,
  inputSchema,
  insertAfter,
  nodeById,
  readRefusal,
  removeStep,
  rightSchema,
  schemaAt,
  sourceDatasets,
  saveConflict,
  spine,
  stepIssue,
  summarise,
  updateParams,
} from "./flow/model";
import { KINDS, KIND_ORDER } from "./flow/vocab";
import { FLOW_STYLES } from "./flow/styles";

/** How many rows a preview asks for. Stated in the banner below — the banner
 *  used to quote `FLOW_PREVIEW_MAX_ROWS` (200) while the panel requested 50,
 *  so the one honest sentence on the screen cited the wrong number. */
const PREVIEW_ROWS = 50;

export function FlowsView() {
  return (
    <>
      <Routes>
        <Route path="/" element={<FlowList />} />
        <Route path=":name" element={<FlowBuilderRoute />} />
      </Routes>
      <style>{FLOW_STYLES}</style>
    </>
  );
}

// ------------------------------------------------------------------ the list

function FlowList() {
  const auth = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  // `?from=` is the dataset detail page's "New pipeline from this dataset"
  // door: open the naming dialog straight away, and carry the dataset through
  // to the builder so its first step is already reading the right data.
  const fromDataset = params.get("from") ?? "";

  const flowsQ = useQuery({
    queryKey: ["flows"],
    queryFn: () => api.get<FlowListEntry[]>(`${API}/flows`),
    enabled: auth.can("editor"),
  });

  // The Python tab's files, for two jobs: the first-run hero must only claim
  // "start here" when the workspace truly has no pipelines of EITHER kind
  // (a visual-empty list over a workspace full of Python pipelines used to
  // read as "nothing exists yet"), and the naming dialog can refuse a name a
  // code transform already owns before the server has to.
  const pipelinesQ = useQuery({
    queryKey: ["pipelines"],
    queryFn: () => api.get<PipelineFileInfo[]>(`${API}/pipelines`),
    enabled: auth.can("editor"),
  });

  // A pipeline's name is also the name of the dataset it builds, so a name an
  // existing dataset owns is refused at the dialog, not by a 409 after it.
  const datasetsQ = useQuery({
    queryKey: ["datasets"],
    queryFn: () => api.get<Dataset[]>(`${API}/datasets`),
  });

  const [naming, setNaming] = useState(!!fromDataset);

  const flows = flowsQ.data ?? [];
  const pythonFiles = pipelinesQ.data ?? [];

  return (
    <div>
      <PageHeader
        title="Pipelines"
        subtitle={PIPELINES_SUBTITLE}
        actions={
          // Role-gated like every other authoring control. A viewer was shown
          // "+ New pipeline" and, behind it, a 403 — an offer the product
          // cannot keep. The nav already hides Pipelines from a viewer; this
          // is the direct-link path.
          auth.can("editor") ? (
            <button type="button" className="primary" onClick={() => setNaming(true)}>
              + New pipeline
            </button>
          ) : undefined
        }
      />

      <PipelinesTabs active="visual" />

      <ImportedPipelinesNotice />

      {!auth.can("editor") ? (
        // Prose, and NO Retry: a role refusal cannot change by asking again,
        // and the Schedules page already answers this situation this way.
        // The raw "Insufficient permissions (403)" with a futile Retry was the
        // only place in the product that offered a control that could not work.
        <EmptyState>
          Pipelines are not shown to your role. They reach an editor and above,
          because a pipeline is code this workspace runs. Ask an editor or an
          administrator if you need to see one.
        </EmptyState>
      ) : flowsQ.isLoading ? (
        <Spinner />
      ) : flowsQ.error ? (
        <ErrorBox error={flowsQ.error} onRetry={() => flowsQ.refetch()} />
      ) : flows.length === 0 && pythonFiles.length > 0 ? (
        // Not the first-run hero: this workspace HAS pipelines, they are just
        // all Python. Saying "start your first pipeline" here told an analyst
        // the workspace was empty when it was not.
        <EmptyState>
          <div>
            No visual pipelines yet — but {pythonFiles.length} Python pipeline{" "}
            {pythonFiles.length === 1 ? "file exists" : "files exist"} on the{" "}
            <Link to="/pipelines?tab=python">Python tab</Link>.
          </div>
          <button
            type="button"
            className="primary"
            style={{ marginTop: 10 }}
            onClick={() => setNaming(true)}
          >
            + New visual pipeline
          </button>
        </EmptyState>
      ) : flows.length === 0 ? (
        <div className="fx-onboard">
          <h2>Build a pipeline without writing code.</h2>
          <p>
            Pick a dataset, then add steps: filter rows, combine two datasets,
            group and summarise. Every step shows you the result as you go.
          </p>
          <p className="faint">
            What you build is a normal transform. It appears on{" "}
            <Link to="/builds">Builds</Link> with its lineage, it can be
            scheduled, and it obeys the same permissions as everything else.
          </p>
          <button type="button" className="primary" onClick={() => setNaming(true)}>
            Start your first pipeline
          </button>
        </div>
      ) : (
        <div className="cards fx-cards">
          {flows.map((f) => (
            <div
              key={f.name}
              className="card clickable"
              role="button"
              tabIndex={0}
              onClick={() => navigate(`/pipelines/${encodeURIComponent(f.name)}`)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  navigate(`/pipelines/${encodeURIComponent(f.name)}`);
                }
              }}
            >
              <div className="fx-card-head">
                <strong>{f.name}</strong>
                {f.failed ? (
                  <Badge tone="red">needs repair</Badge>
                ) : (
                  <Badge tone="neutral">{f.nodes} steps</Badge>
                )}
              </div>
              <div className="dim">{f.description || "No description"}</div>
              <div className="faint" style={{ marginTop: 6 }}>
                {f.failed
                  ? "This pipeline will not load. Open it to see what to fix."
                  : `Reads ${f.sources.join(", ") || "nothing yet"}`}
              </div>
              {f.author && <div className="faint">by {f.author}</div>}
            </div>
          ))}
        </div>
      )}

      {naming && (
        <NameDialog
          title="Name your new pipeline"
          intro={
            <>
              A pipeline builds one dataset, and the two share a name. Pick something
              you would be happy to see on a dashboard — it cannot be renamed afterwards, because
              everything downstream is keyed on it.
            </>
          }
          confirmLabel="Start building"
          taken={[
            ...flows.map((f) => f.name),
            // Transforms the Python tab's files declare: the server refuses a
            // flow that shares a name with a code transform, so the dialog
            // refuses it first, with the same rule.
            ...pythonFiles.flatMap((f) => f.transforms ?? []),
          ]}
          takenDatasets={(datasetsQ.data ?? []).map((d) => d.name)}
          onCancel={() => setNaming(false)}
          onSubmit={(name) => {
            setNaming(false);
            // Nothing is written until the first Save. An empty flow (one
            // source, no dataset yet) is structurally valid, so the URL is
            // real and refreshable from the first moment.
            navigate(
              `/pipelines/${encodeURIComponent(name)}?new=1` +
                (fromDataset ? `&dataset=${encodeURIComponent(fromDataset)}` : ""),
            );
          }}
        />
      )}
    </div>
  );
}

// ------------------------------------------------------------------ builder

/**
 * Keyed on the flow's name, and that `key` is load-bearing.
 *
 * The builder holds the draft in component state and seeds it once, when the
 * fetch lands. Without a key, navigating from one flow to another reuses the
 * instance: the URL and the header change, the draft does not, and the author
 * is editing the previous flow's steps under the new flow's name. Measured
 * exactly that before adding this — the header read "policy_probe" while the
 * body read "builds the dataset late_flights".
 */
function FlowBuilderRoute() {
  const { name = "" } = useParams();
  return <FlowBuilder key={name} name={name} />;
}

function FlowBuilder({ name }: { name: string }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const auth = useAuth();
  const urlParams = new URLSearchParams(window.location.hash.split("?")[1] ?? "");
  const isNew = urlParams.get("new") === "1";
  // Carried from the dataset detail page's "New pipeline from this dataset"
  // door (via the naming dialog): the first step reads this dataset already.
  const presetSource = urlParams.get("dataset") ?? "";

  const [draft, setDraft] = useState<FlowDef | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [dirty, setDirty] = useState(false);
  const [saveResult, setSaveResult] = useState<FlowWriteResult | null>(null);
  const [showSql, setShowSql] = useState(false);
  // Save was clicked while a step is unfinished. The click used to be a
  // silent no-op: the button stayed enabled, nothing near it changed, and the
  // only explanation sat lower on the page where it had already been before
  // the click — nothing connected "I clicked Save" to "here is why nothing
  // happened", on a path where closing the tab loses the whole draft.
  const [saveRefused, setSaveRefused] = useState(false);
  // A refused step removal, said in the page rather than in an OS dialog.
  const [stepRefusal, setStepRefusal] = useState<string | null>(null);
  const [ejectOpen, setEjectOpen] = useState(false);
  const [addAfter, setAddAfter] = useState<string | null>(null);
  const [builtId, setBuiltId] = useState<string | null>(null);
  const [duplicating, setDuplicating] = useState(false);

  // --------------------------------------------------------------- loading

  const flowQ = useQuery({
    queryKey: ["flow", name],
    queryFn: () => api.get<FlowReadResult>(`${API}/flows/${encodeURIComponent(name)}`),
    enabled: !!name && !isNew,
    retry: false,
  });

  useEffect(() => {
    if (isNew && draft === null) {
      const f = emptyFlow(name, auth.user?.username ?? "");
      // The source picker still governs what is offered — this only pre-picks
      // the dataset the caller was just looking at.
      if (presetSource) f.nodes[0] = { ...f.nodes[0], params: { dataset: presetSource } };
      setDraft(f);
      setSelected("s1");
      setDirty(true);
    }
  }, [isNew, name, draft, auth.user?.username, presetSource]);

  useEffect(() => {
    if (flowQ.data?.flow && draft === null) {
      setDraft(flowQ.data.flow);
      setSelected(flowQ.data.flow.terminal);
    }
  }, [flowQ.data, draft]);

  // Datasets this account can read — the source picker's whole vocabulary.
  // A dataset the author cannot view is not offered rather than refused after
  // the fact, which is the same rule the server enforces in
  // `check_flow_sources`, said earlier.
  const datasetsQ = useQuery({
    queryKey: ["datasets"],
    queryFn: () => api.get<Dataset[]>(`${API}/datasets`),
  });

  // Schemas of every source the flow reads. `GET /flows/schema` is the only
  // route that answers for a source-scanned dataset (federated / ClickHouse /
  // StarRocks / Iceberg have no version row, so /datasets/{n}/schema 400s).
  const wantedSchemas = draft ? sourceDatasets(draft) : [];
  const schemaQs = useQueries({
    queries: wantedSchemas.map((ds) => ({
      queryKey: ["flow-schema", ds],
      queryFn: () => api.get<FlowSchemaResult>(`${API}/flows/schema?dataset=${encodeURIComponent(ds)}`),
      staleTime: 60_000,
      retry: false,
    })),
  });
  const sourceSchemas = useMemo(() => {
    const out: Record<string, string[] | undefined> = {};
    wantedSchemas.forEach((ds, i) => {
      const d = schemaQs[i]?.data;
      if (d) out[ds] = d.columns;
    });
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(wantedSchemas), schemaQs.map((q) => q.status).join(",")]);

  // --------------------------------------------------------------- preview
  //
  // Debounced, never per keystroke: a canvas that previews as you type competes
  // for the same process-wide admission semaphore as dashboards and object
  // reads, and losing that race hands everyone else a 429.

  const [debounced, setDebounced] = useState<string>("");
  const previewKey = draft && selected ? JSON.stringify({ flow: draft, node: selected }) : "";
  useEffect(() => {
    const t = setTimeout(() => setDebounced(previewKey), 750);
    return () => clearTimeout(t);
  }, [previewKey]);

  const issues = draft ? flowIssues(draft) : [];
  // Only preview a flow whose steps are all filled in. An unfinished step's
  // refusal ("dataset '' is not a valid dataset name") names a key the author
  // has never seen; the card says "Pick a dataset" instead.
  const previewable = !!draft && issues.length === 0;

  // True when the debounce has caught up with the draft. While it has not, the
  // preview on screen answers a question the author has already moved past, and
  // showing its refusal made a message the author had just fixed hang around
  // for the rest of the debounce window.
  const settled = previewKey === debounced;

  const previewQ = useQuery({
    queryKey: ["flow-preview", debounced],
    queryFn: ({ signal }) =>
      api.post<FlowPreviewResult>(`${API}/flows/preview`, {
        flow: JSON.parse(debounced).flow,
        node_id: JSON.parse(debounced).node,
        max_rows: PREVIEW_ROWS,
      }, signal),
    enabled: previewable && !!debounced,
    retry: false,
    staleTime: 30_000,
  });

  // Column types, learned from whatever previews have run. Accumulated across
  // previews rather than read off the current one, because the author is
  // usually editing the step whose preview does not exist yet — that is what
  // "pick a column and the value box becomes a number box" needs to survive.
  //
  // These are only ever DEFAULTS for a value input. The server coerces the
  // declared type strictly and refuses a mismatch, so a wrong guess costs the
  // author one dropdown, never a wrong answer.
  const [hints, setHints] = useState<TypeHints>({});
  useEffect(() => {
    if (!previewQ.data) return;
    const learned = typeHints(previewQ.data);
    setHints((prev) => {
      const merged = { ...prev, ...learned };
      const changed = Object.keys(merged).some((k) => merged[k] !== prev[k]);
      return changed ? merged : prev;
    });
  }, [previewQ.data]);

  // What each column *holds*, from the server rather than guessed from values.
  // Two sources, and both matter: `/flows/schema` answers before any preview
  // has run (so the very first "Group and summarise" form is already right),
  // and the preview answers for steps that invent columns.
  const kinds = useMemo(() => {
    const out: Record<string, FlowKind> = {};
    wantedSchemas.forEach((_ds, i) => {
      Object.assign(out, schemaQs[i]?.data?.kinds ?? {});
    });
    Object.assign(out, previewQ.data?.kinds ?? {});
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(wantedSchemas), schemaQs.map((q) => q.status).join(","), previewQ.data]);

  // --------------------------------------------------------------- mutations

  const save = useMutation<FlowWriteResult, unknown, FlowDef>({
    mutationFn: (f) =>
      api.put<FlowWriteResult>(`${API}/flows/${encodeURIComponent(name)}`, { flow: f }),
    onSuccess: (r) => {
      setSaveResult(r);
      setDirty(false);
      setDraft(r.flow);
      qc.invalidateQueries({ queryKey: ["flows"] });
      qc.invalidateQueries({ queryKey: ["transforms"] });
      if (isNew) navigate(`/pipelines/${encodeURIComponent(name)}`, { replace: true });
    },
  });

  const del = useMutation({
    mutationFn: () => api.del(`${API}/flows/${encodeURIComponent(name)}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["flows"] });
      navigate("/pipelines");
    },
  });

  const build = useMutation<Build, unknown, void>({
    mutationFn: () => api.post<Build>(`${API}/builds`, { targets: [name] }),
    onSuccess: (b) => {
      setBuiltId(b.id);
      qc.invalidateQueries({ queryKey: ["builds"] });
    },
  });

  // Follow the build this screen started until it lands somewhere. "Build
  // started — watch it on Builds" was a static sentence that stayed on screen
  // after the build had failed; the Builds page's own in-flight pattern
  // (refetchInterval while pending/running) is copied here so the note
  // converges to the outcome without the author leaving the builder.
  const buildQ = useQuery<Build>({
    queryKey: ["flow-build", builtId],
    queryFn: () => api.get<Build>(`${API}/builds/${encodeURIComponent(builtId!)}`),
    enabled: !!builtId,
    refetchInterval: (query) => {
      const s = query.state.data?.status;
      return s === "pending" || s === "running" || s === undefined ? 1500 : false;
    },
    // The app disables refetch-on-focus globally (App.tsx), so if this
    // interval paused while the tab was unfocused the note would stay at
    // "Build started…" forever for anyone who switched away and came back.
    refetchIntervalInBackground: true,
  });

  // Fetched whenever the flow is saved, not only when "Show SQL" is open: the
  // response's `params` COUNT is shown under the compiled SQL, so an author can
  // see that their filter values are held apart from the query text. It no
  // longer gates ejection — `@sql_transform` takes bound values now, so an
  // ejected pipeline binds exactly what the flow bound.
  // "Duplicate" is what this screen offers instead of "Rename". There is no
  // rename anywhere in the flow API, deliberately: `spec.name` is the primary
  // key of lineage_edges, transform_state and build_tasks, and nothing in this
  // tree deletes lineage — so a rename would orphan the governance state of the
  // old name rather than move it.
  const duplicate = useMutation<FlowWriteResult, unknown, string>({
    mutationFn: (to) =>
      api.put<FlowWriteResult>(`${API}/flows/${encodeURIComponent(to)}`, {
        // `output` has to move with the name: the server refuses a flow whose
        // output dataset is not its own name.
        flow: { ...draft, name: to, output: to },
      }),
    onSuccess: (r) => {
      setDuplicating(false);
      qc.invalidateQueries({ queryKey: ["flows"] });
      navigate(`/pipelines/${encodeURIComponent(r.name)}`);
    },
  });

  const flowsQ = useQuery({
    queryKey: ["flows"],
    queryFn: () => api.get<FlowListEntry[]>(`${API}/flows`),
    enabled: duplicating,
  });

  const sqlQ = useQuery({
    queryKey: ["flow-sql", name, saveResult?.name],
    queryFn: () => api.get<FlowCompiledSql>(`${API}/flows/${encodeURIComponent(name)}/sql`),
    enabled: !dirty,
    retry: false,
  });

  // --------------------------------------------------------------- editing

  function edit(next: FlowDef) {
    setDraft(next);
    setDirty(true);
    save.reset();
    setSaveResult(null);
    // A build outcome describes the flow that was built. Once the author is
    // editing again the note is about the past, and "Build succeeded" above
    // unsaved changes reads as "these changes succeeded".
    setBuiltId(null);
  }

  function addStep(afterId: string, kind: FlowNodeKind) {
    if (!draft) return;
    const { flow: next, selected: sel } = insertAfter(draft, afterId, kind);
    edit(next);
    setSelected(sel);
    setAddAfter(null);
  }

  function dropStep(id: string) {
    if (!draft) return;
    const node = nodeById(draft, id);
    if (!node) return;
    // Guarded on arity, not on "is it the terminal": a JOIN's side chain also
    // starts at a source, and removing *that* would leave the join pointing at
    // a step that no longer exists — a flow the server refuses with a message
    // about a step id nobody has seen.
    if (node.inputs.length === 0) {
      const where = consumerOf(draft, id) ? "this side of the combine" : "this pipeline";
      // Not window.alert. The refusal is a page state, not an OS interrupt: an
      // alert steals focus, cannot be styled, is not announced as a status,
      // and vanishes on OK leaving no trace of what was refused or why. This
      // screen already renders its Save refusal as an inline role=status note
      // — one screen, one way of saying no.
      setStepRefusal(
        `${where[0].toUpperCase()}${where.slice(1)} has to start somewhere. ` +
          "Change the dataset instead of removing this step — or remove the whole " +
          "step that uses it.",
      );
      return;
    }
    if (node.kind === "join" && !window.confirm("Remove this combine step and the dataset it brings in?")) return;
    setStepRefusal(null);
    const next = removeStep(draft, id);
    edit(next);
    setSelected(next.terminal);
  }

  // --------------------------------------------------------------- render

  if (!name) return <EmptyState>No pipeline named.</EmptyState>;
  if (flowQ.isLoading && !draft) return <Spinner label="Opening pipeline…" />;
  if (flowQ.error && !draft) {
    const err = flowQ.error;
    if (err instanceof ApiError && err.status === 404) {
      return (
        <div>
          <PageHeader title={name} subtitle="No such pipeline" />
          <EmptyState>
            There is no pipeline called {name}. <Link to="/pipelines">Back to Pipelines</Link>.
          </EmptyState>
        </div>
      );
    }
    return <ErrorBox error={err} onRetry={() => flowQ.refetch()} />;
  }
  if (!draft && flowQ.data && !flowQ.data.flow) {
    // The file exists but is not a pipeline — hand-edited JSON, usually. The
    // read deliberately answers 200 with `{flow: null, error}` so this page
    // can say what is wrong; it used to fall through to the `!draft` spinner
    // below and spin forever, with the list card promising "open it to see
    // what to fix". The one repair this screen can offer is delete (the
    // DELETE route has always existed; the door did not).
    return (
      <div>
        <PageHeader title={name} subtitle="This pipeline will not load" />
        <div className="fx-note fx-note-bad">
          <strong>The saved file for this pipeline is not a valid pipeline.</strong>{" "}
          {flowQ.data.error ?? "No further detail was recorded for this failure."}
        </div>
        <p className="dim">
          The file was probably edited outside Laurelin. Fix{" "}
          <span className="mono">pipelines/{name}.flow.json</span> in the workspace (or restore
          it from version control) — or delete the pipeline here. Deleting removes only the
          pipeline definition; the dataset it built and that dataset&apos;s lineage are kept.
        </p>
        {del.error != null && <ErrorBox error={del.error} />}
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <Link to="/pipelines">← Pipelines</Link>
          <button
            type="button"
            className="danger"
            disabled={del.isPending}
            onClick={() => {
              // The other two delete confirms (the healthy-pipeline one below and the
              // Python tab's) promise BOTH halves. This one promised only the dataset,
              // so the same action reassured differently depending on whether the
              // pipeline happened to be loadable. Lineage survival is the half a
              // reader is least sure of, so it is the half that must not be dropped.
              if (window.confirm(`Delete the broken pipeline ${name}? The dataset it built is kept, and so is its lineage.`)) {
                del.mutate();
              }
            }}
          >
            {del.isPending ? "Deleting…" : "Delete this pipeline"}
          </button>
        </div>
      </div>
    );
  }
  if (!draft) return <Spinner />;

  const steps = spine(draft, draft.terminal);
  const selectedNode = nodeById(draft, selected) ?? steps[steps.length - 1];
  const outputSchema = schemaAt(draft, draft.terminal, sourceSchemas) ?? previewQ.data?.schema ?? [];

  // The server's refusal, mapped onto a card. `FlowRefused` names the step by
  // id; nobody has ever seen a node id, so `readRefusal` rewrites it into the
  // step's position and human label.
  const refusalSource =
    (save.error instanceof ApiError && save.error.status === 400 ? save.error.detail : null) ??
    (settled && previewQ.error instanceof ApiError && previewQ.error.status === 400
      ? previewQ.error.detail
      : null) ??
    (flowQ.data?.error ?? null);
  const refusal = refusalSource ? readRefusal(draft, refusalSource) : null;

  const restricted = saveResult?.output_will_be_restricted ?? flowQ.data?.output_will_be_restricted ?? false;
  const maskedByDataset = previewQ.data?.masked_columns ?? {};
  const maskedList = Object.entries(maskedByDataset);

  // A 409 is not one thing. "Someone else owns this name" is a naming problem
  // with an on-screen fix; "the workspace will not collect" is an operator
  // problem with no on-screen fix. `saveConflict` reads the server's
  // discriminator (falling back to the route's two known collision sentences)
  // so the rename-sized problem stops being announced as a broken workspace.
  const conflict =
    save.error instanceof ApiError && save.error.status === 409
      ? saveConflict(save.error.detail)
      : null;
  // `--lock-pipelines` no longer covers flows — that flag locks *code* (a
  // pipeline file is exec'd as the server), while a flow compiles to bound,
  // schema-checked SQL and cannot reach exec. Flows have their own lock,
  // `--lock-flows`, and the boot probe (/auth/status) tells us up front
  // whether it is set, so the banner renders before the author builds
  // something unsaveable. The 403-on-save path stays as a backstop for a
  // client whose cached auth status predates a server restart: a lock 403 is
  // server-wide, not personal, and sending the author to an administrator who
  // cannot help without a restart is the wrong diagnosis.
  const authoringLocked =
    auth.flowsLocked || (save.error instanceof ApiError && save.error.status === 403);
  //  0 = every value in this flow is a keyword the compiler owns, so the SQL is
  //  self-contained and a Python pipeline can hold all of it.
  // >0 = the flow binds that many author-supplied values, and eject is refused.
  const boundValues = sqlQ.data?.params ?? 0;

  const handlers: CardHandlers = {
    onSelect: setSelected,
    addAfter,
    setAddAfter,
    onPick: addStep,
    onRemove: dropStep,
  };

  // A step's name in the right-hand pane and in the preview header. A node on a
  // JOIN's side chain is not on the spine at all, so numbering it would print
  // "Step 0" — it gets named by what it brings in instead.
  const spineIndex = selectedNode ? steps.findIndex((s) => s.id === selectedNode.id) : -1;
  const stepLabel = !selectedNode
    ? "Step"
    : spineIndex >= 0
      ? `Step ${spineIndex + 1}`
      : "Step being combined in";

  return (
    <div className="fx-page">
      <div className="fx-head">
        <div className="fx-head-left">
          <Link to="/pipelines" className="fx-back">
            ← Pipelines
          </Link>
          <h1>{name}</h1>
          <span className="faint">
            builds the dataset <span className="mono">{draft.output}</span>
            {draft.author ? ` · by ${draft.author}` : ""}
          </span>
        </div>
        <div className="fx-head-right">
          {dirty && <span className="fx-dirty">unsaved changes</span>}
          <button
            type="button"
            className="primary"
            disabled={save.isPending || !dirty}
            onClick={() => {
              if (!previewable) {
                setSaveRefused(true);
                return;
              }
              setSaveRefused(false);
              save.mutate(draft);
            }}
          >
            {save.isPending ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            disabled={dirty || build.isPending}
            title={dirty ? "Save first — a build runs what is saved, not what is on screen." : undefined}
            onClick={() => build.mutate()}
          >
            {build.isPending ? "Building…" : "Build now"}
          </button>
          <button type="button" onClick={() => setShowSql(!showSql)}>
            {showSql ? "Hide SQL" : "Show SQL"}
          </button>
          <button
            type="button"
            onClick={() => {
              duplicate.reset();
              setDuplicating(true);
            }}
            title="Copy these steps into a new pipeline under a new name. Pipelines cannot be renamed."
          >
            Duplicate…
          </button>
          <button
            type="button"
            onClick={() => setEjectOpen(true)}
            disabled={dirty || auth.pipelinesLocked}
            /* No longer gated on `boundValues`. It used to be, because
               `TransformSpec` had no `params` field, so every flow carrying a
               single filter constant — which is to say every flow that filters
               anything — was excluded from the advertised escape hatch, and the
               only explanation lived in this tooltip. `sql_transform` now takes
               bound values, so the generated pipeline binds them exactly as the
               flow did and nothing is written into the SQL text.

               Gated on the server's Python lock, though: eject writes a `.py`,
               which is exactly what `--lock-pipelines` exists to prevent, and
               the server refuses it with a 403. Disabled with the reason here,
               rather than letting the click fail. */
            title={
              auth.pipelinesLocked
                ? "Python authoring is locked on this server (--lock-pipelines), and ejecting writes a Python file."
                : dirty
                  ? "Save first."
                  : "Convert this pipeline to Python — one-way."
            }
          >
            Open in Python…
          </button>
          <button
            type="button"
            className="danger"
            onClick={() => {
              if (window.confirm(`Delete the pipeline ${name}? The dataset it built is kept, and so is its lineage.`)) {
                del.mutate();
              }
            }}
          >
            Delete
          </button>
        </div>
      </div>

      {/* ------------------------------------------------ header-level notes */}

      {/* The Save click's receipt, adjacent to the button that refused it.
          Rendered from the LIVE issue list, so it disappears the moment the
          step is finished; role=status so the refusal is announced. */}
      {stepRefusal && (
        <div className="fx-note fx-note-warn" role="status">
          <strong>Step kept</strong> — {stepRefusal}{" "}
          <button type="button" className="small" onClick={() => setStepRefusal(null)}>
            Dismiss
          </button>
        </div>
      )}
      {saveRefused && !previewable && (
        <div className="fx-note fx-note-warn" role="status">
          <strong>Not saved</strong> —{" "}
          {issues.length === 1 ? "one step is unfinished" : `${issues.length} steps are unfinished`}:{" "}
          {/* Each `issue` is already a sentence ending in a full stop, so
              appending one produced "Pick a column.." on screen. Strip the
              step's own terminator; the joiner supplies the punctuation. */}
          {issues
            .map((x) => `${KINDS[x.node.kind].label} — ${x.issue.replace(/\.$/, "")}`)
            .join("; ") +
            ". Finish" +
            (issues.length === 1 ? " that step" : " those steps") +
            " below, then Save again."}
        </div>
      )}
      {restricted && (
        <div className="fx-note fx-note-gov">
          A dataset this pipeline reads is restricted, so <strong>{draft.output}</strong> will be
          readable only by <strong>{draft.author || "its author"}</strong> until an administrator
          grants access to someone else.
        </div>
      )}
      {refusal && (
        <div className="fx-note fx-note-bad">
          <strong>This pipeline will not run yet.</strong> {refusal.message}
        </div>
      )}
      {/* A 409 here is almost never about *this* flow. `PUT /flows/{name}`
          collects the whole registry to check for a second producer of the
          output dataset, and that collection fails if ANY file in pipelines/
          is broken — including the file this request is trying to repair.
          Measured: with one malformed `.flow.json` present, `PUT` on that very
          flow answered 409 naming it, so the repair the screen invites is not
          reachable through the screen. Say what is really wrong and where the
          way out is, rather than showing the server's sentence (which sends
          the author to Transforms, where a flow file cannot be opened). */}
      {authoringLocked ? (
        <div className="fx-note fx-note-bad">
          <strong>Visual pipeline authoring is locked on this server.</strong> An operator started
          it with <code>--lock-flows</code>, so visual pipelines cannot be saved or deleted here by
          anyone — this is the server's posture, not your permissions. Nothing you have on screen is
          lost; it just cannot be saved here. The pipelines that already exist still build and run.
        </div>
      ) : conflict?.kind === "name_collision" ? (
        <div className="fx-note fx-note-bad">
          <strong>The name {name} is taken.</strong> {conflict.message}
          <div style={{ marginTop: 6 }}>
            Nothing else is wrong with these steps — they just need a name of their own.{" "}
            <button type="button" className="small" onClick={() => { duplicate.reset(); setDuplicating(true); }}>
              Save these steps under a different name…
            </button>
          </div>
        </div>
      ) : conflict?.kind === "workspace_collect_failed" ? (
        <div className="fx-note fx-note-bad">
          <strong>Nothing can be saved or built in this workspace right now.</strong> Another
          pipeline file will not load, and Laurelin has to read them all together to work out
          which pipeline produces which dataset. That includes this one, so saving a repair is
          blocked too.
          <div style={{ marginTop: 6 }}>
            The server said: <em>{conflict.message}</em> Someone with access to the
            workspace files has to fix or remove that file — or, if the broken one is a visual
            pipeline, it can be deleted from <Link to="/pipelines">Pipelines</Link>.
          </div>
        </div>
      ) : (
        save.error != null &&
        !(save.error instanceof ApiError && save.error.status === 400) && (
          <ErrorBox error={save.error} />
        )
      )}
      {del.error != null && <ErrorBox error={del.error} />}
      {build.error != null && <ErrorBox error={build.error} />}
      {builtId && (
        <LiveStatus
          className={`fx-note ${buildQ.data?.status === "failed" ? "fx-note-bad" : "fx-note-ok"}`}
        >
          {/* One sentence family for a kicked build, everywhere a build is
              kicked (here, the Builds page, and Schedules). Measured across
              the three screens this had drifted into three subjects
              ("Build <id>" / "Run of <name>" / a bare "Build"), two failure
              trailers ("for what went wrong" / none) and three pending
              sentences. The TERMINAL fact converges completely —
              "Build <id> finished: <outcome>." then "See the build." and
              nothing after it. A second trailer is where the drift started:
              once a screen is allowed to append its own explanation of what
              the outcome means, each screen invents one. The row count that
              used to hang off "succeeded" here was exactly that, and it is
              one click away on the build itself.
              The PENDING sentence shares the subject and may carry a
              page-specific tail, because a reader standing on a history that
              is following the build needs to be told so. */}
          {buildQ.data?.status === "succeeded" || buildQ.data?.status === "failed" ? (
            <>
              <strong>
                Build <span className="mono">{builtId}</span> finished: {buildQ.data.status}.
              </strong>{" "}
              <Link to={`/builds?build=${encodeURIComponent(builtId)}`}>See the build</Link>.
            </>
          ) : (
            <>
              Build <span className="mono">{builtId}</span> is running…{" "}
              <Link to={`/builds?build=${encodeURIComponent(builtId)}`}>See the build</Link>.
            </>
          )}
        </LiveStatus>
      )}
      {save.isSuccess && !dirty && (
        <div className="fx-note fx-note-ok">
          Saved. <strong>{draft.output}</strong> will have{" "}
          {saveResult?.schema.length ?? 0} column
          {(saveResult?.schema.length ?? 0) === 1 ? "" : "s"}:{" "}
          <span className="mono">{(saveResult?.schema ?? []).join(", ")}</span>
        </div>
      )}

      {showSql && (
        <div className="fx-sql">
          <div className="fx-sql-head">
            Generated from these steps — not editable. Edit the steps, not the SQL.
          </div>
          {dirty ? (
            <div className="faint">Save first: this shows the SQL of the saved pipeline.</div>
          ) : sqlQ.isLoading ? (
            <Spinner />
          ) : sqlQ.error ? (
            <ErrorBox error={sqlQ.error} />
          ) : (
            <>
              <pre className="mono">{sqlQ.data?.sql}</pre>
              <div className="faint">
                {boundValues} value{boundValues === 1 ? " is" : "s are"} passed separately, not
                written into the query. Your filter values are never part of this text, and they
                stay separate if you convert this pipeline to Python.
              </div>
            </>
          )}
        </div>
      )}

      {/* ------------------------------------------------------ the workbench */}

      <div className="fx-grid">
        <div className="fx-steps">
          <div className="fx-col-head">Steps</div>
          {steps.map((node, i) => (
            <StepCard
              key={node.id}
              flow={draft}
              node={node}
              index={i}
              selected={selected === node.id}
              issue={stepIssue(draft, node)}
              refused={refusal?.node === node.id ? refusal.message : null}
              columns={(schemaAt(draft, node.id, sourceSchemas) ?? []).length}
              branch={
                node.kind === "join"
                  ? spine(draft, node.inputs[1]).map((b) => ({
                      node: b,
                      issue: stepIssue(draft, b),
                      refused: refusal?.node === b.id ? refusal.message : null,
                    }))
                  : null
              }
              h={handlers}
            />
          ))}

          <ExpectationsForm
            flow={draft}
            outputSchema={outputSchema}
            onChange={(expectations) => edit({ ...draft, expectations })}
          />

          <div className="field fx-desc">
            <label htmlFor="fx-desc-input">What is this pipeline for?</label>
            <input
              id="fx-desc-input"
              className="fx-in fx-wide"
              type="text"
              placeholder="One line, for whoever finds it later"
              value={draft.description}
              onChange={(e) => edit({ ...draft, description: e.target.value })}
            />
          </div>
        </div>

        <div className="fx-panel">
          <div className="fx-col-head">{stepLabel}</div>
          {selectedNode ? (
            <StepForm
              flow={draft}
              node={selectedNode}
              schema={
                selectedNode.kind === "source"
                  ? []
                  : inputSchema(draft, selectedNode.id, sourceSchemas) ?? []
              }
              rightSchema={rightSchema(draft, selectedNode.id, sourceSchemas)}
              hints={hints}
              kinds={kinds}
              datasets={datasetsQ.data ?? []}
              onParams={(params) => edit(updateParams(draft, selectedNode.id, params))}
              onRemove={() => dropStep(selectedNode.id)}
            />
          ) : (
            <EmptyState>Pick a step on the left.</EmptyState>
          )}
        </div>
      </div>

      {/* ------------------------------------------------------------ preview */}

      <div className="fx-preview">
        <div className="fx-preview-head">
          <strong>Preview</strong>
          <span className="faint">{selectedNode ? stepLabel.toLowerCase() : ""}</span>
          <span style={{ flex: 1 }} />
          <button
            type="button"
            className="small"
            disabled={!previewable || previewQ.isFetching}
            onClick={() => previewQ.refetch()}
          >
            {previewQ.isFetching ? "Running…" : "Run preview"}
          </button>
        </div>

        <div className="fx-banner">
          <strong>Preview runs as you.</strong> Your row and column policy is applied. The
          build runs as the system and will see at least as many rows. At most {PREVIEW_ROWS} rows
          are shown here however big the real result is.
        </div>

        {maskedList.length > 0 && (
          <div className="fx-note fx-note-warn">
            {maskedList.map(([ds, cols]) => {
              // Split on whether the masked column is actually *on screen*.
              // `masked_columns` is per source dataset, so a flow that drops
              // the masked column still reports it — and telling the author
              // "what you see is the mask" about a column that is not in the
              // grid is simply false.
              const shown = cols.filter((c) => (previewQ.data?.columns ?? []).includes(c));
              const hidden = cols.filter((c) => !shown.includes(c));
              return (
                <div key={ds}>
                  {shown.length > 0 && (
                    <div>
                      <span className="mono">{shown.join(", ")}</span> in{" "}
                      <span className="mono">{ds}</span> {shown.length === 1 ? "is" : "are"} masked
                      for you, so the values below are the mask, not the data.{" "}
                      <strong>The build would see the real values</strong> — a total or an average
                      over one of these is wrong here and right there. Saving a pipeline that keeps
                      a masked column is refused for that reason.
                    </div>
                  )}
                  {hidden.length > 0 && (
                    <div>
                      <span className="mono">{ds}</span> has{" "}
                      {hidden.length === 1 ? "a column" : "columns"} you are not shown in full (
                      <span className="mono">{hidden.join(", ")}</span>). This pipeline does not
                      include {hidden.length === 1 ? "it" : "them"}, so nothing here is affected.
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {!previewable ? (
          <div className="fx-note fx-note-warn">
            {issues.length === 1 ? "One step is unfinished" : `${issues.length} steps are unfinished`}:{" "}
            {issues
              .map((x) => `${KINDS[x.node.kind].label} — ${x.issue}`)
              .join("; ")}
          </div>
        ) : !settled || previewQ.isLoading || previewQ.isFetching ? (
          <Spinner label="Running…" />
        ) : previewQ.error ? (
          previewQ.error instanceof ApiError && previewQ.error.status === 400 && refusal ? (
            <div className="fx-note fx-note-bad">{refusal.message}</div>
          ) : (
            <ErrorBox error={previewQ.error} />
          )
        ) : previewQ.data ? (
          <>
            {/* `truncated` is now reachable: the compiled preview asks for one
                row more than it shows, so the server can tell "this is all of
                it" from "this is the first page". It used to be structurally
                false — the LIMIT and the fetch used the same number — and this
                line therefore read "50 rows · 7 columns" for a 56-row dataset
                and "200 rows · 2 columns" for a 5,000-group aggregate, with the
                number looking like the size of the answer. */}
            <div className="result-meta">
              {previewQ.data.truncated ? (
                <>
                  <strong>{truncationNote(previewQ.data.row_count)}</strong> ·{" "}
                </>
              ) : (
                <>
                  {fmtNum(previewQ.data.row_count)} row
                  {previewQ.data.row_count === 1 ? "" : "s"} ·{" "}
                </>
              )}
              {previewQ.data.columns.length} column
              {previewQ.data.columns.length === 1 ? "" : "s"}
              {previewQ.data.truncated && " · the build produces all of them"}
            </div>
            {previewQ.data.columns.length === 0 ? (
              <EmptyState>No columns.</EmptyState>
            ) : (
              <DataTable
                columns={previewQ.data.columns.map((c) => ({
                  label: c,
                  render: (r: Record<string, unknown>) => fmtValue(r[c]),
                }))}
                rows={previewQ.data.rows}
                rowKey={(_r, i) => String(i)}
              />
            )}
          </>
        ) : (
          <EmptyState>Press “Run preview” to see the result of this step.</EmptyState>
        )}
      </div>

      {duplicating && (
        <NameDialog
          title={`Duplicate ${name}`}
          intro={
            <>
              This copies every step into a new pipeline. Pipelines cannot be renamed — everything
              downstream is keyed on the name — so duplicating under the name you want and
              deleting the old one is how a rename is done here.
            </>
          }
          confirmLabel="Duplicate"
          taken={(flowsQ.data ?? []).map((f) => f.name)}
          takenDatasets={(datasetsQ.data ?? []).map((d) => d.name)}
          busy={duplicate.isPending}
          error={duplicate.error}
          onCancel={() => setDuplicating(false)}
          onSubmit={(to) => duplicate.mutate(to)}
        />
      )}

      {ejectOpen && (
        <EjectDialog
          name={name}
          onClose={() => setEjectOpen(false)}
          onDone={() => {
            qc.invalidateQueries({ queryKey: ["flows"] });
            qc.invalidateQueries({ queryKey: ["pipelines"] });
            // Land on the Python tab with the generated file already open.
            navigate(`/pipelines?tab=python&file=${encodeURIComponent(name)}`);
          }}
        />
      )}
    </div>
  );
}

/** Guess a literal type per column from the preview's own values, so a filter
 *  on a number defaults to a number input rather than to text. Only ever a
 *  default: the author can retype it, and the server coerces strictly. */
function typeHints(preview: FlowPreviewResult | undefined): TypeHints {
  const out: TypeHints = {};
  if (!preview) return out;
  for (const c of preview.columns) {
    for (const row of preview.rows) {
      const v = row[c];
      if (v === null || v === undefined) continue;
      let t: FlowLitType | null = null;
      if (typeof v === "number") t = Number.isInteger(v) ? "bigint" : "double";
      else if (typeof v === "boolean") t = "boolean";
      else if (typeof v === "string") t = "string";
      if (t) out[c] = t;
      break;
    }
  }
  return out;
}

// ------------------------------------------------------------------ step card

interface BranchStep {
  node: FlowNode;
  issue: string | null;
  refused: string | null;
}

interface CardHandlers {
  onSelect: (id: string) => void;
  /** Which step's "add a step" menu is open, if any. */
  addAfter: string | null;
  setAddAfter: (id: string | null) => void;
  onPick: (afterId: string, kind: FlowNodeKind) => void;
  onRemove: (id: string) => void;
}

/** The "+ / menu" pair that sits under any step, spine or branch. */
function AddStep({
  afterId,
  h,
}: {
  afterId: string;
  h: CardHandlers;
}) {
  const open = h.addAfter === afterId;
  return (
    <>
      <div className="fx-connector">
        <button
          type="button"
          className="fx-plus"
          onClick={() => h.setAddAfter(open ? null : afterId)}
          title={open ? "Close" : "Add a step here"}
          aria-expanded={open}
        >
          {open ? "\u00d7" : "+"}
        </button>
      </div>
      {open && (
        <div className="fx-menu">
          {KIND_ORDER.map((k) => (
            <button
              key={k}
              type="button"
              className="fx-menu-item"
              // `title` gives the button an accessible name: its label lives in
              // nested <strong>/<em>, which screen readers and the keyboard
              // path do not reliably compose into one.
              title={KINDS[k].action}
              onClick={() => h.onPick(afterId, k)}
            >
              <StepIcon kind={k} />
              <span>
                <strong>{KINDS[k].action}</strong>
                <em>{KINDS[k].blurb}</em>
              </span>
            </button>
          ))}
        </div>
      )}
    </>
  );
}

function StepCard({
  flow,
  node,
  index,
  selected,
  issue,
  refused,
  columns,
  branch,
  h,
}: {
  flow: FlowDef;
  node: FlowNode;
  index: number;
  selected: boolean;
  issue: string | null;
  refused: string | null;
  columns: number;
  branch: BranchStep[] | null;
  h: CardHandlers;
}) {
  const tone = refused ? "bad" : issue ? "todo" : "";
  return (
    <>
      {/* A div rather than a <button>: the card contains its own buttons and a
          whole nested branch, and a button inside a button is invalid. So it
          carries the role and the keyboard handler explicitly. */}
      <div
        className={`fx-card ${tone}${selected ? " selected" : ""}`}
        role="button"
        tabIndex={0}
        aria-pressed={selected}
        onClick={() => h.onSelect(node.id)}
        onKeyDown={(e) => {
          if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            h.onSelect(node.id);
          }
        }}
      >
        <div className="fx-card-top">
          <span className="fx-num">{index + 1}</span>
          <StepIcon kind={node.kind} />
          <span className="fx-card-kind">{KINDS[node.kind].label}</span>
          <span style={{ flex: 1 }} />
          {columns > 0 && <span className="faint">{columns} columns</span>}
          {index > 0 && (
            <button
              type="button"
              className="fx-x"
              title="Remove this step"
              onClick={(e) => {
                e.stopPropagation();
                h.onRemove(node.id);
              }}
            >
              ×
            </button>
          )}
        </div>
        <div className="fx-card-sum">{summarise(flow, node)}</div>
        {refused ? (
          <div className="fx-card-msg bad">{refused}</div>
        ) : issue ? (
          <div className="fx-card-msg todo">{issue}</div>
        ) : null}

        {/* A join's second input is a chain of its own, drawn inside the card
            that consumes it. It gets the same add/remove affordances as the
            spine: without them the server's own advice ("add a rename step to
            one side") would name a gesture the screen does not offer. */}
        {branch && (
          <div className="fx-branch" onClick={(e) => e.stopPropagation()}>
            <div className="fx-branch-label">brings in</div>
            {branch.map((b, bi) => (
              <div key={b.node.id}>
                <div
                  className={`fx-card small ${b.refused ? "bad" : b.issue ? "todo" : ""}`}
                  role="button"
                  tabIndex={0}
                  onClick={() => h.onSelect(b.node.id)}
                  onKeyDown={(e) => {
                    if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
                      e.preventDefault();
                      h.onSelect(b.node.id);
                    }
                  }}
                >
                  <div className="fx-card-top">
                    <StepIcon kind={b.node.kind} />
                    <span className="fx-card-kind">{KINDS[b.node.kind].label}</span>
                    <span style={{ flex: 1 }} />
                    {bi > 0 && (
                      <button
                        type="button"
                        className="fx-x"
                        title="Remove this step"
                        onClick={(e) => {
                          e.stopPropagation();
                          h.onRemove(b.node.id);
                        }}
                      >
                        ×
                      </button>
                    )}
                  </div>
                  <div className="fx-card-sum">{summarise(flow, b.node)}</div>
                  {(b.refused || b.issue) && (
                    <div className={`fx-card-msg ${b.refused ? "bad" : "todo"}`}>
                      {b.refused ?? b.issue}
                    </div>
                  )}
                </div>
                <AddStep afterId={b.node.id} h={h} />
              </div>
            ))}
          </div>
        )}
      </div>

      <AddStep afterId={node.id} h={h} />
    </>
  );
}

/** Hand-rolled glyphs. This repo draws its own SVG rather than adding a
 *  dependency, and a step list wants a shape you can scan, not a word. */
function StepIcon({ kind }: { kind: FlowNodeKind }) {
  const paths: Record<FlowNodeKind, JSX.Element> = {
    source: (
      <>
        <ellipse cx="8" cy="4.2" rx="5.5" ry="2.2" />
        <path d="M2.5 4.2v7.6c0 1.2 2.5 2.2 5.5 2.2s5.5-1 5.5-2.2V4.2" />
      </>
    ),
    filter: <path d="M2 3h12l-4.6 5.4v4.4l-2.8 1.4V8.4z" />,
    select: (
      <>
        <rect x="2" y="3" width="3.4" height="10" />
        <rect x="6.3" y="3" width="3.4" height="10" />
        <rect x="10.6" y="3" width="3.4" height="10" />
      </>
    ),
    rename: (
      <>
        <path d="M2 8h5" />
        <path d="M9 5.5l2.5 2.5L9 10.5" />
        <path d="M11.5 8H14" />
      </>
    ),
    derive: (
      <>
        <rect x="2.5" y="2.5" width="11" height="11" rx="2" />
        <path d="M8 5.5v5M5.5 8h5" />
      </>
    ),
    cast: (
      <>
        <path d="M2 5.5h9l-2-2M14 10.5H5l2 2" />
      </>
    ),
    join: (
      <>
        <circle cx="6" cy="8" r="4" />
        <circle cx="10" cy="8" r="4" />
      </>
    ),
    aggregate: (
      <>
        <rect x="2.5" y="9" width="2.6" height="4.5" />
        <rect x="6.7" y="5.5" width="2.6" height="8" />
        <rect x="10.9" y="7.5" width="2.6" height="6" />
      </>
    ),
    dedupe: (
      <>
        <rect x="2.5" y="2.5" width="8" height="8" rx="1.5" />
        <path d="M5.5 13.5h8V5.5" />
      </>
    ),
    sort: (
      <>
        <path d="M2.5 4h11M2.5 8h7.5M2.5 12h4" />
      </>
    ),
  };
  return (
    <svg className="fx-icon" viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
      <g fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round">
        {paths[kind]}
      </g>
    </svg>
  );
}

// ------------------------------------------------------------------ eject

/**
 * The one-way door, said before the click rather than after it.
 *
 * There is no import in the other direction and there never will be for v1:
 * re-parsing Python into an IR is a Python-source analyser, unbounded in scope,
 * and wrong the first time someone writes a helper function. So the modal is
 * type-to-confirm, and the wording does not soften it.
 */
function EjectDialog({
  name,
  onClose,
  onDone,
}: {
  name: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [typed, setTyped] = useState("");

  const eject = useMutation<FlowEjectResult, unknown, void>({
    mutationFn: () => api.post<FlowEjectResult>(`${API}/flows/${encodeURIComponent(name)}/eject`),
    onSuccess: (r) => {
      if (!r.collect_error) onDone();
    },
  });

  return (
    <Modal label="Open this pipeline in Python?" onClose={onClose} width={540}>
      <div className="fx-modal">
        <h2>Open this pipeline in Python?</h2>
        <p>
          <strong>This is one way.</strong> Laurelin will write{" "}
          <span className="mono">pipelines/{name}.py</span> containing the SQL these steps
          generate, and then delete the visual version. The step-by-step builder cannot open it
          again — from then on you edit it as Python on the <strong>Python</strong> tab.
        </p>
        <p className="faint">
          There is no un-eject. If you are not sure, press Cancel and keep building here; you can
          always look at the SQL with <strong>Show SQL</strong> without giving anything up.
        </p>
        <div className="field">
          <label htmlFor="fx-eject-confirm">
            Type <span className="mono">{name}</span> to confirm
          </label>
          <input
            id="fx-eject-confirm"
            className="fx-in fx-wide"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={name}
          />
        </div>
        {eject.error != null && <ErrorBox error={eject.error} />}
        {eject.data?.collect_error && (
          <>
            <div className="fx-note fx-note-bad">
              The Python file was written, but the workspace will not collect.
            </div>
            <FailureNote failure={eject.data.collect_error} />
          </>
        )}
        <div className="fx-modal-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="danger"
            disabled={typed !== name || eject.isPending}
            onClick={() => eject.mutate()}
          >
            {eject.isPending ? "Converting…" : "Convert to Python, permanently"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
