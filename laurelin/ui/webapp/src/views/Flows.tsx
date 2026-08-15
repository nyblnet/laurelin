// Flows — build a pipeline without writing code.
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

import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
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
} from "../types";
import {
  Badge,
  DataTable,
  EmptyState,
  ErrorBox,
  FailureNote,
  PageHeader,
  Spinner,
  fmtNum,
  fmtValue,
} from "../ui";
import { ImportedPipelinesNotice } from "./ImportedPipelinesNotice";
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
  spine,
  stepIssue,
  summarise,
  updateParams,
} from "./flow/model";
import { KINDS, KIND_ORDER } from "./flow/vocab";
import { FLOW_STYLES } from "./flow/styles";

const NAME_RE = /^[a-z][a-z0-9_]*$/;
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
  const navigate = useNavigate();

  const flowsQ = useQuery({
    queryKey: ["flows"],
    queryFn: () => api.get<FlowListEntry[]>(`${API}/flows`),
  });

  const [naming, setNaming] = useState(false);

  const flows = flowsQ.data ?? [];

  return (
    <div>
      <PageHeader
        title="Flows"
        subtitle="Build a pipeline step by step. No code — every flow becomes a real transform on the same build, lineage and permissions as everything else."
        actions={
          <button type="button" className="primary" onClick={() => setNaming(true)}>
            + New flow
          </button>
        }
      />

      <ImportedPipelinesNotice />

      {flowsQ.isLoading ? (
        <Spinner />
      ) : flowsQ.error ? (
        <ErrorBox error={flowsQ.error} />
      ) : flows.length === 0 ? (
        <div className="fx-onboard">
          <h2>Build a pipeline without writing code.</h2>
          <p>
            Pick a dataset, then add steps: filter rows, combine two datasets,
            group and summarise. Every step shows you the result as you go.
          </p>
          <p className="faint">
            What you build is a normal transform. It appears on{" "}
            <Link to="/pipeline">Pipeline</Link> with its lineage, it can be
            scheduled, and it obeys the same permissions as everything else.
          </p>
          <button type="button" className="primary" onClick={() => setNaming(true)}>
            Start your first flow
          </button>
        </div>
      ) : (
        <div className="cards fx-cards">
          {flows.map((f) => (
            <div
              key={f.name}
              className="card clickable"
              onClick={() => navigate(`/flows/${encodeURIComponent(f.name)}`)}
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
                  ? "This flow will not load. Open it to see what to fix."
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
              A flow builds one dataset, and the flow and the dataset share a name. Pick something
              you would be happy to see on a dashboard — it cannot be renamed afterwards, because
              everything downstream is keyed on it.
            </>
          }
          confirmLabel="Start building"
          taken={flows.map((f) => f.name)}
          onCancel={() => setNaming(false)}
          onSubmit={(name) => {
            setNaming(false);
            // Nothing is written until the first Save. An empty flow (one
            // source, no dataset yet) is structurally valid, so the URL is
            // real and refreshable from the first moment.
            navigate(`/flows/${encodeURIComponent(name)}?new=1`);
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
  const isNew = new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("new") === "1";

  const [draft, setDraft] = useState<FlowDef | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [dirty, setDirty] = useState(false);
  const [saveResult, setSaveResult] = useState<FlowWriteResult | null>(null);
  const [showSql, setShowSql] = useState(false);
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
      setDraft(emptyFlow(name, auth.user?.username ?? ""));
      setSelected("s1");
      setDirty(true);
    }
  }, [isNew, name, draft, auth.user?.username]);

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
      if (isNew) navigate(`/flows/${encodeURIComponent(name)}`, { replace: true });
    },
  });

  const del = useMutation({
    mutationFn: () => api.del(`${API}/flows/${encodeURIComponent(name)}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["flows"] });
      navigate("/flows");
    },
  });

  const build = useMutation<Build, unknown, void>({
    mutationFn: () => api.post<Build>(`${API}/builds`, { targets: [name] }),
    onSuccess: (b) => {
      setBuiltId(b.id);
      qc.invalidateQueries({ queryKey: ["builds"] });
    },
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
      navigate(`/flows/${encodeURIComponent(r.name)}`);
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
      const where = consumerOf(draft, id) ? "this side of the combine" : "this flow";
      window.alert(
        `${where[0].toUpperCase()}${where.slice(1)} has to start somewhere. ` +
          "Change the dataset instead of removing this step — or remove the whole " +
          "step that uses it.",
      );
      return;
    }
    if (node.kind === "join" && !window.confirm("Remove this combine step and the dataset it brings in?")) return;
    const next = removeStep(draft, id);
    edit(next);
    setSelected(next.terminal);
  }

  // --------------------------------------------------------------- render

  if (!name) return <EmptyState>No flow named.</EmptyState>;
  if (flowQ.isLoading && !draft) return <Spinner label="Opening flow…" />;
  if (flowQ.error && !draft) {
    const err = flowQ.error;
    if (err instanceof ApiError && err.status === 404) {
      return (
        <div>
          <PageHeader title={name} subtitle="No such flow" />
          <EmptyState>
            There is no flow called {name}. <Link to="/flows">Back to Flows</Link>.
          </EmptyState>
        </div>
      );
    }
    return <ErrorBox error={err} />;
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

  const blockedByWorkspace = save.error instanceof ApiError && save.error.status === 409;
  // `--lock-pipelines` covers flows, deliberately: the flag's contract is "no
  // authoring on this server", and a flow authors a transform that runs as the
  // system and reads datasets. A bare "403" would read as "you personally are
  // not allowed", which is the wrong diagnosis and sends the author to an
  // administrator who cannot help without a restart.
  const authoringLocked = save.error instanceof ApiError && save.error.status === 403;
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
          <Link to="/flows" className="fx-back">
            ← Flows
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
            onClick={() => save.mutate(draft)}
          >
            {save.isPending ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            disabled={dirty || build.isPending}
            title={dirty ? "Save first — a build runs what is saved, not what is on screen." : undefined}
            onClick={() => build.mutate()}
          >
            {build.isPending ? "Starting…" : "Build now"}
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
            title="Copy these steps into a new flow under a new name. Flows cannot be renamed."
          >
            Duplicate…
          </button>
          <button
            type="button"
            onClick={() => setEjectOpen(true)}
            disabled={dirty}
            /* No longer gated on `boundValues`. It used to be, because
               `TransformSpec` had no `params` field, so every flow carrying a
               single filter constant — which is to say every flow that filters
               anything — was excluded from the advertised escape hatch, and the
               only explanation lived in this tooltip. `sql_transform` now takes
               bound values, so the generated pipeline binds them exactly as the
               flow did and nothing is written into the SQL text. */
            title={dirty ? "Save first." : "Convert this flow into a Python pipeline. One way."}
          >
            Open in Python…
          </button>
          <button
            type="button"
            className="danger"
            onClick={() => {
              if (window.confirm(`Delete the flow ${name}? The dataset it built is kept, and so is its lineage.`)) {
                del.mutate();
              }
            }}
          >
            Delete
          </button>
        </div>
      </div>

      {/* ------------------------------------------------ header-level notes */}

      {restricted && (
        <div className="fx-note fx-note-gov">
          A dataset this flow reads is restricted, so <strong>{draft.output}</strong> will be
          readable only by <strong>{draft.author || "its author"}</strong> until an administrator
          grants access to someone else.
        </div>
      )}
      {refusal && (
        <div className="fx-note fx-note-bad">
          <strong>This flow will not run yet.</strong> {refusal.message}
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
          <strong>This server does not accept pipeline changes.</strong> Flows, like Python
          transforms, can only be edited where authoring is enabled — an operator started this
          server with authoring locked. Nothing you have on screen is lost; it just cannot be
          saved here. The flows that already exist still build and still run.
        </div>
      ) : blockedByWorkspace ? (
        <div className="fx-note fx-note-bad">
          <strong>Nothing can be saved or built in this workspace right now.</strong> Another
          pipeline file will not load, and Laurelin has to read them all together to work out
          which pipeline produces which dataset. That includes this one, so saving a repair is
          blocked too.
          <div style={{ marginTop: 6 }}>
            The server said: <em>{(save.error as ApiError).detail}</em> Someone with access to the
            workspace files has to fix or remove that file — or, if the broken one is a flow, it
            can be deleted from <Link to="/flows">Flows</Link>.
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
        <div className="fx-note fx-note-ok">
          Build started. Watch it on <Link to="/pipeline">Pipeline</Link>.
        </div>
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
            Generated from this flow — not editable. Edit the steps, not the SQL.
          </div>
          {dirty ? (
            <div className="faint">Save first: this shows the SQL of the saved flow.</div>
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
                stay separate if you convert this flow to a Python pipeline.
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
            <label>What is this pipeline for?</label>
            <input
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
                      over one of these is wrong here and right there. Saving a flow that keeps a
                      masked column is refused for that reason.
                    </div>
                  )}
                  {hidden.length > 0 && (
                    <div>
                      <span className="mono">{ds}</span> has{" "}
                      {hidden.length === 1 ? "a column" : "columns"} you are not shown in full (
                      <span className="mono">{hidden.join(", ")}</span>). This flow does not
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
                  <strong>first {fmtNum(previewQ.data.row_count)} rows</strong> of a larger
                  result ·{" "}
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
              This copies every step into a new flow. Flows cannot be renamed — everything
              downstream is keyed on the name — so duplicating under the name you want and
              deleting the old one is how a rename is done here.
            </>
          }
          confirmLabel="Duplicate"
          taken={(flowsQ.data ?? []).map((f) => f.name)}
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
            navigate("/transforms");
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

// ------------------------------------------------------------------ naming

/**
 * Name a flow. Used for "New flow" and for "Duplicate".
 *
 * Not `window.prompt`, which is what the Transforms screen uses for the same
 * job. A prompt cannot show the rule, cannot show that a name is already
 * taken, and cannot say the one thing that matters here — that a flow's name is
 * the name of the dataset it produces and there is no rename afterwards,
 * because `spec.name` is the primary key of lineage and nothing in this tree
 * deletes lineage. That is too much to learn from a failed submit, and this
 * screen's audience is precisely the one that should not have to.
 */
function NameDialog({
  title,
  intro,
  confirmLabel,
  taken,
  busy,
  error,
  onCancel,
  onSubmit,
}: {
  title: string;
  intro: ReactNode;
  confirmLabel: string;
  taken: string[];
  busy?: boolean;
  error?: unknown;
  onCancel: () => void;
  onSubmit: (name: string) => void;
}) {
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => inputRef.current?.focus(), []);

  const name = value.trim();
  const problem = !name
    ? null
    : !NAME_RE.test(name)
      ? "Use lowercase letters, digits and underscores, starting with a letter — for example late_orders."
      : taken.includes(name)
        ? `There is already a flow called ${name}.`
        : null;
  const ok = !!name && !problem;

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal fx-modal" onClick={(e) => e.stopPropagation()}>
        <h2>{title}</h2>
        <p>{intro}</p>
        <div className="field">
          <label>Name</label>
          <input
            ref={inputRef}
            className="fx-in fx-wide"
            value={value}
            placeholder="late_orders"
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && ok && !busy) onSubmit(name);
            }}
          />
          {problem ? (
            <div className="hint bad">{problem}</div>
          ) : (
            <div className="hint">
              Lowercase letters, digits and underscores. This is also the name of the dataset it
              produces, and it cannot be changed later.
            </div>
          )}
        </div>
        {error != null && <ErrorBox error={error} />}
        <div className="fx-modal-actions">
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            disabled={!ok || busy}
            onClick={() => onSubmit(name)}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
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
  const inputRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => inputRef.current?.focus(), []);

  const eject = useMutation<FlowEjectResult, unknown, void>({
    mutationFn: () => api.post<FlowEjectResult>(`${API}/flows/${encodeURIComponent(name)}/eject`),
    onSuccess: (r) => {
      if (!r.collect_error) onDone();
    },
  });

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal fx-modal" onClick={(e) => e.stopPropagation()}>
        <h2>Open this flow in Python?</h2>
        <p>
          <strong>This is one way.</strong> Laurelin will write{" "}
          <span className="mono">pipelines/{name}.py</span> containing the SQL this flow
          generates, and then delete the flow. The step-by-step builder cannot open it again —
          from then on you edit it as Python on the <strong>Transforms</strong> screen.
        </p>
        <p className="faint">
          There is no un-eject. If you are not sure, press Cancel and keep building here; you can
          always look at the SQL with <strong>Show SQL</strong> without giving anything up.
        </p>
        <div className="field">
          <label>
            Type <span className="mono">{name}</span> to confirm
          </label>
          <input
            ref={inputRef}
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
    </div>
  );
}
