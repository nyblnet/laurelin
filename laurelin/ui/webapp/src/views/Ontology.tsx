import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { API, api } from "../api";
import type { ObjectTypeDef, ObjectTypeDetail } from "../types";
import { ErrorBox, PageHeader, Spinner } from "../ui";
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
      {q.data && <ObjectBrowser detail={q.data} />}
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
