import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { API, api } from "../../api";
import type { LinkTypeDef, ObjectQueryResult } from "../../types";
import { ErrorBox } from "../../ui";

/**
 * Lazily loads and lists the objects linked to `pk` via `link`. Titles link
 * through to the object on the other side of the relationship.
 */
export function LinkSection({
  link,
  type,
  pk,
}: {
  link: LinkTypeDef;
  type: string;
  pk: string;
}) {
  // The type on the far side of the link.
  const otherType = link.from === type ? link.to : link.from;

  const q = useQuery({
    queryKey: ["links", type, pk, link.api_name],
    queryFn: () =>
      api.get<ObjectQueryResult>(
        `${API}/ontology/objects/${type}/${encodeURIComponent(pk)}/links/${link.api_name}`,
      ),
  });

  return (
    <div>
      <h3>{link.display_name || link.api_name}</h3>
      {q.isLoading && <div className="dim">Loading…</div>}
      {q.isError && <ErrorBox error={q.error} />}
      {q.data &&
        (q.data.objects.length === 0 ? (
          <div className="faint" style={{ fontSize: 12.5 }}>
            No linked objects.
          </div>
        ) : (
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {q.data.objects.map((o) => (
              <li key={o.__pk} style={{ marginBottom: 2 }}>
                <Link to={`/ontology/${otherType}/${encodeURIComponent(o.__pk)}`}>
                  {o.__title || o.__pk}
                </Link>
              </li>
            ))}
          </ul>
        ))}
    </div>
  );
}
