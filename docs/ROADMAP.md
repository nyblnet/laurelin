# Laurelin roadmap — an enterprise-grade open alternative to Palantir Foundry

This document is the feature plan for taking Laurelin from its current
single-process alpha to an enterprise-grade data platform. It is organized as:
the target architecture, ten feature workstreams (each mapped to the Foundry
capability it replaces), and a phased delivery sequence where every phase ships
something independently useful.

## What Foundry is, functionally

To replace it we have to be honest about what it does. Foundry is six products
wearing one trenchcoat:

| Foundry capability | What it actually is | Laurelin workstream |
|---|---|---|
| Data Connection / Magritte | Connector fleet: JDBC, files, SaaS APIs, streams, CDC | WS2 Connectors |
| Datasets / Catalog | Versioned tables w/ transactions, branching, schema evolution | WS1 Storage |
| Code Repositories / Pipeline Builder | Git-backed transforms (Spark/SQL/Python), visual pipeline authoring, scheduling, data expectations | WS3 Pipelines |
| Ontology (OSv2, Actions, Functions) | Semantic object layer: typed objects/links backed by datasets, indexed for search/aggregation, validated write-back, server-side functions | WS4 Ontology |
| Contour / Quiver / Workshop / Slate | Interactive analysis, dashboards, app builder | WS5 Analysis & apps |
| Code Workspaces / OSDK | Jupyter/VS Code integration, generated typed SDKs | WS6 Developer platform |
| Platform security | SSO, SCIM, RBAC + markings, row/column policies, audit | WS7 Identity, WS8 Governance |
| Apollo / platform ops | Deployment, HA, monitoring, upgrades | WS9 Operations |
| AIP | LLM agents & assistance over the ontology | WS10 AI layer |

## Principles (the "more open" contract)

Every feature below must preserve these, or it doesn't ship:

1. **Open formats at rest.** Data readable by tools that aren't Laurelin
   (Parquet/Iceberg, SQL metadata). Exit cost stays `cp -r` / `pg_dump`.
2. **Two deployment shapes, one codebase.** *Embedded* (today: SQLite + local
   Parquet, single process, zero infra) and *Server* (Postgres + S3 + workers).
   Features degrade gracefully in embedded mode rather than disappearing.
3. **Everything has an API.** The UI may not do anything the REST API can't.
4. **Definitions are files.** Ontology, pipelines, policies, dashboards —
   diffable, reviewable, exportable as YAML/Python in git.
5. **Apache-2.0, no open-core bait.** SSO, RBAC, and audit are not "enterprise
   edition" upsells; they're in the open product.

## Target architecture (server shape)

```
┌───────────────────────────────────────────────────────────────────┐
│  Web app (React + TS)      CLI        SDKs (py/ts, generated)     │
│  MCP server (AI tools)     Jupyter    third-party OAuth2 apps     │
├───────────────────────────────────────────────────────────────────┤
│                REST + WebSocket API (FastAPI)                     │
│        AuthN: local / OIDC / SAML · AuthZ: RBAC + policies        │
├──────────────┬──────────────────┬─────────────────────────────────┤
│ Catalog svc  │ Orchestrator     │ Ontology svc                    │
│ datasets,    │ scheduler, build │ object index, actions,          │
│ branches,    │ queue, workers   │ functions, links, search        │
│ lineage      │ (N processes)    │                                 │
├──────────────┴──────────────────┴─────────────────────────────────┤
│ Metadata: Postgres (embedded: SQLite)                             │
│ Data: S3/MinIO/GCS/azblob via Iceberg tables (embedded: local FS) │
│ Compute: DuckDB per-worker (pluggable: Spark/Trino for huge jobs) │
│ Index: Postgres FTS → optional OpenSearch (embedded: SQLite FTS)  │
│ Queue: Postgres-backed (no Redis requirement)                     │
└───────────────────────────────────────────────────────────────────┘
```

Key bets, and why:

- **Apache Iceberg for server-mode tables.** Branching, time travel, schema
  evolution, and hidden partitioning for free — and Spark/Trino/Snowflake/DuckDB
  can all read our tables directly. This is the strongest possible "no lock-in"
  statement. Embedded mode keeps today's simple Parquet version dirs.
- **DuckDB as default compute, pluggable engines above it.** DuckDB covers the
  99% (interactive + batch up to ~1TB working sets). The transform API stays
  engine-agnostic (Arrow in/out, SQL text) so a Spark/Trino executor is an
  adapter, not a rewrite.
- **Postgres for metadata, queue, and search in server mode.** One dependency,
  boring, HA story well-known. No Redis/Zookeeper/Kafka required to start.
- **React + TypeScript for the web app.** The vanilla-JS SPA was right for the
  alpha; an IDE-grade surface (SQL workbench, pipeline canvas, dashboard editor)
  needs a component model, routing, and typed API clients. Still zero-CDN,
  still served by the API process.

---

## Workstreams

### WS1 — Storage & catalog (Foundry: Datasets/Catalog)

- [ ] **Iceberg table format** in server mode (pyiceberg + REST catalog we host);
      embedded mode stays Parquet-dirs. One `Dataset` abstraction over both.
- [ ] **Branches & merges**: `laurelin branch create staging`, build against a
      branch, diff datasets between branches, merge = atomic metadata swap.
      (Iceberg refs make this cheap.)
- [ ] **Transactions**: append / overwrite / upsert-by-key write modes, not just
      snapshot replace.
- [ ] **Schema evolution** with enforcement: additive by default, breaking
      changes require explicit migration + downstream impact report from lineage.
- [ ] **External / federated tables**: register existing Postgres/MySQL/S3
      parquet/Iceberg/Delta locations as read-only datasets (DuckDB attach).
- [ ] **Retention & TTL policies** per dataset (keep N versions / D days), GDPR
      purge that provably rewrites history.
- [ ] **Media sets**: blob datasets (documents, images) with metadata tables.
- [ ] Storage backends: local FS, S3, MinIO, GCS, Azure Blob (fsspec).

### WS2 — Connectors & ingestion (Foundry: Data Connection)

- [x] **First connectors (built-in)**: sources stored in metadata with secret
      redaction; `postgres` (server-side cursor streamed to Parquet in
      batches), `http` (CSV/Parquet fetch), `file` (server-side path/glob).
      Admin-managed, editor-triggered syncs; results are normal dataset
      versions (`sync:<type>`) so lineage/ACLs/markings apply. UI on the
      Datasets page.
- [ ] **Connector plugin SDK**: a connector is a pip-installable package
      exposing `extract() -> Arrow batches` + config schema + secret refs;
      discovered via entry points. First-party set: MySQL, SQL Server,
      Oracle, S3/GCS/azblob file drops, SFTP, HTTP/REST (w/ pagination recipes),
      Google Sheets, Kafka (streaming v2).
- [x] **Incremental syncs**: `mode: "append"` + `cursor_column` pulls only
      rows above the stored high-water mark and appends them — O(delta) in
      both directions. Full-refresh remains the default; per-sync health
      status is recorded. (Still to do: *scheduled* pulls — see WS3.)
- [x] **Incremental storage**: a dataset version is a manifest of Parquet
      parts, so `append` writes only the delta and references prior parts
      (~70× faster than a rewrite on 5 M rows w/ a 1% delta); `compact()`
      merges parts back.
- [ ] **CDC**: Debezium-format ingestion from Kafka; Postgres logical
      replication direct (no Kafka) for the common case.
- [ ] **Agent mode**: an outbound-only ingestion agent for data behind
      firewalls (mirrors Foundry's agent), same connector SDK.
- [ ] **Secrets management**: encrypted-at-rest secret store (Fernet w/ KMS-style
      key wrapping), env/Vault providers, secrets never in logs or API responses.
- [ ] **Uploads UI**: drag-drop CSV/Parquet/Excel with schema preview + column
      type overrides (exists in basic form; grow into wizard).

### WS3 — Pipelines & orchestration (Foundry: Code Repos / Pipeline Builder / Data Health)

- [x] **Async builds (in-process)**: POST /builds returns a pending build
      immediately; an in-process worker pool (LAURELIN_BUILD_WORKERS) executes
      it off the request thread and the UI polls to convergence. `wait=true`
      keeps the old blocking behavior for scripts/tests.
- [ ] **Scheduler & workers**: cron + event triggers (upstream dataset updated),
      Postgres-backed job queue, N separate worker processes, retries w/
      backoff, timeouts, concurrency limits, backfill runs. Embedded mode:
      in-process scheduler thread.
- [ ] **Incremental transforms**: `@transform(incremental=True)` receiving only
      new/changed input partitions; snapshot fallback on schema change.
- [ ] **Data expectations**: `@expect(col("x").not_null(), row_count > 0)` —
      fail-build or warn modes, results stored, surfaced on lineage + dataset
      pages (Foundry's Data Health).
- [ ] **Git-native pipeline repos**: pipelines live in any git repo; Laurelin
      registers a repo+ref, checks out/builds from it, PR-preview builds against
      a data branch. CI helper (`laurelin ci check`) validates DAG + expectations
      offline.
- ✅ **In-browser transform authoring** — a code editor over `pipelines/*.py`
      (Python + `@sql_transform`), "save query as transform" from the workbench;
      editor-gated, disableable with `--lock-pipelines`. **done**
- [ ] **Visual pipeline builder** (later phase): node/edge canvas that emits the
      same Python/SQL files — the visual layer is a *view over code*, never a
      proprietary format.
- [ ] **Streaming transforms** (v2): micro-batch over Kafka topics into datasets.
- [ ] **dbt interop**: import a dbt project as transforms w/ lineage mapping.
- [ ] Build UX: live log streaming (WebSocket), per-task Gantt, cancel/retry,
      build diffs (rows added/changed vs previous version).

### WS4 — Ontology (Foundry: Ontology/OSv2/Actions/Functions)

- [ ] **Object index**: materialize objects into indexed storage (Postgres
      tables w/ GIN/FTS; embedded: SQLite FTS5) instead of per-request DuckDB
      scans — sub-100ms search/filter/aggregate over millions of objects,
      incremental re-index on dataset build.
- [ ] **Aggregations API**: group-by/count/sum/min/max/percentiles over objects,
      powering dashboards.
- [ ] **Action side effects**: webhooks, notifications, and enqueue-build on
      action apply; submission criteria (declarative preconditions: role, object
      state, parameter rules).
- [ ] **Functions**: server-side logic (sandboxed Python) callable via API —
      computed properties, custom action logic, derived links.
- [ ] **Writeback datasets**: edits materialized back into a dataset so
      pipelines can consume user edits (closing Foundry's writeback loop).
- [ ] **Time series properties**: attach a timeseries dataset to an object type;
      efficient range queries + downsampling for charts.
- [ ] **Geospatial**: point/geometry property types (DuckDB spatial), map query
      API (bbox/radius).
- [ ] **Ontology proposals**: schema changes as reviewable diffs with
      migration plan (rename/retype/backfill).

### WS5 — Analysis & visualization web app (Foundry: Contour/Quiver/Workshop/Object Explorer)

The web app is rebuilt in React+TS as an extensible workbench ("everything is a
panel"), keeping the current information-dense dark aesthetic:

- [ ] **Platform shell**: project switcher, global search (datasets, objects,
      transforms, docs), command palette, notifications, keyboard-first.
- [ ] **Data manager**: dataset browser w/ schema/version/lineage/expectation
      tabs, branch switcher, upload wizard, retention controls, column stats
      (null %, distinct, min/max, histograms — computed on write).
- [ ] **SQL workbench**: monaco editor, DuckDB over any datasets, schema
      sidebar, result grid (virtualized, millions of rows), EXPLAIN, save query
      as a SQL transform or a chart in one click.
- [ ] **Analysis boards** (Contour): notebook-of-panels over datasets/objects —
      filter, join, pivot, chart panels chained together; each board
      exportable as a pipeline; stored as YAML in the workspace.
- [x] **Charts & dashboards v1**: zero-dependency SVG charts (bar, line,
      area, big-number, table) on workbench results; dashboards as grids of
      saved queries with an inline panel editor and "Add to dashboard" from
      the workbench. Panels execute through /query with the *viewer's*
      credentials, so RLS/ACLs/markings apply per user.
- [ ] **Charts & dashboards v2** (Quiver): scatter, histogram, heatmap, map;
      cross-filtering, auto-refresh, parameter controls (date range, object
      picker); dashboards as YAML files; share links; PNG/CSV export.
- [ ] **Object explorer v2**: faceted search, saved object sets, bulk actions,
      link graph visualization (interactive network), object timelines
      (time series props), map view (geo props).
- [ ] **Lineage explorer**: full-graph pan/zoom, column-level lineage (v2),
      impact analysis ("what breaks if I change this"), expectation status
      overlay.
- [ ] **App builder-lite** (Workshop, later): compose object tables, forms
      (actions), charts into shareable internal apps — declarative YAML,
      no proprietary runtime.
- [ ] **Realtime**: WebSocket push for build status, dataset updates,
      notifications.

### WS6 — Developer platform & coding-tool integration (Foundry: Code Workspaces/OSDK)

- [ ] **Generated typed SDKs (OSDK equivalent)**: `laurelin sdk generate
      python|typescript` emits a typed client from the ontology —
      `Aircraft.search().where(A.status == "maintenance")`, typed actions.
      This is Foundry's stickiest feature; ours is open codegen.
- [ ] **Jupyter integration**: `laurelin.notebook` client (auth'd dataset
      read/write as Arrow/pandas/polars) + optional hosted JupyterLab
      (server mode, per-user kernels, workspace-mounted).
- [x] **MCP server** (`laurelin mcp`, `pip install laurelin[mcp]`): datasets,
      SQL, ontology search/get/links, actions, lineage, builds, and source
      syncs as MCP tools over stdio. Every call goes through the REST API
      with an API token, so the agent is a scoped, audited user — no side
      door. Ships with `laurelin.mcp.LaurelinClient`, a small Python SDK.
- [ ] **VS Code extension**: workspace explorer, run/build transforms,
      dataset preview, ontology autocomplete for SDKs.
- [ ] **API completeness**: pagination/filtering conventions, idempotency keys
      on mutations, webhooks for platform events, OpenAPI-generated docs site.
- [ ] **laurelin-lite client**: zero-dependency Python client (requests+pyarrow)
      for pipelines running elsewhere (Airflow/Dagster tasks can read/write
      Laurelin datasets).

### WS7 — Identity & authentication (Foundry: platform SSO)

Baseline (already specced in ARCHITECTURE.md, partially underway):

- [ ] **Local users**: scrypt-hashed passwords, httpOnly session cookies,
      first-run admin setup, login throttling, password policy.
- [ ] **API tokens**: per-user, named, hashed at rest, expiring, scoped
      (read-only / read-write / admin).
- [ ] **RBAC**: viewer/editor/admin baseline, then per-project roles (WS8).

Enterprise:

- ✅ **OIDC SSO** (Okta, Entra ID, Google, Keycloak, Auth0): authlib code-flow
      with PKCE, JIT user provisioning, group→role mapping (+ superadmin group).
      **done** *(the 90% of enterprise SSO)*
- ✅ **SAML 2.0**: SP-initiated + IdP-initiated, signed assertions (xmlsec1),
      metadata endpoint, group→role mapping. **done**
- ✅ **SCIM 2.0 provisioning**: users + groups pushed from IdP; deprovisioning
      disables the account and immediately kills its sessions + tokens. **done**
- [ ] **Groups**: local + IdP-synced; permissions bind to groups.
- [ ] **MFA (TOTP)** for local accounts (SSO deployments delegate MFA to IdP).
- [ ] **Service accounts**: non-interactive principals for pipelines/agents,
      token-only, ownable by teams.
- [ ] **OAuth2 provider**: Laurelin as an authorization server so third-party
      apps/SDKs do auth-code flow instead of pasting API tokens.
- [ ] **Session management UI**: active sessions/devices, revoke, admin force-logout.

### WS8 — Governance & authorization (Foundry: Projects/markings/checkpoints)

- ✅ **Multi-workspace hosting** (`serve --root`): one server hosts many isolated
      workspaces with global identity + per-workspace membership roles and a
      superadmin tier. This is the tenancy/organization unit we shipped instead of
      in-workspace "projects". **done**
- [ ] **Projects (spaces) within a workspace**: a finer org unit *inside* a
      workspace (datasets/pipelines/ontology grouped, per-project grants). Still
      open — multi-workspace covers coarse-grained tenancy; projects would add
      intra-workspace structure.
- ✅ **Fine-grained policies**: per-object-type ontology access, per-dataset
      ACLs, AND row-level security + column masking (per-subject row rules;
      null/redact/hash masks with exemptions) — all enforced uniformly on the row
      API, SQL workbench, and ontology objects, and composed together. **done**
- ✅ **Markings (mandatory access control)**: classification labels (PII,
      CONFIDENTIAL, …) that *propagate through lineage* — a derived dataset
      inherits its inputs' markings on build; a non-admin needs clearance for
      every marking to see the data, on every read path. Foundry's crown jewel,
      done. **done**
- [ ] **Approvals**: protected actions/datasets require second-person approval
      (request → review → apply, all audited).
- [ ] **Audit v2**: structured events for every read/write/login/permission
      change, tamper-evident hash chain, streaming export (syslog/S3/webhook),
      retention policy, compliance reports (who accessed dataset X in range Y).
- [ ] **Data deletion workflows**: subject-erasure across datasets + versions +
      indexes with certificate of deletion (GDPR/CCPA).

### WS9 — Operations & deployment (Foundry: Apollo, minus the hubris)

- [ ] **Server mode**: config file + env, Postgres + S3 backends, `laurelin
      server` (api) / `laurelin worker` / `laurelin scheduler` processes.
- ◑ **Storage backend**: ✅ metadata/control stores run on SQLite or PostgreSQL
      via a dialect adapter; the control plane can be Postgres (`--control-db`)
      for multi-tenant deployments. **done** (per-workspace data still SQLite dirs)
- ✅ **Packaging**: Docker image + docker-compose AND a **Helm chart** (lint-clean;
      Deployment/Service/Ingress/HPA/PVC/Secret) with a deployment guide.
      **done**; still to do: registry-published images.
- ◑ **HA**: ✅ stateless API replicas, shared Postgres control plane, liveness +
      readiness probes, additive/idempotent migrations (safe rolling deploy).
      **done** for the API/identity tier; still to do: shared/object storage for
      the per-workspace data plane (so >1 replica doesn't need a RWX volume), and
      a leader-elected scheduler once the async scheduler lands.
- ◑ **Observability**: ✅ health/readiness probes. Still to do: Prometheus
      metrics, OpenTelemetry traces, structured JSON logs, built-in status page (queue depth,
      build latency, index lag).
- [ ] **Backups & DR**: `laurelin backup` (metadata dump + data manifest),
      point-in-time restore docs, disaster-recovery runbook.
- [ ] **Performance targets** (gate for "enterprise-grade" claim): 10k datasets,
      1k transforms/DAG, 10M ontology objects searchable <100ms p95, 100
      concurrent UI users on a 3-node deployment.
- [ ] **Air-gapped install**: offline wheels/images bundle, no phone-home,
      (optional, off-by-default, anonymous usage ping).
- [ ] **Security posture**: threat model doc, dependency scanning, signed
      releases, SBOM, security.md + disclosure process, third-party pentest
      before 1.0.

### WS10 — AI layer (Foundry: AIP) — deliberately last, deliberately thin

- [ ] MCP server (WS6) is the foundation: any agent, any vendor.
- [ ] **Semantic search** over datasets/objects/docs (embeddings, pluggable
      providers incl. local).
- [ ] **Assist features**: NL→SQL in workbench, NL→chart in dashboards,
      pipeline doc generation, anomaly summaries on expectations failures.
- [ ] **Governed agent actions**: agents act as service accounts through the
      normal permission/audit path — no side door.

---

## Phased delivery

Each phase is shippable and useful on its own. Rough scale assumes 2–4 active
contributors; phases overlap in practice.

### Phase 1 — Trustworthy core (v0.2, ~2-3 months)
The "you can put this on a server without embarrassment" release.
- ✅ WS7: local auth (users, sessions, RBAC, API tokens, first-run setup, login UI) — **done**
- ✅ WS5: React + TS shell replacing the vanilla SPA at feature parity + login/setup,
  SQL workbench (first new surface) — **done**
- [ ] WS7: OIDC SSO + group→role mapping
- [ ] WS8: projects with per-project roles; audit v2 (structured, exportable)
- [ ] WS9: Postgres metadata backend, Docker image + compose, metrics, migrations
- [ ] WS3: scheduler (cron + on-upstream-update) with Postgres queue + workers
- [ ] WS1: S3/fsspec storage backend

### Phase 2 — Pipeline platform (v0.3, ~3 months)
Competes with "Foundry for pipelines" + basic BI.
- WS1: Iceberg tables, branches, append/upsert transactions, retention
- WS2: connector SDK + Postgres/MySQL/S3/SFTP/REST connectors, syncs w/ cursors, secret store
- WS3: incremental transforms, data expectations + health page, git-repo
  pipelines, live build logs, backfills
- WS5: data manager v2 (column stats, expectation tabs), charts + dashboards v1
- WS6: laurelin-lite client, webhooks

### Phase 3 — Ontology platform (v0.4, ~3 months)
The differentiator: nobody open-source has a good ontology layer.
- WS4: object index (Postgres/SQLite FTS), aggregations API, action submission
  criteria + side effects (webhooks/notifications), writeback datasets, functions v1
- WS6: **SDK codegen (python/ts)**, MCP server, Jupyter client
- WS5: object explorer v2 (facets, link graph, bulk actions), lineage explorer v2
- WS8: row-level security + column masking

### Phase 4 — Analysis & apps (v0.5, ~3 months)
- WS5: analysis boards (Contour-like), dashboard cross-filtering + sharing,
  app builder-lite, realtime updates
- WS4: time series + geospatial properties, map panels
- WS3: visual pipeline builder (view-over-code), dbt import
- WS2: CDC (Postgres logical replication), agent mode

### Phase 5 — Enterprise hardening (v1.0, ~3-4 months)
- WS7: SAML, SCIM, MFA, service accounts, OAuth2 provider, session mgmt UI
- WS8: markings w/ lineage propagation, approvals, deletion workflows
- WS9: Helm/HA, backup/restore, perf targets met + published benchmarks,
  pentest + security docs, air-gap bundle
- WS10: semantic search + NL→SQL assist (optional module)

### Continuous
- Docs site (operator guide, API reference), example gallery,
  integration tests against real IdPs (Keycloak in CI), release cadence +
  LTS policy, CONTRIBUTING/governance for outside contributors.
- [x] Task-shaped tutorials ([docs/tutorials](tutorials/)), `SECURITY.md`
      (security model + trust boundaries + disclosure), and published
      reproducible benchmarks ([SCALE.md](SCALE.md), `bench/benchmark.py`).

## Non-goals

- Reimplementing Spark. Big-compute plugs in; DuckDB is the default.
- A proprietary notebook format, chart format, or DSL of any kind.
- Multi-region active-active storage replication (defer to S3/Postgres).
- Feature-flag-gated "enterprise edition". One product, Apache-2.0.

## Immediate next steps

1. Finish Phase-1 local auth per the spec in ARCHITECTURE.md (underway).
2. Decide React app scaffolding (Vite + React + TS, self-hosted fonts,
   TanStack Router/Query, monaco, vega-lite) and port the four existing views.
3. Introduce the storage/metadata abstraction seams (`MetadataStore` →
   interface w/ SQLite + Postgres impls; `DatasetStorage` → local + fsspec)
   before more features accrete on the SQLite-only paths.
