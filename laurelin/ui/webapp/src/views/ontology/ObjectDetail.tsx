import { useQuery } from "@tanstack/react-query";
import { API, ApiError, api } from "../../api";
import type { ObjectTypeDetail, OntologyObject } from "../../types";
import { ErrorBox, Spinner, fmtValue } from "../../ui";
import { ActionForm } from "./ActionForm";
import { LinkSection } from "./LinkSection";

/** Right-hand panel: full properties, linked objects, and action forms. */
export function ObjectDetail({
  detail,
  type,
  pk,
}: {
  detail: ObjectTypeDetail;
  type: string;
  pk: string;
}) {
  const q = useQuery({
    queryKey: ["object", type, pk],
    queryFn: () =>
      api.get<OntologyObject>(
        `${API}/ontology/objects/${type}/${encodeURIComponent(pk)}`,
      ),
    retry: false,
  });

  if (q.isLoading) return <Spinner />;
  if (q.error instanceof ApiError && q.error.status === 404) {
    return <div className="empty">This object no longer exists.</div>;
  }
  if (q.isError) return <ErrorBox error={q.error} />;
  const obj = q.data;
  if (!obj) return null;

  const propEntries = Object.entries(detail.properties);

  return (
    <div>
      <h2 style={{ marginBottom: 12 }}>{obj.__title || obj.__pk}</h2>

      <dl className="kv">
        {propEntries.map(([key, def]) => (
          <div key={key} style={{ display: "contents" }}>
            <dt>{def.display_name || key}</dt>
            <dd>{fmtValue(obj[key])}</dd>
          </div>
        ))}
      </dl>

      {detail.links.map((link) => (
        <LinkSection key={link.api_name} link={link} type={type} pk={pk} />
      ))}

      {detail.actions.length > 0 && (
        <>
          <h3>Actions</h3>
          {detail.actions.map((action) => (
            <ActionForm
              key={action.api_name}
              action={action}
              type={type}
              selectedPk={pk}
            />
          ))}
        </>
      )}
    </div>
  );
}
