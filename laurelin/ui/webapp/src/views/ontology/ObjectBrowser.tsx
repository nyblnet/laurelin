import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { API, api } from "../../api";
import type {
  ObjectQueryResult,
  ObjectTypeDetail,
  OntologyObject,
} from "../../types";
import {
  Column,
  DataTable,
  ErrorBox,
  Spinner,
  fmtNum,
  fmtValue,
} from "../../ui";
import { useDebounced } from "./useDebounced";
import { ObjectDetail } from "./ObjectDetail";

const PAGE = 25;

/** Object table (left) + selected-object detail (right) for one type. */
export function ObjectBrowser({ detail }: { detail: ObjectTypeDetail }) {
  const { pk: routePk } = useParams();
  const type = detail.api_name;

  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [selectedPk, setSelectedPk] = useState<string | null>(routePk ?? null);
  const debouncedSearch = useDebounced(search, 300);

  // A pk in the URL (arrived via a link) selects that object.
  useEffect(() => {
    if (routePk) setSelectedPk(routePk);
  }, [routePk]);

  // Reset paging when the query changes.
  useEffect(() => {
    setOffset(0);
  }, [debouncedSearch]);

  const q = useQuery({
    queryKey: ["objects", type, debouncedSearch, offset],
    queryFn: () =>
      api.get<ObjectQueryResult>(
        `${API}/ontology/objects/${type}?search=${encodeURIComponent(
          debouncedSearch,
        )}&limit=${PAGE}&offset=${offset}`,
      ),
  });

  // First ~5 declared property names for the table's columns.
  const propKeys = Object.keys(detail.properties).slice(0, 5);
  const columns: Column<OntologyObject>[] = [
    { label: "Title", render: (o) => o.__title || o.__pk },
    ...propKeys.map((key) => ({
      label: detail.properties[key].display_name || key,
      render: (o: OntologyObject) => fmtValue(o[key]),
    })),
  ];

  const total = q.data?.total ?? 0;

  return (
    <div className="ontology-browser">
      {/* LEFT: searchable object table */}
      <div>
        <div className="toolbar">
          <input
            type="search"
            placeholder="Search objects…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        {q.isLoading && <Spinner />}
        {q.isError && <ErrorBox error={q.error} />}
        {q.data &&
          (q.data.objects.length === 0 ? (
            <div className="empty">No objects match.</div>
          ) : (
            <>
              <DataTable
                columns={columns}
                rows={q.data.objects}
                rowKey={(o) => o.__pk}
                onRowClick={(o) => setSelectedPk(o.__pk)}
                isSelected={(o) => o.__pk === selectedPk}
              />
              <div className="pager">
                <button
                  className="small"
                  disabled={offset === 0}
                  onClick={() => setOffset((o) => Math.max(0, o - PAGE))}
                >
                  Prev
                </button>
                <span>
                  {total === 0
                    ? "0"
                    : `${offset + 1}–${Math.min(offset + PAGE, total)}`}{" "}
                  of {fmtNum(total)}
                </span>
                <button
                  className="small"
                  disabled={offset + PAGE >= total}
                  onClick={() => setOffset((o) => o + PAGE)}
                >
                  Next
                </button>
              </div>
            </>
          ))}
      </div>

      {/* RIGHT: selected object detail */}
      <div>
        {selectedPk ? (
          <ObjectDetail detail={detail} type={type} pk={selectedPk} />
        ) : (
          <div className="empty">Select an object.</div>
        )}
      </div>
    </div>
  );
}
