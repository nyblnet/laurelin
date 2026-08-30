import { useState } from "react";
import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../api";
import { useAuth } from "../auth";
import type { ObjectTypeDef, ObjectTypeDetail } from "../types";
import { Badge, EmptyState, ErrorBox, PageHeader, Spinner } from "../ui";
import { ObjectBrowser } from "./ontology/ObjectBrowser";
import { EditLogPanel } from "./ontology/EditLogPanel";
import { WritebackPanel } from "./ontology/WritebackPanel";

/** Index: object types as clickable cards. */
function ObjectTypeList() {
  const navigate = useNavigate();
  const q = useQuery({
    queryKey: ["object-types"],
    queryFn: () => api.get<ObjectTypeDef[]>(`${API}/ontology/object-types`),
  });

  return (
    <div>
      <PageHeader title="Ontology" subtitle="Object types, links, and actions" />
      {q.isLoading && <Spinner />}
      {q.isError && <ErrorBox error={q.error} />}
      {q.data &&
        (q.data.length === 0 ? (
          <EmptyState>No object types defined.</EmptyState>
        ) : (
          <div className="cards">
            {q.data.map((t) => (
              <div
                key={t.api_name}
                className="card clickable"
                onClick={() => navigate(`/ontology/${t.api_name}`)}
              >
                <div className="card-title">{t.display_name || t.api_name}</div>
                {t.description && (
                  <div className="dim" style={{ fontSize: 12.5, margin: "4px 0 8px" }}>
                    {t.description}
                  </div>
                )}
                <div className="faint" style={{ fontSize: 12 }}>
                  {Object.keys(t.properties).length} props · {t.backing_dataset}
                </div>
              </div>
            ))}
          </div>
        ))}
    </div>
  );
}

/** Per-type page: header + object browser/detail. */
function ObjectTypePage() {
  const { type = "" } = useParams();
  // A fold in flight makes a rebuild pointless — the fold writes a new dataset
  // version and rebuilds the store itself at the end — so the two panels share
  // one busy flag rather than racing.
  const [folding, setFolding] = useState(false);
  const q = useQuery({
    queryKey: ["object-type", type],
    queryFn: () =>
      api.get<ObjectTypeDetail>(`${API}/ontology/object-types/${type}`),
  });

  return (
    <div>
      <PageHeader
        title={q.data ? q.data.display_name || q.data.api_name : type}
        subtitle={q.data?.description || undefined}
        actions={<Link to="/ontology">← All types</Link>}
      />
      {q.isLoading && <Spinner />}
      {q.isError && <ErrorBox error={q.error} />}
      {q.data && (
        <>
          {/* The order is the story of the page: what the store knows, then how
              to make it permanent, then what that permanence lets you throw
              away, then the objects themselves. */}
          <ObjectStoreControl type={q.data} busy={folding} />
          <WritebackPanel type={q.data} onBusyChange={setFolding} />
          <EditLogPanel type={q.data} />
          <ObjectBrowser detail={q.data} />
        </>
      )}
    </div>
  );
}

/**
 * Build or drop this type's materialized object store.
 *
 * Materializing makes key lookups constant-time and search sub-linear, at the
 * cost of storage and a refresh — worth it for entities, wasteful for
 * high-volume events, so it's opt-in per type. Health is shown because a store
 * that isn't current is silently bypassed in favour of a full scan: without
 * this, "why did this get slow again" has no visible answer.
 *
 * There are three ways of not being current, with three different remedies, so
 * they get three different sentences. Behind by N edits catches up on the next
 * write (catch-up runs on the write path and on an explicit rebuild, never on a
 * read — a read that writes breaks read-only replicas). A new dataset version
 * can rewrite any row and renumbers every ordinal, so no incremental delta
 * expresses it and only a rebuild does; a changed object-type definition is the
 * same story for the same reason.
 *
 * The lag hint here used to be worded to be true in either case, because it had
 * to be: a store that was a version behind *and* held unapplied edits reported
 * lag > 0 and nothing else, while catch-up bailed on the version mismatch
 * before replaying anything — so the number climbed 1, 2, 3 with every edit and
 * no write ever cleared it. The payload now carries the built-at dataset
 * version alongside the current one, and `stale_version` / `stale_definition`
 * are the verdict, so each state can name its own remedy instead of hedging.
 */
function ObjectStoreControl({
  type,
  busy = false,
}: {
  type: ObjectTypeDetail;
  busy?: boolean;
}) {
  const qc = useQueryClient();
  const { user } = useAuth();
  const canEdit = user?.role === "editor" || user?.role === "admin";
  const index = type.index ?? {
    indexed: false,
    fresh: false,
    objects: 0,
    lag: 0,
    store: null,
    applied_seq: 0,
    dataset_version: null,
    current_dataset_version: null,
    stale_version: null,
    stale_definition: null,
  };

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["object-type", type.api_name] });

  const build = useMutation({
    mutationFn: () =>
      api.post<{ objects: number; state: unknown }>(
        `${API}/ontology/object-types/${type.api_name}/index`,
        {},
      ),
    onSuccess: invalidate,
  });
  const drop = useMutation({
    mutationFn: () => api.del(`${API}/ontology/object-types/${type.api_name}/index`),
    onSuccess: invalidate,
  });

  // Null, not zero: the counters describe the *shared* materialization, so the
  // API withholds them from a caller the backing dataset's policy narrows —
  // otherwise a tenant seeing three of six objects is told there are six. The
  // badge drops the number rather than inventing one.
  const objects = index.objects;
  const lag = index.lag ?? 0;
  // Order matters: a stale version *also* accumulates lag, and "behind by 3
  // edits" is the one reading that promises a fix which never arrives. The
  // rebuild-only states are checked first so the number never speaks over them.
  const needsRebuild = index.stale_version || index.stale_definition;
  const status = !index.indexed
    ? { tone: "neutral" as const, text: "Not built" }
    : index.fresh
      ? {
          tone: "green" as const,
          text: objects === null ? "Live" : `Live · ${objects.toLocaleString()} objects`,
        }
      : needsRebuild
        ? {
            tone: "gold" as const,
            text: index.stale_version
              ? "Stale — the dataset changed, rebuild to use it"
              : "Stale — the object type changed, rebuild to use it",
          }
        : lag > 0
          ? {
              tone: "gold" as const,
              text: `Behind by ${lag} edit${lag === 1 ? "" : "s"}`,
            }
          : {
              tone: "gold" as const,
              text: "Stale — rebuild to use it",
            };

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="toolbar" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <label style={{ margin: 0 }}>Object store</label>
          <Badge tone={status.tone}>
            <span title={index.applied_seq === null ? undefined
              : `Applied up to edit ${index.applied_seq}`}>
              {status.text}
            </span>
          </Badge>
          {index.store && (
            <Badge tone={index.store === "starrocks" ? "gold" : "neutral"}>
              <span title="Where the materialized objects live">
                {index.store}
              </span>
            </Badge>
          )}
        </div>
        {canEdit && (
          <div className="toolbar" style={{ gap: 8 }}>
            <button
              className="small"
              disabled={build.isPending || busy}
              onClick={() => build.mutate()}
            >
              {build.isPending ? "Building…" : index.indexed ? "Rebuild" : "Build"}
            </button>
            {index.indexed && (
              <button
                className="small"
                disabled={drop.isPending || busy}
                onClick={() => drop.mutate()}
              >
                Drop
              </button>
            )}
          </div>
        )}
      </div>
      {index.indexed && !index.fresh && (
        <p className="hint" style={{ marginBottom: 0 }}>
          {index.stale_version
            ? // `current_dataset_version` is null when the backing dataset has
              // no versions at all — it was dropped or replaced under the type.
              // Naming a version that does not exist would send the operator
              // looking for it, so that case gets its own sentence.
              index.current_dataset_version === null
              ? `${type.backing_dataset} has no versions, so there is nothing for this store to be current with. Reads fall back to a full scan.`
              : `Built from ${type.backing_dataset} v${index.dataset_version}, which is now at v${index.current_dataset_version}. A new version can rewrite any row, so no incremental delta expresses it: catch-up will not run${lag > 0 ? ` and those ${lag} edit${lag === 1 ? "" : "s"} will keep accumulating` : ""}. Rebuild is the only fix. Until then reads fall back to a full scan.`
            : index.stale_definition
              ? `Built from an older definition of ${type.api_name}. The properties, key, title or backing dataset changed, so the stored rows no longer match what a read projects — if the binding moved, they are a different dataset's rows entirely. Rebuild is the only fix. Until then reads fall back to a full scan.`
              : lag > 0
                ? `Behind by ${lag} edit${lag === 1 ? "" : "s"}. Reads fall back to a full scan until it catches up, which happens on the next write — no rebuild needed.`
                : "Reads fall back to a full scan until this is rebuilt."}
        </p>
      )}
      {/* The build endpoint answers 200 with zero objects and no state when the
          backing dataset has no stable local snapshot to materialize from —
          `reindex` drops the store and returns 0 for a table scanned at the
          source, a dataset that no longer exists, or one with no versions. The
          badge then goes back to saying "Not built", which from the user's side
          is a button that did nothing. Verified against an object type whose
          backing dataset is a StarRocks table. */}
      {build.isSuccess &&
        build.data?.objects === 0 &&
        build.data?.state == null && (
          <p className="hint" style={{ marginBottom: 0 }}>
            Nothing was built. <code>{type.backing_dataset}</code> has no local
            snapshot to materialize from — it is scanned where it lives, or has
            no versions yet. Materialize the rows with a transform and bind this
            type to that.
          </p>
        )}

      {index.store === "starrocks" && (
        <p className="hint" style={{ marginBottom: 0 }}>
          StarRocks-backed object store is unverified against a live server.
        </p>
      )}
      {(build.isError || drop.isError) && (
        <ErrorBox error={build.error || drop.error} />
      )}
    </div>
  );
}

export function OntologyView() {
  return (
    <Routes>
      <Route index element={<ObjectTypeList />} />
      <Route path=":type" element={<ObjectTypePage />} />
      <Route path=":type/:pk" element={<ObjectTypePage />} />
    </Routes>
  );
}
