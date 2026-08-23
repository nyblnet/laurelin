import { useMemo, useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { API, api } from "../api";
import { useAuth } from "../auth";
import type {
  Build,
  BuildStatus,
  ExpectationResult,
  LineageGraph,
  LineageNode,
  TransformSummary,
} from "../types";
import {
  Badge,
  Column,
  DataTable,
  EmptyState,
  ErrorBox,
  FailureBadge,
  FailureNote,
  PageHeader,
  Spinner,
  Withheld,
  fmtNum,
  fmtTime,
} from "../ui";
import { ImportedPipelinesNotice } from "./ImportedPipelinesNotice";

// ------------------------------------------------------------------ helpers

function statusTone(status: BuildStatus): "green" | "red" | "blue" {
  if (status === "succeeded") return "green";
  if (status === "failed") return "red";
  return "blue"; // running | pending
}

function truncate(s: string, max = 22): string {
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

function shortId(id: string): string {
  return id.length > 12 ? id.slice(0, 12) : id;
}

// ------------------------------------------------------------- lineage graph

const LAYER_W = 190;
const ROW_H = 64;
const MARGIN_X = 40;
const MARGIN_Y = 32;
const NODE_W = 140;
const NODE_H = 40;

interface Placed {
  node: LineageNode;
  layer: number;
  x: number;
  y: number;
}

/**
 * Assign each node a layer = longest path (in edges) from any source node.
 * Cycle-safe: a `visiting` set turns back-edges into a 0-contribution so we
 * never recurse infinitely. Results are memoized per node.
 */
function computeLayers(graph: LineageGraph): Map<string, number> {
  const incoming = new Map<string, string[]>();
  const ids = new Set(graph.nodes.map((n) => n.id));
  for (const n of graph.nodes) incoming.set(n.id, []);
  for (const e of graph.edges) {
    if (ids.has(e.from) && ids.has(e.to)) {
      incoming.get(e.to)!.push(e.from);
    }
  }

  const memo = new Map<string, number>();
  const visiting = new Set<string>();

  const layerOf = (id: string): number => {
    const cached = memo.get(id);
    if (cached !== undefined) return cached;
    if (visiting.has(id)) return 0; // back-edge: contribute nothing
    visiting.add(id);
    let best = 0;
    for (const src of incoming.get(id) ?? []) {
      best = Math.max(best, layerOf(src) + 1);
    }
    visiting.delete(id);
    memo.set(id, best);
    return best;
  };

  for (const n of graph.nodes) layerOf(n.id);
  return memo;
}

function layout(graph: LineageGraph): {
  placed: Placed[];
  byId: Map<string, Placed>;
  width: number;
  height: number;
} {
  const layers = computeLayers(graph);
  // Group nodes by layer, preserving input order for stable stacking.
  const byLayer = new Map<number, LineageNode[]>();
  let maxLayer = 0;
  for (const n of graph.nodes) {
    const l = layers.get(n.id) ?? 0;
    maxLayer = Math.max(maxLayer, l);
    if (!byLayer.has(l)) byLayer.set(l, []);
    byLayer.get(l)!.push(n);
  }

  let maxRows = 0;
  for (const nodes of byLayer.values()) maxRows = Math.max(maxRows, nodes.length);

  const placed: Placed[] = [];
  const byId = new Map<string, Placed>();
  for (let l = 0; l <= maxLayer; l++) {
    const nodes = byLayer.get(l) ?? [];
    // Center this layer's stack vertically within the tallest layer.
    const offset = ((maxRows - nodes.length) * ROW_H) / 2;
    nodes.forEach((node, i) => {
      const x = MARGIN_X + l * LAYER_W;
      const y = MARGIN_Y + offset + i * ROW_H;
      const p: Placed = { node, layer: l, x, y };
      placed.push(p);
      byId.set(node.id, p);
    });
  }

  const width = MARGIN_X * 2 + maxLayer * LAYER_W + NODE_W;
  const height = MARGIN_Y * 2 + Math.max(1, maxRows) * ROW_H;
  return { placed, byId, width, height };
}

function LineageGraphView({ graph }: { graph: LineageGraph }) {
  const { placed, byId, width, height } = useMemo(() => layout(graph), [graph]);

  if (graph.nodes.length === 0) {
    return <EmptyState>No lineage yet — run a build.</EmptyState>;
  }

  return (
    <div className="lineage">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width={width}
        height={height}
        role="img"
        aria-label="Transform lineage graph"
      >
        <defs>
          <marker
            id="lineage-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="7"
            markerHeight="7"
            orient="auto-start-reverse"
          >
            <path d="M0,0 L10,5 L0,10 z" fill="var(--border-2)" />
          </marker>
        </defs>

        {graph.edges.map((e, i) => {
          const from = byId.get(e.from);
          const to = byId.get(e.to);
          if (!from || !to) return null;
          const x1 = from.x + NODE_W;
          const y1 = from.y + NODE_H / 2;
          const x2 = to.x;
          const y2 = to.y + NODE_H / 2;
          const dx = Math.max(30, (x2 - x1) / 2);
          const d = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
          return (
            <path
              key={i}
              className="edge"
              d={d}
              markerEnd="url(#lineage-arrow)"
            />
          );
        })}

        {placed.map((p) => {
          const isTransform = p.node.type === "transform";
          return (
            <g key={p.node.id}>
              <rect
                className={isTransform ? "node-transform" : "node-dataset"}
                x={p.x}
                y={p.y}
                width={NODE_W}
                height={NODE_H}
                rx={isTransform ? 16 : 8}
              />
              <text
                className="node-label"
                x={p.x + NODE_W / 2}
                y={p.y + NODE_H / 2}
                textAnchor="middle"
                dominantBaseline="central"
              >
                {truncate(p.node.id, 18)}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

// A transform with no declared expectations shows nothing rather than "0/0" —
// absence of checks is a different statement from checks that all passed.
//
// And "you were not sent them" is a third statement again. An expectation's
// `message` is prose an editor wrote in a pipeline file, so the whole list is
// OPERATIONAL and a viewer receives no key — which would render as the "no
// checks declared" dash and quietly misinform them.
function Expectations({ results }: { results?: ExpectationResult[] }) {
  if (results === undefined) {
    return (
      <Withheld
        what="A transform's expectation results"
        role="editor"
        why="Each carries a message written in the pipeline file."
      />
    );
  }
  if (results.length === 0) return <span className="dim">—</span>;
  const failed = results.filter((r) => !r.passed);
  if (failed.length === 0) {
    return <Badge tone="green">{results.length} passed</Badge>;
  }
  const blocking = failed.some((r) => r.severity === "error");
  return (
    <span title={failed.map((r) => r.message).join("\n")}>
      <Badge tone={blocking ? "red" : "gold"}>
        {failed.length} of {results.length} failed
      </Badge>
    </span>
  );
}

// -------------------------------------------------------------- build card

function BuildCard({ build }: { build: Build }) {
  const [open, setOpen] = useState(false);

  const taskColumns: Column<Build["tasks"][number]>[] = [
    {
      label: "Transform",
      className: "mono",
      render: (t) => t.transform_name,
    },
    {
      label: "Status",
      render: (t) => <Badge tone={statusTone(t.status)}>{t.status}</Badge>,
    },
    {
      label: "Rows",
      className: "num",
      render: (t) => fmtNum(t.rows_written),
    },
    {
      label: "Version",
      className: "mono",
      render: (t) => (t.output_version == null ? "—" : `v${t.output_version}`),
    },
    {
      label: "Expectations",
      render: (t) => <Expectations results={t.expectations} />,
    },
    {
      // R1: this column used to print f"{type(exc).__name__}: {exc}" — whatever
      // an arbitrary library said while a transform ran, on a viewer-readable
      // route. It now prints Laurelin's own classification; the driver's words
      // exist only in the server log, findable by the ref in the tooltip.
      label: "Failure",
      render: (t) => (t.failure ? <FailureBadge failure={t.failure} /> : ""),
    },
  ];

  // Per-task detail is only worth its space when it says more than the
  // build-level record already did — which means when the reader got the wide
  // projection. A viewer's copy of both is `{code, subject}`, so printing them
  // one under the other just says the same sentence twice.
  const failedTasks = build.tasks.filter((t) => t.failure?.detail_ref);

  return (
    <div className="card">
      <div
        className="clickable"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          cursor: "pointer",
        }}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="dim" style={{ width: 12 }}>
          {open ? "▾" : "▸"}
        </span>
        <span className="mono">{shortId(build.id)}</span>
        <Badge tone={statusTone(build.status)}>{build.status}</Badge>
        <span className="dim">{fmtTime(build.started_at)}</span>
        <span className="dim" style={{ marginLeft: "auto" }}>
          {build.targets.length > 0 ? build.targets.join(", ") : "all targets"}
        </span>
      </div>

      {build.failure && <FailureNote failure={build.failure} />}

      {open && (
        <div style={{ marginTop: 12 }}>
          {/* The build-level record says *that* it failed; the per-task one
              says which step and carries the log reference. Both, in that
              order, so an operator reads the cause without expanding twice. */}
          {failedTasks.map((t) => (
            <div key={t.transform_name} style={{ marginBottom: 8 }}>
              <div className="mono dim" style={{ fontSize: 12 }}>
                {t.transform_name}
              </div>
              <FailureNote failure={t.failure!} />
            </div>
          ))}
          {build.tasks.length === 0 ? (
            <EmptyState>No tasks.</EmptyState>
          ) : (
            <DataTable
              columns={taskColumns}
              rows={build.tasks}
              rowKey={(t, i) => `${t.transform_name}-${i}`}
            />
          )}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------- view

export function PipelineView() {
  const auth = useAuth();
  const qc = useQueryClient();

  const lineageQ = useQuery({
    queryKey: ["lineage"],
    queryFn: () => api.get<LineageGraph>(`${API}/lineage`),
  });

  const transformsQ = useQuery({
    queryKey: ["transforms"],
    queryFn: () => api.get<TransformSummary[]>(`${API}/transforms`),
  });

  const buildsQ = useQuery({
    queryKey: ["builds"],
    queryFn: () => api.get<Build[]>(`${API}/builds`),
    staleTime: 15_000,
    // Builds run async on the server: poll while one is in flight so the
    // history converges without a manual refresh.
    refetchInterval: (query) =>
      query.state.data?.some((b) => b.status === "pending" || b.status === "running")
        ? 2000
        : false,
  });

  const runBuild = useMutation({
    mutationFn: () => api.post<Build>(`${API}/builds`, {}),
    onSuccess: () => {
      // The POST returns a pending build immediately; polling picks it up.
      qc.invalidateQueries({ queryKey: ["builds"] });
      qc.invalidateQueries({ queryKey: ["lineage"] });
      qc.invalidateQueries({ queryKey: ["datasets"] });
    },
  });

  const canEdit = auth.can("editor");
  const buildInFlight =
    runBuild.isPending ||
    (buildsQ.data?.some((b) => b.status === "pending" || b.status === "running") ?? false);

  const actions = canEdit ? (
    <button
      className="primary"
      disabled={buildInFlight}
      onClick={() => runBuild.mutate()}
    >
      {buildInFlight ? "Building…" : "Run build"}
    </button>
  ) : (
    <span className="dim">Viewer — builds are read-only.</span>
  );

  const transformColumns: Column<TransformSummary>[] = [
    { label: "Name", className: "mono", render: (t) => t.name },
    {
      label: "Kind",
      render: (t) => (
        // `flow` gets its own tone rather than sharing `python`'s. The
        // distinction a reader needs from this column is what they would have
        // to be able to *do* to have written the row: `python` and `sql` are
        // authored code, `flow` is a declarative artifact that is never
        // exec'd, and that is the whole point of the kind existing.
        <Badge tone={t.kind === "sql" ? "gold" : t.kind === "flow" ? "green" : "blue"}>
          {t.kind}
        </Badge>
      ),
    },
    {
      label: "Inputs",
      className: "dim",
      render: (t) => (t.inputs.length > 0 ? t.inputs.join(", ") : "—"),
    },
    { label: "Output", className: "mono", render: (t) => t.output },
  ];

  return (
    <div>
      <PageHeader
        title="Pipeline"
        subtitle="Transform DAG and build history."
        actions={actions}
      />

      <ImportedPipelinesNotice />

      {/* The lock posture, said where builders look. Builds themselves are
          unaffected — the lock closes *authoring* Python, not running what
          exists on disk. */}
      {auth.pipelinesLocked && auth.can("editor") && (
        <div className="withheld-box">
          <div className="withheld-head">Python authoring is locked on this server</div>
          <p>
            Pipeline files are managed on disk (<code>--lock-pipelines</code>); existing
            transforms still build. Flows and Explore remain available for authoring without
            code.
          </p>
        </div>
      )}

      {runBuild.isError && <ErrorBox error={runBuild.error} />}

      {/* ----------------------------------------------------- lineage */}
      <section style={{ marginTop: 16 }}>
        <h2>Lineage</h2>
        {lineageQ.isLoading ? (
          <Spinner />
        ) : lineageQ.isError ? (
          <ErrorBox error={lineageQ.error} />
        ) : (
          <LineageGraphView graph={lineageQ.data!} />
        )}
      </section>

      {/* -------------------------------------------------- transforms */}
      <section style={{ marginTop: 24 }}>
        <h2>Transforms</h2>
        {transformsQ.isLoading ? (
          <Spinner />
        ) : transformsQ.isError ? (
          <ErrorBox error={transformsQ.error} />
        ) : transformsQ.data!.length === 0 ? (
          <EmptyState>No transforms defined.</EmptyState>
        ) : (
          <DataTable
            columns={transformColumns}
            rows={transformsQ.data!}
            rowKey={(t) => t.name}
          />
        )}
      </section>

      {/* ------------------------------------------------ build history */}
      <section style={{ marginTop: 24 }}>
        <h2>Build history</h2>
        {buildsQ.isLoading ? (
          <Spinner />
        ) : buildsQ.isError ? (
          <ErrorBox error={buildsQ.error} />
        ) : buildsQ.data!.length === 0 ? (
          <EmptyState>No builds yet.</EmptyState>
        ) : (
          <div style={{ display: "grid", gap: 10, marginTop: 8 }}>
            {/* The API returns builds newest-first (ORDER BY rowid DESC). */}
            {buildsQ.data!.map((b) => (
              <BuildCard key={b.id} build={b} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
