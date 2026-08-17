# Laurelin

[![CI](https://github.com/laurelin-data/laurelin/actions/workflows/ci.yml/badge.svg)](https://github.com/laurelin-data/laurelin/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

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
| Exit cost | High | `laurelin export` — one archive, and a governance fingerprint that **proves** the copy decides identically |

Everything Laurelin knows lives in one **workspace directory** of ordinary files.
Delete the tool and your data, lineage, and ontology are still readable.

### Leaving is a command, and it is checkable

```bash
laurelin export workspace.tar --fingerprint   # data, governance, ontology, pipelines
laurelin import workspace.tar -w new          # reconstruct it, on SQLite or PostgreSQL
laurelin verify-governance --baseline workspace.tar -w new
```

The third line is the point. It recomputes, per `(principal, dataset)`, the rows
that principal sees and the cells they see unmasked — through all three
enforcement paths — and diffs it against the archive. "It still governs
identically" is something you check, not something we assert.

The export **withholds every credential** rather than redacting it (Laurelin's
own API redactors were attacked with a corpus of 26 credential shapes and ten
of them leaked — `tests/test_redaction.py`; those are fixed, by withholding
rather than by a better regex), and the
import **binds no principal**: rules land verbatim, users, group memberships and
clearances do not, so a reconstruction can narrow access and never widen it. The
manifest is a checklist of exactly what has to be re-supplied.

It does not carry everything, and [docs/PORTABILITY.md](docs/PORTABILITY.md)
says what: federated, ClickHouse and StarRocks datasets are pointers whose rows
live elsewhere, Iceberg tables must be re-registered against a reachable
warehouse, object-store data planes are unverified, and there is no incremental
or resumable export.

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
- **Flows** — the same thing, built without code. Pick a dataset, then add
  steps: filter rows, combine two datasets, group and summarise, sort. A flow is
  stored as `pipelines/<name>.flow.json` and compiled to SQL in memory, so it is
  a *transform* — same build, same lineage, same permissions, same schedules —
  and not a second engine. There is no free-text SQL box anywhere in it: every
  value you type is bound as a query parameter and every column name is checked
  against the live schema. See [what a flow cannot do](#what-a-flow-cannot-do).
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
- **Apps** — a curated view over one object type: the columns that matter, the
  filters that scope it, the actions an operator should reach for. Configured,
  not coded, and it grants no access the ontology doesn't already.
- **MCP server** — `laurelin mcp` exposes datasets, SQL, the ontology, actions,
  and builds to AI agents over the Model Context Protocol. Agents authenticate
  with an API token and go through the same permission and audit path as any
  user (needs the `mcp` extra — see Status below for how to install).
- **Audit** — mutations through the API are written to an audit log.

## Where compute happens

Four roles, one governance layer. The role is a per-dataset property, not a
deployment mode — the same catalog, ACLs, markings, row policies and lineage
apply across all four.

- **Embedded analytical default — DuckDB, in process, per replica.** Managed
  Parquet datasets, the SQL workbench, dashboards, transforms and ontology
  pushdown all run here. It is the default, it needs no infrastructure, and for
  medium data it is the only role you ever touch.
- **Federation over foreign systems — DuckDB attach, and Flight SQL engines.**
  Register an existing Iceberg/Delta/Parquet/PostgreSQL table as a *federated*
  dataset and Laurelin governs bytes it does not hold, scanning them in place
  with the policy compiled around the remote scan. For work that is genuinely
  huge and non-selective, a `@remote_transform` *delegates* to Trino, Dremio or
  Databricks over Flight SQL and stores the reduced result with lineage and
  policy intact. Laurelin runs no cluster and does not intend to.
- **Serving tier — StarRocks (flagship) and ClickHouse (supported peer).**
  Governed, policy-pushed-down reads of a table the serving engine owns.
  StarRocks leads because querying Iceberg is a first-class path there, so
  "open at rest" survives the serving tier instead of being traded away for it;
  because it joins natively, and an ontology link *is* a join; and because it
  has primary-key tables with real upserts. ClickHouse shipped the same day and
  is fully supported — embedded via **chdb**, so there is no server to run.
  Both are **read-only from Laurelin**: the engine serves, Laurelin governs the
  read. Laurelin does not operate or load a serving engine.
- **Operational store — the materialization of ontology object state.** The
  metadata store is the default and the only backend wired to configuration.
  A StarRocks-backed store exists behind the same seam, writing via Stream Load
  with primary-key upserts, but it has **only run against an in-memory double —
  never a real StarRocks server** — and no env var selects it, so every
  deployment today runs the metadata store.

**Open at rest, in every role.** Datasets can be **Apache Iceberg** tables
(`pip install 'laurelin[iceberg]'`) with branches, time travel and schema
evolution, which Spark, Trino, Snowflake and DuckDB open directly. That is also
what makes the serving tier coherent rather than a lock-in: the table a
serving engine reads can be the same open table everything else reads.

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
├── data/             # <dataset>/parts/*.parquet  (immutable; a version is a manifest of parts)
├── pipelines/        # transforms: *.py (code) and *.flow.json (no-code flows)
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
├────────────┼──────────────┴──────────────────────────┤
│  Storage   │  Parquet · Iceberg (data)               │
│            │  SQLite / PostgreSQL (metadata)         │
├────────────┼─────────────────────────────────────────┤
│  Compute   │  DuckDB embedded · federation ·         │
│            │  serving tier · operational store       │
└────────────┴─────────────────────────────────────────┘
```

The four compute roles are [below](#where-compute-happens); DuckDB embedded is
the default and the only one you need to start.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for module-level detail.

### What a flow cannot do

The no-code builder covers the shape of pipeline most analysts write, and stops
there on purpose — a builder that half-supports a feature is worse than one that
does not offer it. It has ten step kinds (start from a dataset, filter, choose
columns, rename, add a column, change type, combine with another dataset, group
and summarise, remove duplicates, sort) and it deliberately has no:

- **union** — "stack this month onto last month" has no expression in the
  builder. This is the likeliest first complaint and the likeliest first
  addition.
- **window functions**, pivot/unpivot, subqueries, correlated predicates, or
  non-equi / right / full / cross joins.
- **`case` beyond a single if/else**, regex, or date parsing with a format
  string.
- **incremental or streaming flows.** Both are single-input and row-wise by
  construction, so a joined or summarised flow could never be one, and offering
  the checkbox only to degrade it silently to a full rebuild would be worse than
  not offering it.

Two things it *can* do but awkwardly, so you know before you start:

- **An aggregate cannot be wrapped in a calculation.** `round(avg(x), 1)` is
  three steps — summarise, add a column, drop the scratch column — where SQL
  writes one expression.
- **A join refuses two inputs sharing a column name**, even one the pipeline
  never uses, and the fix is a "Choose columns" step on one side first. The
  refusal names every clashing column.

Two behaviours differ from SQL on purpose, because the screen says words rather
than operators:

- **"is not" and "is not one of" keep empty values.** `v is not 5` returns the
  rows where `v` is empty, which is what the sentence means to somebody who does
  not write SQL; `IS NOT NULL` (`is not empty`) is the explicit way to ask about
  them. `is` is unchanged and excludes empties.
- **A preview runs as *you*** — with your row policy and column masks — while
  the build runs as the system. A preview can never show more than you may read,
  so a policied analyst may preview twelve rows and build twelve million.

**Ejecting a flow to Python is one-way.** It writes `pipelines/<name>.py` and
deletes the flow; the visual builder cannot reopen it. There is no import in the
other direction, and there is not going to be: re-parsing Python into an IR is a
Python-source analyser, and it is wrong the first time somebody writes a helper
function.

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

**0.2.0** — see [CHANGELOG.md](CHANGELOG.md). **Nothing has been released yet:**
there is no git tag in this repository and nothing has been uploaded to PyPI
(the release workflow is inert until trusted publishing is configured), so
`pip install laurelin` does not work — install from a checkout. Early alpha:
the core loop — ingest → transform → build → ontology → act — works end to
end, with 3,132 tests run against both SQLite and PostgreSQL, but expect rough
edges and breaking changes before 1.0.

Laurelin runs as a single process on a laptop *or* as N stateless replicas
behind a load balancer — identity, workspace metadata and build coordination in
PostgreSQL, dataset Parquet in object storage (`LAURELIN_DATA_URI=s3://…`).
Compute is four roles behind one governance layer — see [Where compute
happens](#where-compute-happens). The default is DuckDB in-process per replica:
strong on governance and semantics, deliberately not a distributed compute
engine. Data too big for that is federated, delegated, or served from a
StarRocks/ClickHouse table Laurelin reads but does not operate.
[docs/SCALE.md](docs/SCALE.md) publishes measured numbers, including the
unflattering ones: the SQL path stays comfortable into the tens of millions of
rows, appends cost the delta rather than the dataset, and ontology queries run
in DuckDB (~26× faster than they were — 36 s to 1.37 s for a page over 5 M
objects). An object type can also be *indexed*, which makes key lookups
constant-time: **1.3 ms at both 200 K and 800 K objects**. What an index does
**not** make cheap is a filter on a non-key property, or counting every match
of a broad search.

Those numbers were **re-measured on 2026-08-14**, after the security work that
put an audience projection on every serialized response and a policy check on
every ontology read. Three things that re-run found, all of them in
[docs/SCALE.md](docs/SCALE.md):

- **The service layer did not regress** — query, ingest, build and ontology
  numbers all within 0.85–1.13× of the pre-security tree, benchmarked back to
  back. All six ratio claims in `bench/regression.py` still pass; CI runs them
  on every push and pull request.
- **Serializing a response got 3–5× more expensive**, on a path no published
  number covered. Half of that has been recovered; the remainder is documented
  rather than hidden, along with why an *admin* pays more than a viewer.
- **Two published numbers had already rotted** before this work, and are
  corrected: the UI row page was never flat, and object get-by-key was
  optimistic. The object-index table is now reported from a committed harness,
  because the script that produced the old one never was.

There is **no published number for the serving tier** — no benchmark, no
latency, no comparison against DuckDB.

## License

[Apache-2.0](LICENSE)
