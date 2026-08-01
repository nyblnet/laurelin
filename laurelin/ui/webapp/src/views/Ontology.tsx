import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../api";
import { useAuth } from "../auth";
import type { ObjectTypeDef, ObjectTypeDetail } from "../types";
import { Badge, ErrorBox, PageHeader, Spinner } from "../ui";
import { ObjectBrowser } from "./ontology/ObjectBrowser";

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
          <div className="empty">No object types defined.</div>
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
          <IndexControl type={q.data} />
          <ObjectBrowser detail={q.data} />
        </>
      )}
    </div>
  );
}

/**
 * Build or drop this type's search index.
 *
 * Indexing makes key lookups constant-time and search sub-linear, at the cost
 * of storage and a refresh — worth it for entities, wasteful for high-volume
 * events, so it's opt-in per type. Freshness is shown because a stale index is
 * silently bypassed: without this, "why did search get slow again" has no
 * visible answer.
 */
function IndexControl({ type }: { type: ObjectTypeDetail }) {
  const qc = useQueryClient();
  const { user } = useAuth();
  const canEdit = user?.role === "editor" || user?.role === "admin";
  const index = type.index ?? { indexed: false, fresh: false, objects: 0 };

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["object-type", type.api_name] });

  const build = useMutation({
    mutationFn: () => api.post(`${API}/ontology/object-types/${type.api_name}/index`, {}),
    onSuccess: invalidate,
  });
  const drop = useMutation({
    mutationFn: () => api.del(`${API}/ontology/object-types/${type.api_name}/index`),
    onSuccess: invalidate,
  });

  const status = !index.indexed
    ? { tone: "neutral" as const, text: "Not indexed" }
    : index.fresh
      ? { tone: "green" as const, text: `Indexed · ${index.objects.toLocaleString()} objects` }
      : { tone: "gold" as const, text: "Index stale — rebuild to use it" };

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="toolbar" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <label style={{ margin: 0 }}>Search index</label>
          <Badge tone={status.tone}>{status.text}</Badge>
        </div>
        {canEdit && (
          <div className="toolbar" style={{ gap: 8 }}>
            <button
              className="small"
              disabled={build.isPending}
              onClick={() => build.mutate()}
            >
              {build.isPending ? "Building…" : index.indexed ? "Rebuild" : "Build index"}
            </button>
            {index.indexed && (
              <button
                className="small"
                disabled={drop.isPending}
                onClick={() => drop.mutate()}
              >
                Drop
              </button>
            )}
          </div>
        )}
      </div>
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
