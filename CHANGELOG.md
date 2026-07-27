# Changelog

Notable changes to Laurelin. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[semantic versioning](https://semver.org/), with the caveat that pre-1.0
minor releases may break things.

## 0.2.0 — 2026-07-27

**The first published release.** `0.1.0` existed only in the source tree and
was never tagged or uploaded, so there is nothing to upgrade from — this
describes what Laurelin *is*, not what changed since something you could have
installed.

### Storage

- **Versions are manifests of Parquet parts.** `append` writes only the new
  rows and references the previous version's parts, so adding today's data
  costs today's data: **72× faster than a rewrite** on a 5 M-row dataset with a
  1% delta, and the advantage widens as the dataset grows. `compact()` merges
  parts back when you want to pay that cost deliberately;
  `LAURELIN_AUTO_COMPACT_PARTS` does it on a threshold.
- **Object storage** for dataset Parquet (`s3://`, `gs://`, `abfs://`) via
  `LAURELIN_DATA_URI`. A version commits by inserting its manifest row rather
  than renaming a directory, so the protocol is native to object stores.
- **Apache Iceberg datasets** (`pip install 'laurelin[iceberg]'`): written and
  versioned by Laurelin, readable by Spark/Trino/Snowflake/DuckDB without it.
  Each write is a snapshot *and* a Laurelin version pinned to it, so time
  travel, lineage and builds share one notion of "when". Branches are named
  pointers into the snapshot history (cutting one copies no data); merges
  fast-forward. Schema changes are additive by default, and dropping or
  renaming a column names the transitive downstream datasets before it lets
  you. No REST catalog to run — pyiceberg's `SqlCatalog` points at the
  database Laurelin already has.
- **Federated datasets**: govern Iceberg/Delta/Parquet/PostgreSQL tables
  without holding the bytes, scanned in place with pushdown.

### Ontology

- **Query pushdown**: object queries run in DuckDB over Parquet instead of
  materializing every row in Python — **36 s → 1.4 s at 5 M objects**.
- **An opt-in object index**: paging 293 ms → 27 ms at 1 M objects, and key
  lookups **constant-time** at 1.4 ms. A stale index is never read — freshness
  is checked against both the dataset version and the edit count on every
  query, and a request carrying row-level security never touches it.
- **Trigram-accelerated search**: a selective search over 800 K objects goes
  **288 ms → 5.9 ms** and stays flat. Deliberately trigram, not full-text:
  search means *substring*, and token matching would silently redefine it.
  Hits are ranked by where the term appears in the title.
- **Aggregations**: `POST /ontology/objects/{type}/aggregate` — group-by with
  count / sum / avg / min / max / median / count_distinct, pushed into DuckDB
  (437 ms over 1 M objects). It aggregates *objects*, so the edit overlay is
  included; SQL over the backing dataset is not.
- **Object apps**: curated single-type views — the columns that matter, the
  filters that scope them, and only the actions an operator should reach for.

### Pipelines

- **Async builds** with a worker pool, leased so exactly one replica executes
  each and a dead replica's work is reclaimed rather than stranded.
- **Streaming transforms** (`streaming=True`): peak memory tracks a batch, not
  the dataset — 121 MB → 21 MB on 3 M rows.
- **Incremental transforms** (`incremental=True`): process only the rows an
  input has gained. Because a version is a manifest, "did this input only
  grow?" is a prefix comparison rather than a guess.
- **Data expectations**: `@expect(not_null(...), unique(...), row_count(min=1))`.
  Checked against the written Parquet **before the manifest row is inserted**,
  so a failing output is never published rather than published and retracted.
- **Scheduling**: cron and on-upstream triggers, leased so firing is
  exactly-once across replicas with no leader election.
- **Connectors**: PostgreSQL, HTTP and file sources, with incremental syncs
  that pull only rows above a cursor high-water mark.
- **Delegated compute**: `@remote_transform` submits SQL to Trino, Dremio,
  Databricks — anything speaking Flight SQL — and stores the reduced result
  with lineage intact. Laurelin runs no cluster and does not intend to.

### Governance

- Local users, **OIDC** (PKCE), **SAML 2.0**, and **SCIM** provisioning.
- RBAC, per-dataset ACLs, and ontology grants that **compose**: object access
  requires both the ontology grant and access to the backing dataset.
- **Classification markings** that propagate through lineage, so a pipeline
  cannot launder classified data into an unmarked output.
- **Row-level security and column masking**, pushed into the scan. The policy
  tax at 5 M rows went from 3.6× to **1.0×** — one decision, rendered to
  either Arrow or SQL, so federated and Iceberg tables are covered by the same
  interpretation of a rule.
- Append-only audit log.

### Operations

- **Horizontal scaling**: PostgreSQL schema-per-workspace, object storage, and
  leased builds. With both, nothing is node-local and replicas are
  interchangeable. Embedded mode (SQLite) remains single-replica by
  construction, and says so.
- **Query resource limits**: memory, wall-clock and concurrency, so one
  expensive query fails its own request rather than the replica.
- **Observability**: Prometheus `/metrics`, JSON logs, and an `X-Request-ID` on
  every response and log line.
- Docker Compose, a Helm chart, and a readiness probe.

### Interfaces

- React + TypeScript UI served as one self-contained HTML file, zero CDN:
  datasets, SQL workbench, pipeline canvas, dashboards, ontology explorer,
  object apps, admin.
- **Drag-and-drop import** with a schema preview before anything is created.
- **Dashboards** with SVG charts and no chart library. Panels draw from SQL or
  from an object aggregation — the latter reflects the edit overlay, which SQL
  over the backing dataset does not.
- **MCP server** so an agent is a scoped, audited user rather than a side door.
- Python SDK and a `laurelin` CLI.

### Quality

- **662 tests**, run against SQLite *and* PostgreSQL.
- **CI** across Python 3.11–3.14, with a guard that fails the run if the
  PostgreSQL suite silently skipped.
- **The tutorials execute in CI.** Running them found three defects a reader
  would have hit, including a path that stopped existing when versions became
  manifests.
- **A benchmark gate** asserting the scaling claims published in
  [docs/SCALE.md](docs/SCALE.md) as ratios, so a lost pushdown fails the build.

### Known limitations

Published deliberately rather than discovered later — see
[docs/SCALE.md](docs/SCALE.md#known-limitations-plainly) for the current list
with numbers. The short version: compute is DuckDB in one process and there
are no plans to change that; builds don't spread across replicas; object edits
are an overlay that doesn't flow back into Parquet; horizontal scaling needs
PostgreSQL; and Iceberg merges are fast-forward only.

**Not battle-tested.** It is early. It has a coherent design and a lot of
tests; it does not have production hours behind it.
