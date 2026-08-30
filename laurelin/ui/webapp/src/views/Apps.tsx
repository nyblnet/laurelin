// Object apps — a curated view over one object type.
//
// The Ontology tab is an explorer: every type, every property, every action.
// An app is the opposite — one type, the columns that matter, the filters that
// scope it, and the handful of actions an operator should reach for. Same data,
// same permissions, narrower surface. This is what you hand to someone who has
// a job to do rather than an ontology to explore.

import { useState } from "react";
import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { API, api } from "../api";
import { useAuth } from "../auth";
import type {
  ObjectApp,
  ObjectQueryResult,
  ObjectTypeDetail,
  OntologyObject,
} from "../types";
import {
  Badge,
  Column,
  DataTable,
  EmptyState,
  ErrorBox,
  PageHeader,
  Spinner,
  Withheld,
  fmtCount,
  fmtValue,
} from "../ui";
import { ActionForm } from "./ontology/ActionForm";
import { LinkSection } from "./ontology/LinkSection";
import { useDebounced } from "./ontology/useDebounced";

const PAGE = 25;

export function AppsView() {
  return (
    <Routes>
      <Route index element={<AppList />} />
      <Route path=":name" element={<AppPage />} />
      <Route path=":name/:pk" element={<AppPage />} />
    </Routes>
  );
}

// ------------------------------------------------------------------- list

function AppList() {
  const navigate = useNavigate();
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["apps"],
    queryFn: () => api.get<ObjectApp[]>(`${API}/apps`),
  });

  const columns: Column<ObjectApp>[] = [
    {
      label: "App",
      render: (a) => <Link to={`/apps/${a.name}`}>{a.title || a.name}</Link>,
    },
    {
      label: "Description",
      render: (a) =>
        a.description ? (
          <span className="dim">{a.description}</span>
        ) : (
          <span className="faint">—</span>
        ),
    },
    {
      label: "Objects",
      render: (a) => <Badge tone="gold">{a.object_type}</Badge>,
    },
  ];

  return (
    <div>
      <PageHeader
        title="Apps"
        subtitle="Ready-made working screens — one kind of object, with just the columns, filters and actions for one job."
      />
      {isLoading && <Spinner />}
      {error && <ErrorBox error={error} onRetry={() => refetch()} />}
      {data &&
        (data.length === 0 ? (
          <EmptyState>
            No apps yet. An app is a trimmed-down screen over one object type
            from the Ontology page — just the columns, filters and actions
            someone needs for a job, like “Aircraft in maintenance”. Creating
            one requires an admin, who defines it through the REST API (
            <code>PUT /api/v1/apps/&lt;name&gt;</code> — the request shape is in{" "}
            <code>docs/ARCHITECTURE.md</code>); there is no in-app editor yet.
          </EmptyState>
        ) : (
          <DataTable
            columns={columns}
            rows={data}
            rowKey={(a) => a.name}
            onRowClick={(a) => navigate(`/apps/${a.name}`)}
          />
        ))}
    </div>
  );
}

// ----------------------------------------------------------------- one app

function AppPage() {
  const { name = "", pk } = useParams();
  const appQ = useQuery({
    queryKey: ["app", name],
    queryFn: () => api.get<ObjectApp>(`${API}/apps/${name}`),
  });

  if (appQ.isLoading) return <Spinner />;
  if (appQ.isError) return <ErrorBox error={appQ.error} onRetry={() => appQ.refetch()} />;
  return <AppBody app={appQ.data!} selectedPk={pk ?? null} />;
}

function AppBody({ app, selectedPk }: { app: ObjectApp; selectedPk: string | null }) {
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const debounced = useDebounced(search, 250);

  // Reset paging when the search changes — during render, before the query
  // fires. Resetting in the input's onChange (the old shape) raced the
  // debounce: offset hit the query key immediately while the search string
  // arrived 250ms later, so a page-2 search cost an extra request for page 1
  // of the OLD search on every first keystroke.
  const [searchApplied, setSearchApplied] = useState(debounced);
  if (debounced !== searchApplied) {
    setSearchApplied(debounced);
    setOffset(0);
  }

  // The object type carries the property/action/link definitions; the app
  // only narrows which of them to show.
  const typeQ = useQuery({
    queryKey: ["objectType", app.object_type],
    queryFn: () =>
      api.get<ObjectTypeDetail>(`${API}/ontology/object-types/${app.object_type}`),
  });

  const objectsQ = useQuery({
    // Prefixed ["objects", <type>] so applying an action invalidates this list
    // too — otherwise an aircraft returned to service would linger in the
    // maintenance queue it no longer belongs to.
    queryKey: ["objects", app.object_type, app.name, debounced, offset],
    // The app's own route, not the generic one. Its filters are OPERATIONAL
    // under R2 and are not sent to anyone below admin — and this client used to
    // be the thing applying them, so without them it would have shown the whole
    // object type while still calling itself "Aircraft in maintenance".
    // The server holds the filters and applies them; the caller's permissions
    // still decide the rows.
    queryFn: () =>
      api.get<ObjectQueryResult>(
        `${API}/apps/${encodeURIComponent(app.name)}/objects?limit=${PAGE}&offset=${offset}` +
          (debounced ? `&search=${encodeURIComponent(debounced)}` : ""),
      ),
    // Keep the previous page visible (dimmed) while the next loads, so the
    // pager and the table never unmount under the user mid-pagination.
    placeholderData: (prev) => prev,
  });

  if (typeQ.isLoading) return <Spinner />;
  if (typeQ.isError) return <ErrorBox error={typeQ.error} onRetry={() => typeQ.refetch()} />;
  const type = typeQ.data!;

  // Empty config means "everything the type declares" — an app narrows, it
  // never invents.
  const columns =
    app.columns.length > 0 ? app.columns : Object.keys(type.properties);
  const actions =
    app.actions.length > 0
      ? type.actions.filter((a) => app.actions.includes(a.api_name))
      : type.actions;
  const links =
    app.links.length > 0
      ? type.links.filter((l) => app.links.includes(l.api_name))
      : type.links;

  // Present for an admin, absent for everyone else. Both cases have to say
  // something: "this list is a slice" is a fact about what you are reading, and
  // an unlabelled slice is worse than a labelled one you cannot fully read.
  const activeFilters = Object.entries(app.filters ?? {});
  const filtersWithheld = app.filters === undefined;

  return (
    <div>
      <div style={{ marginBottom: 12 }}>
        <Link to="/apps">← Apps</Link>
      </div>
      <PageHeader
        title={app.title || app.name}
        subtitle={app.description || undefined}
      />

      {(filtersWithheld || activeFilters.length > 0) && (
      <div
        className="dim"
        style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 14 }}
      >
        {filtersWithheld ? (
          <>
            <span style={{ fontSize: 12.5 }}>Scoped by this app</span>
            <Withheld
              what="An app's filter expressions"
              role="admin"
              label="filters not shown"
              why="They are applied server-side, so this list is scoped whether or not you can read the rule."
            />
          </>
        ) : (
          <>
            <span style={{ fontSize: 12.5 }}>Scoped to</span>
            {activeFilters.map(([k, v]) => (
              <Badge key={k} tone="blue">
                {k} = {v}
              </Badge>
            ))}
          </>
        )}
      </div>
      )}

      <div className="toolbar">
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={app.search_placeholder || `Search ${type.display_name || type.api_name}…`}
          style={{ maxWidth: 320 }}
        />
        {objectsQ.data && (
          <span className="dim" style={{ fontSize: 12.5 }}>
            {fmtCount(objectsQ.data)} matching
          </span>
        )}
      </div>

      {objectsQ.isLoading ? (
        <Spinner />
      ) : objectsQ.isError ? (
        <ErrorBox error={objectsQ.error} onRetry={() => objectsQ.refetch()} />
      ) : objectsQ.data!.objects.length === 0 ? (
        debounced ? (
          <EmptyState>
            Nothing matches “{debounced}”.{" "}
            <button type="button" className="small" onClick={() => setSearch("")}>
              Clear the search
            </button>
          </EmptyState>
        ) : (
          <EmptyState>
            Nothing here right now. This app shows only the objects that fit
            its scope — when one does, it appears in this list.
          </EmptyState>
        )
      ) : (
        // DataTable rather than a raw table element: rows are controls (they open
        // the object), and the primitive is what makes them tabbable and
        // Enter/Space-activatable instead of mouse-only.
        <div style={objectsQ.isFetching ? { opacity: 0.6 } : undefined}>
          <DataTable
            columns={columns.map(
              (c): Column<OntologyObject> => ({
                label: type.properties[c]?.display_name || c,
                render: (o) => fmtValue(o[c]),
                className: "mono",
              }),
            )}
            rows={objectsQ.data!.objects}
            rowKey={(o) => o.__pk}
            onRowClick={(o) =>
              navigate(`/apps/${app.name}/${encodeURIComponent(o.__pk)}`)
            }
            isSelected={(o) => o.__pk === selectedPk}
          />
          <div className="pager">
            <button
              className="small"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE))}
            >
              Prev
            </button>
            <button
              className="small"
              disabled={offset + PAGE >= objectsQ.data!.total}
              onClick={() => setOffset(offset + PAGE)}
            >
              Next
            </button>
            <span>
              {offset + 1}–{offset + objectsQ.data!.objects.length} of{" "}
              {fmtCount(objectsQ.data!)}
            </span>
          </div>
        </div>
      )}

      {selectedPk && (
        <SelectedObject
          type={type}
          pk={selectedPk}
          links={links}
          actions={actions}
        />
      )}
    </div>
  );
}

function SelectedObject({
  type,
  pk,
  links,
  actions,
}: {
  type: ObjectTypeDetail;
  pk: string;
  links: ObjectTypeDetail["links"];
  actions: ObjectTypeDetail["actions"];
}) {
  const auth = useAuth();
  const canEdit = type.permissions?.can_edit ?? auth.can("editor");

  const objQ = useQuery({
    queryKey: ["object", type.api_name, pk],
    queryFn: () =>
      api.get<Record<string, unknown>>(
        `${API}/ontology/objects/${type.api_name}/${encodeURIComponent(pk)}`,
      ),
  });

  return (
    <section style={{ marginTop: 28 }}>
      <h2 style={{ marginBottom: 4 }}>{pk}</h2>
      {objQ.isLoading ? (
        <Spinner />
      ) : objQ.isError ? (
        <ErrorBox error={objQ.error} onRetry={() => objQ.refetch()} />
      ) : (
        <div className="card" style={{ marginBottom: 18 }}>
          <dl
            style={{
              display: "grid",
              gridTemplateColumns: "minmax(120px, 200px) 1fr",
              gap: "6px 18px",
              margin: 0,
              fontSize: 13.5,
            }}
          >
            {Object.keys(type.properties).map((p) => (
              <div key={p} style={{ display: "contents" }}>
                <dt className="dim">{type.properties[p]?.display_name || p}</dt>
                <dd className="mono" style={{ margin: 0 }}>
                  {fmtValue(objQ.data![p])}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      {links.map((link) => (
        <LinkSection key={link.api_name} link={link} type={type.api_name} pk={pk} />
      ))}

      {actions.length > 0 && (
        <>
          <h3>Actions</h3>
          {!canEdit && (
            <p className="faint" style={{ fontSize: 12.5 }}>
              You have read-only access to {type.api_name}.
            </p>
          )}
          <div style={{ display: "grid", gap: 12 }}>
            {actions.map((action) => (
              <ActionForm
                key={action.api_name}
                action={action}
                type={type.api_name}
                selectedPk={pk}
                canEdit={canEdit}
              />
            ))}
          </div>
        </>
      )}
    </section>
  );
}
