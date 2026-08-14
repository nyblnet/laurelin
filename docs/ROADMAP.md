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
│ Compute: DuckDB per-worker  ·  delegation to Flight SQL engines   │
│ Serving: StarRocks (flagship) / ClickHouse — governed, read-only  │
│ Index: Postgres FTS → optional OpenSearch (embedded: SQLite FTS)  │
│ Queue: Postgres-backed (no Redis requirement)                     │
└───────────────────────────────────────────────────────────────────┘
```

Key bets, and why:

- **Apache Iceberg for server-mode tables.** Branching, time travel, schema
  evolution, and hidden partitioning for free — and Spark/Trino/Snowflake/DuckDB
  can all read our tables directly. This is the strongest possible "no lock-in"
  statement. Embedded mode keeps today's simple Parquet version dirs.
- **Four roles, one governance layer.** (1) *Embedded default*: DuckDB
  in-process per worker, covering the 99% — interactive plus batch up to ~1TB
  working sets. (2) *Federation*: federated datasets scanned in place
  (Iceberg/Delta/Parquet/Postgres) and delegated compute — `@remote_transform`
  submits SQL to an engine that is already distributed (Trino/Dremio/Databricks
  via Flight SQL) and stores the reduced result with full lineage and policy.
  (3) *Serving tier*: StarRocks (flagship) and ClickHouse (supported peer) —
  governed, policy-pushed-down, **read-only** queries against a table the
  serving engine owns. StarRocks leads because querying Iceberg is first-class
  there (so "open at rest" survives the serving tier), because it joins
  natively and an ontology link *is* a join, and because its primary-key tables
  have real upserts. (4) *Operational store*: the materialization of ontology
  object state, defaulting to the metadata store. Past the first role Laurelin
  does not grow a cluster and does not operate an engine. **Explicit
  non-goals: shuffle, distributed joins, a cluster manager, a cross-node query
  planner.** Those are what make a distributed engine large, and a half-built
  version would be worse than the ones that exist.
- **Postgres for metadata, queue, and search in server mode.** One dependency,
  boring, HA story well-known. No Redis/Zookeeper/Kafka required to start.
- **React + TypeScript for the web app.** The vanilla-JS SPA was right for the
  alpha; an IDE-grade surface (SQL workbench, pipeline canvas, dashboard editor)
  needs a component model, routing, and typed API clients. Still zero-CDN,
  still served by the API process.

---

## Workstreams

### WS1 — Storage & catalog (Foundry: Datasets/Catalog)

- [x] **Iceberg table format**: `kind="iceberg"` datasets, written and
      versioned by Laurelin and readable by Spark/Trino/Snowflake/DuckDB
      without it — a test opens one from a DuckDB that knows nothing about
      Laurelin. Each write is an Iceberg snapshot *and* a Laurelin version
      pinned to it, so time travel, lineage and builds share one notion of
      "when". Managed Parquet remains the default; this is opt-in per dataset.

      No REST catalog: pyiceberg's `SqlCatalog` points at the database
      Laurelin already runs, so adopting Iceberg adds no service to operate
      and a Postgres control plane becomes a shared catalog across replicas
      for free. The roadmap called for hosting a catalog; hosting one turned
      out to be unnecessary.
- [x] **Iceberg branches**: cut a branch (a named pointer into the snapshot
      history, so it copies no data), write to it without main seeing it, and
      merge as a metadata swap. **Fast-forward only** — a three-way merge of
      two diverged histories needs a row-level conflict policy, and guessing
      one silently picks a winner between two people's writes, so a diverged
      branch is refused with an explanation.
- [x] **Schema evolution**: additive by default and always allowed, since
      Iceberg tracks columns by id and old snapshots stay readable. Dropping
      or renaming requires `allow_breaking=True`, and the refusal names the
      transitive downstream datasets from lineage — the question is never "is
      this safe?" but "what breaks when I do it?"
- [~] **Iceberg tags, hidden partitioning, row-level deletes, small-file
      compaction**. *Compaction shipped* — but as a whole-table rewrite into one
      new snapshot, not Iceberg's incremental `rewrite_data_files`, so it costs
      the table and reclaims scan cost rather than disk (earlier snapshots keep
      their files; nothing expires them yet). Tags, hidden partitioning and
      row-level deletes are still open. The Arrow read path also still
      materializes through pyiceberg (the SQL path prunes via `iceberg_scan`).
- [~] **Branches & merges**: *partly done, and the checked "Iceberg branches"
      item above is the part that shipped* — cut a branch, write to it, merge
      as a fast-forward metadata swap. Still open: a `laurelin branch` CLI verb
      (branches are API/UI only today), diffing datasets between branches, and
      anything beyond fast-forward.
- [~] **Transactions**: `append` shipped (a version is a manifest of parts, so
      appending costs the delta) and `compact` merges them back. Still open:
      overwrite and upsert-by-key write modes.
- [x] **Schema evolution with enforcement**: *duplicate of the checked item
      above and shipped with it* — additive by default; dropping or renaming
      requires `allow_breaking=True` and the refusal names the transitive
      downstream datasets from lineage.
- [x] **External / federated tables**: register existing Postgres/S3
      parquet/Iceberg/Delta locations as read-only datasets (DuckDB attach),
      with the row policy and column masks compiled to SQL around the remote
      scan. Shipped as `PUT /datasets/{name}/federated`. Still open: MySQL as
      a federated source.
- [x] **ClickHouse-backed datasets** (`kind="clickhouse"`, read-only, via
      embedded **chdb** — no server to run). The *seam* was the point and
      ClickHouse was the proof; it is a supported peer in the serving tier, not
      an exercise. Proving that one policy decision renders correctly onto a
      *second* SQL dialect did not come free: five things had to change, and each
      would have been a silent leak. DuckDB's identifier quoter resolves a
      ClickHouse column to a **different column** and returns its data;
      ClickHouse's named-parameter channel is **not byte-preserving**, so
      policy values must be escaped by an audited in-repo function;
      `NULLIF(c, c)` leaves **NaN unmasked**; and because ClickHouse resolves
      `WHERE` against `SELECT` aliases, a flat statement evaluates the row
      policy **against the mask** — a total row-policy bypass that fails open.

      The seam is `laurelin/core/dialects.py`, and DuckDB's output is pinned
      byte-for-byte by a golden test so that adding an engine cannot change
      the first one. Two pre-existing fail-open bugs surfaced on the way and
      are fixed for every dialect: an empty column list rendered
      `select_list='*'` with masks pending, and a mask differing from a real
      column only in case masked nothing at all.
- [x] **StarRocks-backed datasets** (`kind="starrocks"`, read-only, over the
      MySQL wire protocol, `pip install 'laurelin[starrocks]'`). The flagship
      of the serving tier: querying Iceberg is a first-class path there so
      "open at rest" survives the serving tier rather than being traded for
      it, it joins natively and an ontology link *is* a join, and its
      primary-key tables have real upserts. First engine that is a **server**
      rather than a library, which changes the threat model more than the
      syntax: stacked statements execute, so `StarRocksDialect.literal()`
      **raises** and every query goes through a prepared cursor that StarRocks
      will not let express an INSERT (error 1295).

      **Caveats, stated rather than discovered:** reading through a StarRocks
      **Iceberg external catalog is untested** — the three-part
      `catalog.db.table` scan expression works, but the type-agreement tables
      were measured on native columns and the Iceberg→StarRocks mapping could
      move DECIMAL scale or DATETIME precision. The opt-in CI job covering the
      StarRocks suites has **never executed on GitHub Actions**; the behaviour
      is written from a local container.
- [ ] **Writes into a serving engine** (ClickHouse `INSERT` into MergeTree;
      StarRocks Stream Load for datasets), and a real ClickHouse *server*
      connection (`clickhouse-connect`, TLS, credential storage, a
      settings-profile threat analysis). Deliberately a separate slice from
      governed reads: half of one produces a dataset that is part local Parquet
      and part remote table.
- [ ] **Retention & TTL policies** per dataset (keep N versions / D days), GDPR
      purge that provably rewrites history.
- [ ] **Media sets**: blob datasets (documents, images) with metadata tables.
- [x] **Storage backends**: local FS or object storage (S3/MinIO, GCS, Azure)
      via `LAURELIN_DATA_URI`, through a pyarrow filesystem layer. A version
      commits by inserting its manifest row — no atomic directory rename — so
      the protocol is native to object stores.

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
- [x] **Uploads UI**: drag-and-drop CSV/Parquet on the Datasets page creates a
      dataset without touching the CLI. `POST /datasets/preview` infers the
      schema and samples rows *without creating anything*, so the import is a
      decision rather than a guess; the dataset name is suggested from the
      filename ("Q3 Orders (final).csv" → `q3_orders_final`) since almost no
      real filename satisfies `^[a-z][a-z0-9_]*$`. Replace/append is now
      selectable on an existing dataset (the API always supported it; the UI
      never sent it).
- [ ] **Column type overrides on import**: the preview shows inferred types but
      can't yet change them — a mis-inferred column still needs a transform.
      Excel input is also still unsupported.

### WS3 — Pipelines & orchestration (Foundry: Code Repos / Pipeline Builder / Data Health)

- [x] **Async builds (in-process)**: POST /builds returns a pending build
      immediately; an in-process worker pool (LAURELIN_BUILD_WORKERS) executes
      it off the request thread and the UI polls to convergence. `wait=true`
      keeps the old blocking behavior for scripts/tests.
- [x] **Scheduler**: cron and on-upstream-changed triggers driving builds or
      connector syncs. Every replica polls; firing requires winning a
      conditional UPDATE, so it is exactly-once without leader election, and a
      dead replica's claim expires rather than wedging the schedule. An
      overdue schedule fires once, not once per missed window.
- [ ] **Workers & queue**: N separate worker processes, retries w/ backoff,
      per-schedule concurrency limits, backfill runs.
- [x] **Incremental transforms**: `@transform(incremental=True)` receives only
      the rows its input gained since the last build and appends its result.
      The delta comes from the version manifest — appends only extend it, so a
      prefix check says precisely whether history still lines up; a rewritten
      input falls back to a full rebuild rather than double-counting.
- [x] **Data expectations**: `@expect(not_null("x"), unique("x"),
      row_count(min=1), accepted_values(...), expression(...))` with
      fail-build or `severity="warn"` modes; results stored per build task and
      surfaced on the pipeline page. Checked against the written Parquet parts
      **before the manifest row is inserted**, so a failing output is never
      published rather than published and retracted — no reader sees it, no
      downstream build consumes it, and the orphaned parts are deleted. Every
      check is SQL evaluated by DuckDB over a lazy dataset, so a streaming
      transform stays streaming.
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
- [x] **Batch-streaming transforms**: `@transform(streaming=True)` receives an
      iterator of Arrow batches and yields batches, so peak memory tracks one
      batch rather than the dataset (121 MB -> 21 MB on a 3 M-row filter).
      Exactly one input; aggregation belongs in a SQL transform.
- [ ] **Streaming ingestion** (v2): micro-batch over Kafka topics into datasets.
- [ ] **dbt interop**: import a dbt project as transforms w/ lineage mapping.
- [ ] Build UX: live log streaming (WebSocket), per-task Gantt, cancel/retry,
      build diffs (rows added/changed vs previous version).

### WS4 — Ontology (Foundry: Ontology/OSv2/Actions/Functions)

- [x] **Query pushdown**: object filter/search/count/paging execute in DuckDB
      over the backing Parquet with the edit overlay merged there, instead of
      materializing the dataset in Python — ~26× faster (36 s → 1.4 s at 5 M
      objects; point lookups 279 ms via a primary-key predicate pushed inside
      the de-duplication window). Row-level security is pushed into the same
      scan, so policied users get it too (~195 ms over 1 M objects); only hash
      masking falls back to the exact in-memory path.
- [x] **RLS predicate pushdown**: row policies apply via `Dataset.filter()`,
      which preserves DuckDB's column pruning — the 3.6× policy tax at 5 M
      rows is gone (1.0×). Column masks still need a Scanner (no pruning);
      hash masking materializes.
- [x] **Object index**: opt-in per object type; materializes objects into the
      metadata store, refreshed by builds. Key lookups are **constant time**
      (1.4 ms at 1 M objects, vs 99 ms scanning), paging is ~11× faster. A
      stale index is never read — freshness is checked against the dataset
      version *and* the edit overlay on every query, and RLS users always take
      the scan.
- [x] **Accelerated object search**: trigram indexing (SQLite FTS5 trigram /
      Postgres `pg_trgm` GIN) makes `LIKE '%…%'` sub-linear *without*
      redefining it — a selective search is 49× faster at 800 K objects and
      effectively constant. Deliberately not FTS: token matching finds
      "minas" in "Minas Tirith" but never "inas Ti", and the scan path shares
      the substring definition. Best-effort, falling back to an unindexed
      LIKE where the extension isn't available.
- [x] **Ranked search**: hits are ordered by where the term appears in the
      title, body-only matches after, applied identically by the index and
      the DuckDB scan. Reorders results without changing which ones match, so
      the substring guarantee the two paths share is untouched — no second
      FTS index needed.
- [x] **Operational object store**: an edit **upserts** the materialization
      instead of invalidating it, in the same transaction that appends it to
      the log. `edit_count` became an `applied_seq` catch-up watermark,
      `object_edits` gained a gapless per-type `edit_seq`, and each
      materialization carries a content **digest** (a watermark cannot detect
      divergence — a drifted store can be perfectly caught up by position). A
      store that cannot prove it applied every committed edit returns nothing
      and the read falls through to the scan. The backend is pluggable:
      `MetadataObjectStore` (default) or `StarRocksObjectStore` via Stream
      Load with primary-key upserts. An edit's rows are built **inside** the
      transaction that writes them, under a lock on the object type's state
      row: they are a read-modify-write of the current rows, and doing that
      read on another connection lost a committed update when two people
      edited one object, resurrected a deleted object, gave concurrent creates
      the same ordinal, and raised a false digest alarm — all while the
      watermark reported the store level with the log.
- [x] **Definition changes invalidate the materialization**:
      `object_index_state.type_fingerprint` hashes the object type's key,
      title property and declared properties. Without it, withdrawing a
      property took effect on every read path except the one that had
      materialized it, and each subsequent write copied it forward.
- [ ] **Verify `StarRocksObjectStore` against a real server, and wire it to
      configuration.** It has only ever run against an in-memory double, and
      no env var, route or config key selects it — `OntologyService`
      constructs `MetadataObjectStore`, so every deployment runs the default.
      "Pluggable" is currently a seam with an implementation behind it, not a
      switch an operator can throw. Also unimplemented there: strict per-row
      position ordering (StarRocks resolves duplicate keys by load order and
      the DDL declares no sequence column). Having no shared transaction it
      cannot take the state-row lock the metadata store uses, so it loads an
      edit only when its watermark is exactly one position behind — otherwise
      it stays behind and lets `catch_up` replay in log order. Sound, and
      slower under concurrency by construction.
- [ ] **Linguistic search**: stemming, synonyms, phrase and boolean
      operators. These change *what matches*, so they need to be an explicit
      opt-in mode rather than a silent upgrade of the default.
- [ ] **Filters on non-key properties**: would need per-type columns rather
      than one JSON blob; extracting JSON per row measured slower than the
      scan it replaced.
- [x] **Aggregations API**: `POST /ontology/objects/{type}/aggregate` —
      group-by over declared properties with count / count_distinct / sum /
      avg / min / max / median, pushed into DuckDB (437 ms over 1 M objects
      grouping with two metrics). Aggregates the *object* set, so the edit
      overlay is included — charting the backing dataset directly would answer
      from rows an action has already changed. Ops are an allowlist, not a
      passthrough, since the op becomes a SQL function name. Falls back to an
      exact in-memory pass when a policy can't be pushed down, so an aggregate
      never becomes a way to read rows you can't list.
- [ ] **Action side effects**: webhooks, notifications, and enqueue-build on
      action apply; submission criteria (declarative preconditions: role, object
      state, parameter rules).
- [ ] **Functions**: server-side logic (sandboxed Python) callable via API —
      computed properties, custom action logic, derived links.
- ◑ **Writeback datasets**: ✅ `POST /ontology/object-types/{name}/writeback`
      folds the edit overlay into a new dataset version so pipelines can
      consume user edits. Requires edit rights on the **backing dataset**, not
      just the object type, and refuses a transform-produced backing unless
      overridden — otherwise the next build silently reverts the folded edits.
      It bounds **read cost, not disk**: folded edits are *marked*, not
      deleted, because `folded_into_version` is what keeps a folded version
      reproducible. It publishes with compare-and-set
      (`catalog.write(..., expect_version=...)`), because the version check it
      had was a check-then-act: two concurrent folds could mark an edit folded
      into one version while publishing another that lacked it, and a fold
      could discard a concurrent build's whole version. It also refuses a
      backing whose declared primary key is not unique, rather than
      materializing the object view's dedup into the dataset and deleting rows
      no edit referenced. Still to do: pruning folded edits, and an automatic
      (rather than manual-only) trigger.
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
- [~] **SQL workbench**: *shipped* — a **CodeMirror** editor (not monaco),
      DuckDB over the datasets the caller may view, a result grid, charting the
      result, and `POST /pipelines/from-query` to save a query as a
      `@sql_transform`. Still open: virtualized paging over millions of result
      rows, a schema sidebar, and EXPLAIN.
- [ ] **Analysis boards** (Contour): notebook-of-panels over datasets/objects —
      filter, join, pivot, chart panels chained together; each board
      exportable as a pipeline; stored as YAML in the workspace.
- [x] **Charts & dashboards v1**: zero-dependency SVG charts (bar, line,
      area, big-number, table) on workbench results; dashboards as grids of
      saved queries with an inline panel editor and "Add to dashboard" from
      the workbench. Panels execute through /query with the *viewer's*
      credentials, so RLS/ACLs/markings apply per user.
- [x] **Object-backed panels**: a dashboard panel can chart an object type
      (group-by + metrics) instead of SQL, so it reflects the ontology's edit
      overlay. A SQL panel over the backing dataset silently disagrees with
      the object list after an action; this closes that gap. `aggregate_objects`
      is also an MCP tool, so agents stop hand-writing SQL for "how many X by Y".
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

Baseline (specced in ARCHITECTURE.md, and **shipped** — these three were left
unchecked long after the code landed):

- [x] **Local users**: scrypt-hashed passwords (`laurelin/core/auth.py`),
      httpOnly session cookies, first-run admin setup, login throttling
      (≥5 consecutive failures per username → locked 30s), 8-character minimum.
- [~] **API tokens**: per-user, named, hashed at rest (SHA-256) — shipped.
      Still open: **expiry and scoping** (read-only / read-write / admin). A
      token today carries its user's full role.
- [x] **RBAC**: viewer/editor/admin baseline, enforced by `require_role`.
      Per-project roles remain WS8.

Enterprise:

- ✅ **OIDC SSO** (Okta, Entra ID, Google, Keycloak, Auth0): authlib code-flow
      with PKCE, JIT user provisioning, group→role mapping (+ superadmin group).
      **done** *(the 90% of enterprise SSO)*
- ✅ **SAML 2.0**: SP-initiated + IdP-initiated, signed assertions (xmlsec1),
      metadata endpoint, group→role mapping. **done**
- ✅ **SCIM 2.0 provisioning**: users + groups pushed from IdP; deprovisioning
      disables the account and immediately kills its sessions + tokens. **done**
- [x] **Groups**: local (admin-managed, `/api/v1/groups`) and IdP-synced via
      SCIM `/Groups`; permissions bind to groups as a grant subject.
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
- ✅ **HA**: stateless API replicas, shared Postgres control plane, liveness +
      readiness probes, additive/idempotent migrations (safe rolling deploy),
      **per-workspace metadata in Postgres schemas** (what made multi-replica
      safe), **object-storage data plane** (no RWX volume needed), and
      **build leases** so exactly one replica executes each build. Still to do:
      a leader-elected scheduler once the async scheduler lands.
- ◑ **Observability**: ✅ health/readiness probes, Prometheus `/metrics`
      (HTTP, queries, rejections by reason, builds, lease claims, scheduler
      fires, syncs — low-cardinality labels only), structured JSON logs, and
      request-id propagation. Still to do: OpenTelemetry traces and a built-in
      status page.
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
- [x] WS7: OIDC SSO + group→role mapping — **done** (also SAML and SCIM, which
      the WS7 section marks ✅; this line had been left unchecked)
- [ ] WS8: projects with per-project roles; audit v2 (structured, exportable)
- [x] WS9: Postgres metadata backend (control plane *and* per-workspace
      schemas), Docker image + compose, migrations
- [~] WS3: scheduler — cron and on-upstream-update triggers shipped
      (`laurelin/core/scheduler.py`), leased so firing is exactly-once across
      replicas. **Not** a Postgres queue with separate worker processes: builds
      run on a per-replica thread pool and are claimed by lease.
- [x] WS1: object-storage backend (S3/GCS/Azure via pyarrow filesystems)

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

- Reimplementing Spark. Big-compute plugs in; DuckDB is the default. And not
  *operating* a serving tier either: Laurelin reads the StarRocks or ClickHouse
  you run, it does not run one for you.
- A proprietary notebook format, chart format, or DSL of any kind.
- Multi-region active-active storage replication (defer to S3/Postgres).
- Feature-flag-gated "enterprise edition". One product, Apache-2.0.

## Immediate next steps

All three items that stood here are **done** and are recorded rather than
deleted, because a roadmap that quietly loses its own history is not one:
Phase-1 local auth shipped; the React app shipped (Vite + React + TS +
TanStack Query + React Router, with **CodeMirror** rather than monaco and no
vega-lite — charts are hand-rolled SVG); and the abstraction seams exist as
`laurelin/core/backend.py` (SQLite + Postgres) and `laurelin/core/storage.py`
(local + object store).

What is actually next, verified against the tree:

1. **Verify `StarRocksObjectStore` against a real server and make it
   selectable.** It has only run against an in-memory double, and
   `OntologyService` hard-constructs `MetadataObjectStore`, so no operator can
   reach it. See the WS4 item below.
2. **Run the opt-in ClickHouse/StarRocks CI jobs on GitHub Actions.** They have
   never executed there; both engines' behaviour is written from a local
   container.
3. **Re-measure the published millisecond tables in `docs/SCALE.md`.** They
   predate the audience-projection and ontology-policy work, which sits in read
   paths. `bench/regression.py` guards the *shape* of six claims and passes,
   but nothing guards the absolute numbers.
4. **Token expiry and scoping** (WS7) — an API token currently carries its
   user's full role forever.
