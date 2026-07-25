# Laurelin

**An open, ontology-driven data platform.** Laurelin gives you the core ideas of
platforms like Palantir Foundry — versioned datasets, code-based transforms with
automatic lineage, and a semantic ontology layer with objects, links, and actions —
built entirely on open formats and open APIs, with no lock-in of any kind.

> *Laurelin was the golden of the Two Trees of Valinor, whose light was gathered
> and shared rather than hoarded.*

## Why "more open"?

| | Proprietary data platforms | Laurelin |
|---|---|---|
| License | Closed source | Apache-2.0 |
| Data at rest | Proprietary stores | **Parquet** files on disk — readable by pandas, DuckDB, Spark, anything |
| Metadata | Opaque services | A single **SQLite** database you can query directly |
| Ontology definitions | GUI-managed, exported with difficulty | Plain **YAML** files in your repo, diffable and code-reviewable |
| Pipelines | Platform-hosted code | Plain **Python** files; run them anywhere |
| API | Partially documented | **REST + OpenAPI** (`/docs`), generated from source |
| Deployment | SaaS / heavyweight | `pip install`, local-first, single process |
| Exit cost | High | `cp -r` your workspace directory. That's it. |

Everything Laurelin knows lives in one **workspace directory** of ordinary files.
Delete the tool and your data, lineage, and ontology are still readable.

## Concepts

Laurelin maps one-to-one onto the concepts you may know from Foundry:

- **Datasets** — versioned tables stored as Parquet. Every write creates an
  immutable new version; the full history is kept.
- **Data sources** — connectors that pull external data into datasets:
  PostgreSQL (streamed in batches), HTTP CSV/Parquet exports, and server-side
  file drops. Synced versions flow through lineage, ACLs, and markings like
  any other data.
- **Transforms** — Python functions (or SQL) declared with `@transform`,
  reading input datasets and producing an output dataset. Laurelin resolves the
  DAG, executes builds, and records **lineage** automatically.
- **Ontology** — YAML-defined *object types* (e.g. `aircraft`, `flight`) backed by
  datasets, with typed properties, *link types* between them, and *actions* —
  validated write-back operations recorded as an edit overlay and audit log.
- **Builds & lineage** — builds run asynchronously on a worker pool and every
  build is recorded; the lineage graph is queryable via API and rendered in
  the UI.
- **Schedules** — cron or on-upstream-changed triggers drive builds and
  connector syncs, so pipelines keep themselves current. Exactly-once across
  replicas, with no leader election.
- **Dashboards** — grids of saved queries rendered as charts (zero-dependency
  SVG). Panels execute with the *viewer's* credentials, so row-level security
  and ACLs apply per user.
- **MCP server** — `laurelin mcp` exposes datasets, SQL, the ontology, actions,
  and builds to AI agents over the Model Context Protocol. Agents authenticate
  with an API token and go through the same permission and audit path as any
  user (`pip install laurelin[mcp]`).
- **Audit** — mutations through the API are written to an audit log.

## Tutorials

Three task-shaped walkthroughs that build on each other — start here:

1. [Ingest → transform → build](docs/tutorials/01-ingest-transform-build.md) — a
   CSV to a versioned dataset to a two-stage pipeline with lineage (~10 min).
2. [Model an ontology and act on it](docs/tutorials/02-ontology-and-actions.md) —
   object types, links, and validated write-back actions (~15 min).
3. [Lock a dataset down](docs/tutorials/03-securing-data.md) — ACLs, row-level
   security, column masking, and classification markings (~15 min).

## Quickstart

```bash
pip install -e ".[dev]"

# Create a demo workspace with sample data, a pipeline, and an ontology
laurelin demo demo-workspace

# Run the pipeline (executes the transform DAG, records lineage)
laurelin build --workspace demo-workspace

# Serve the API + web UI (--no-auth: skip login for local development)
laurelin serve --workspace demo-workspace --no-auth
# UI:      http://127.0.0.1:8787
# OpenAPI: http://127.0.0.1:8787/docs
```

Or start from scratch:

```bash
laurelin init my-workspace --name "My project"
laurelin upload my_dataset data.csv --workspace my-workspace
```

Then write a pipeline in `my-workspace/pipelines/`:

```python
from laurelin.transforms import transform, sql_transform, Input, Output

@transform(output=Output("clean_orders"), orders=Input("raw_orders"))
def clean_orders(orders):
    # orders is a pyarrow.Table; return a pyarrow.Table
    import pyarrow.compute as pc
    return orders.filter(pc.is_valid(orders["order_id"]))

@sql_transform(
    output=Output("orders_by_region"),
    inputs={"o": Input("clean_orders")},
    query="SELECT region, count(*) AS n, sum(amount) AS total FROM o GROUP BY region",
)
def orders_by_region(): ...
```

And an ontology in `my-workspace/ontology/*.yml`:

```yaml
object_types:
  - api_name: order
    display_name: Order
    backing_dataset: clean_orders
    primary_key: order_id
    title_property: order_id
    properties:
      order_id: { type: string }
      region:   { type: string }
      amount:   { type: float }

actions:
  - api_name: flag_order
    display_name: Flag order for review
    object_type: order
    kind: update
    parameters:
      review_status: { type: string, required: true }
```

## Workspace layout

```
my-workspace/
├── laurelin.yml      # workspace config
├── metadata.db       # SQLite: versions, builds, lineage, edits, audit
├── data/             # <dataset>/v<N>/data.parquet  (immutable versions)
├── pipelines/        # your transform code (plain Python)
└── ontology/         # object types, links, actions (plain YAML)
```

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                     Web UI (static)                  │
├──────────────────────────────────────────────────────┤
│                REST API (FastAPI, /docs)             │
├────────────┬──────────────┬──────────────────────────┤
│  Catalog   │  Transforms  │  Ontology                │
│  versioned │  DAG builder │  objects / links /       │
│  datasets  │  + lineage   │  actions + edit overlay  │
├────────────┴──────────────┴──────────────────────────┤
│   Parquet (data)  ·  SQLite (metadata)  ·  DuckDB    │
│                    (query engine)                    │
└──────────────────────────────────────────────────────┘
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for module-level detail.

## Security

Authentication is on by default. On first launch the server is in *setup
mode*: visit the UI (or `POST /api/v1/auth/setup`) to create the first admin
account, then sign in. Users, roles (`viewer` < `editor` < `admin`), and API
tokens are managed in the UI, via `/api/v1/users` + `/api/v1/tokens`, or with
the `laurelin users ...` / `laurelin tokens ...` CLI commands. API clients
authenticate with `Authorization: Bearer <token>`; browsers use an httpOnly
session cookie. For local development, `laurelin serve --no-auth` (or
`LAURELIN_NO_AUTH=1`) disables auth entirely.

**Fine-grained ontology access.** Beyond the global roles, admins can grant
per-object-type view/edit access to specific users, groups, roles, or everyone
(Admin → Ontology access). A type with no grants is open (viewers view, editors
edit); adding any grant turns it into an allowlist. Admins always have access.
Datasets have their own view/edit grants too (Admin → Dataset access), and
ontology view composes with them — locking a dataset hides its objects.

**Row-level security & column masking.** Per dataset, admins can restrict which
*rows* a user sees (a policy column + per-subject allowed values) and *mask*
columns (redact / null / hash) except for exempt subjects. It's enforced
uniformly on the row API, the SQL workbench (aggregates respect it), and
ontology objects — admins are exempt. Admin → Row & column security.

**Multiple workspaces.** `laurelin serve --workspace X` hosts a single
workspace. `laurelin serve --root DIR` hosts many: users are global, a
superadmin creates workspaces and assigns each user a per-workspace role
(viewer/editor/admin), and each workspace is fully isolated — its own datasets,
pipelines, ontology, and ACLs under `DIR/<slug>/`. In the UI a switcher picks
the active workspace; superadmins get a Workspaces admin panel.

**Enterprise SSO (OIDC).** Point Laurelin at an OIDC issuer (Okta, Entra ID,
Google, Keycloak, Auth0, …) with `LAURELIN_OIDC_ISSUER` / `_CLIENT_ID` /
`_CLIENT_SECRET` and users sign in with your IdP; group claims map to roles
(`LAURELIN_OIDC_ROLE_MAP`). Local accounts keep working alongside it.

**Deployment.** Embedded mode is a single process on SQLite + local files. For
multi-tenant deployments, run the control plane on **PostgreSQL**
(`serve --root --control-db postgresql://…`) and use the provided **Docker**
image + `docker-compose.yml`:

```bash
docker compose up --build      # Laurelin + Postgres
# open http://localhost:8787 -> create the server administrator
```

**Transform authoring is code execution.** Writing a pipeline file through the
UI (the Transforms tab) or API is equivalent to running Python on the server —
it is `exec`'d on every build. It requires the `editor` role and can be
disabled entirely with `laurelin serve --lock-pipelines` (or
`LAURELIN_LOCK_PIPELINES=1`) for untrusted multi-user deployments. The executed
transform code is not yet sandboxed.

See [SECURITY.md](SECURITY.md) for the full security model, trust boundaries,
and how to report a vulnerability.

## Status & scale

Early alpha. The core loop — ingest → transform → build → ontology → act —
works end to end; expect rough edges and breaking changes.

Laurelin runs as a single process on a laptop *or* as N stateless replicas
behind a load balancer — identity, workspace metadata and build coordination in
PostgreSQL, dataset Parquet in object storage (`LAURELIN_DATA_URI=s3://…`).
Compute is DuckDB in-process per replica: strong on governance and semantics,
deliberately not a distributed compute engine. Data too big for that is either
**federated** (governed in place, scanned remotely) or **delegated** — a
`@remote_transform` runs on a Trino/Dremio/Databricks cluster and Laurelin
stores the reduced result with lineage and policy intact.
[docs/SCALE.md](docs/SCALE.md) publishes measured numbers, including the
unflattering ones: the SQL path stays comfortable into the tens of millions of
rows, appends cost the delta rather than the dataset, and ontology queries run
in DuckDB (~26× faster than they were) but are still scans rather than an
index. Reproduce them with `python bench/benchmark.py`.

## License

[Apache-2.0](LICENSE)
