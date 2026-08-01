# Changelog

Notable changes to Laurelin. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[semantic versioning](https://semver.org/), with the caveat that pre-1.0
minor releases may break things.

## Unreleased

### Ontology — the object store stopped being thrown away on every write

The complaint this answers: "the ontology is slow as an application database."
It was, and for a specific reason. The object index reported itself stale
whenever the edit *count* changed, so a single hand edit invalidated the whole
index for that object type and every subsequent read fell back to a full scan
that replayed the entire edit log. Read cost grew with total write history.

- **An edit now upserts the materialization instead of invalidating it.** For
  the default (metadata-store) backend the log append and the row upsert are
  **one transaction**, so a reader can never see "caught up" with the row not
  yet there. Measured on 100 K objects: reading a page costs **1.01× / 0.97× /
  0.96×** of the zero-edit baseline at 100 / 1 000 / 10 000 edits, and edit
  #1 000 costs **1.00×** what edit #1 did. Under the old behaviour the first
  edit sent every subsequent read back to a full scan.
- **`object_index_state.edit_count` became `applied_seq`**, a catch-up
  watermark rather than an invalidation flag, and `object_edits` gained a
  gapless per-type `edit_seq`. A store that has not applied every committed
  edit **refuses to answer** and the read falls through to the scan path, which
  is always correct. Unreachable, a missing state row and a dropped table all
  count as "behind". *This field is visible over HTTP* on
  `GET /ontology/object-types/{name}`, which also now reports `lag` and `store`.
- **A pluggable object store** (`laurelin/ontology/store.py`): the metadata
  store (default, no new dependency) or StarRocks primary-key tables via Stream
  Load. The StarRocks implementation is **unverified against a real server** —
  it has only run against an in-memory double.
- **A content digest per materialization**, XOR-combined and maintained
  incrementally, because a watermark cannot detect divergence: a store that has
  drifted can be perfectly caught up by position.
- **Writeback** (`POST /ontology/object-types/{name}/writeback`, manual only):
  folds the overlay into a new dataset version and marks the folded edits
  rather than deleting them, so the version stays reproducible. Requires edit
  rights on the **backing dataset**, not just the object type. Refuses a
  transform-produced backing by name unless overridden — otherwise the next
  build silently reverts the folded edits.

### Ontology — concurrency, governance and writeback fixes on the above

Everything in this section was demonstrated with runnable reproductions against
the code as first written, most of them with two ordinary threads calling
`apply_action`. The common shape: the store was not *behind*, it was **wrong
while level**, so the watermark reported `fresh: true, lag: 0`, reads never fell
through, and `catch_up()` had nothing to replay.

- **Fixed: a concurrent edit could be silently discarded.** An edit's rows are a
  read-modify-write of the current rows, and the read happened on a different
  connection from the write. Two people editing one object each merged onto the
  same pre-image and one committed edit became invisible to every reader; a
  delete racing an update let the update re-insert the deleted row; four
  concurrent creates all took the same ordinal; and the divergence digest was
  computed from a superseded base, so ordinary concurrent writes raised a false
  corruption alarm. `ObjectStore.commit_edit`/`apply_edit` now take a
  `build(pre_image, seq)` callback invoked *inside* the write transaction, under
  a lock on the object type's state row.
- **Fixed: on PostgreSQL, creating an object silently killed the
  materialization.** `object_index.ord` was `INTEGER` — 64-bit on SQLite, 32-bit
  on PostgreSQL — while created objects sort at `2**62 + edit_seq`. The INSERT
  overflowed, the write path swallowed it, the user saw success, and reads
  reverted permanently to the full scan this feature exists to remove; the
  rebuild endpoint then returned 500 forever. Now `BIGINT`, with a migration.
- **Fixed: `POST /ontology/object-types/{name}/index` returned 500 when an
  object was created during a rebuild.** `reindex` read its three inputs on
  three connections; they now come from one pinned snapshot.
- **Fixed: a policied user could overwrite an object their row-level security
  hides.** A create for an existing key is a replacement — it inherits the
  hidden row's position and a writeback folds it over that row in the dataset,
  destroying another tenant's data for everyone. Refused for callers a dataset
  policy narrows.
- **Fixed: withdrawing a property from the ontology did not stop it being
  served.** Every other read path projects to the declared properties; the
  materialization did not, and each subsequent write copied the withdrawn
  property forward. `object_index_state.type_fingerprint` now invalidates on a
  definition change, exactly as a dataset version does.
- **Fixed: `index.objects` disclosed the unpoliced object count** to a user
  whose row-level security shows them a subset. `objects`, `lag` and
  `applied_seq` are `null` for policied callers.
- **Fixed (StarRocks store): a failed load after the log commit was reported to
  the caller as a failed write** — for an edit that was durable and visible to
  every reader, with no audit record. `commit_edit` is now all-or-nothing by
  contract. The store also refuses to apply an edit it is not exactly one
  position behind, which is what stands in for the lock it cannot take.

**Writeback**, same treatment:

- **Fixed: two concurrent folds could destroy an edit permanently.** The version
  check was a check-then-act; publishing is now compare-and-set
  (`catalog.write(..., expect_version=...)`, raising `StaleBaseVersion`). The
  same gap let a fold discard a whole dataset version published by a concurrent
  build — the exact scenario the check was written for.
- **Fixed: a fold could not express clearing a property to NULL.** The overlay
  merged with `COALESCE`, which cannot tell an assigned NULL from an absent one,
  so folding a null-clearing edit silently restored the old value — and broke
  live null updates that had been working before the fold.
- **Fixed: a fold deleted every duplicate-primary-key row from the dataset.** It
  materialized the object view's last-wins dedup into the data. Now refused,
  with a pointer at doing the dedup in a transform where it is visible.
- **Fixed: a create over an existing key nulled that row's undeclared columns.**
- **Fixed: the transform-backed guard disappeared after its first override**,
  because writeback overwrites the version source it was reading.

### Engines — one policy decision, now rendered for three SQL dialects

Governance was "one decision, two renderers", but the SQL renderer *was*
DuckDB's SQL with a seam drawn around it. That is an abstraction now, because
two other engines implement it — and each one bent it somewhere different.

- **A dialect seam** (`laurelin/core/dialects.py`). `SqlDialect` has no safe
  defaults: every method is abstract, so a dialect that forgets one fails
  loudly instead of silently emitting DuckDB syntax. `DatasetInfo.sql_dialect`
  now names the dialect a dataset's policy must be rendered in, because
  `scans_at_source` had been quietly carrying two facts — "read via the source
  expression" *and* "DuckDB renders the SQL" — that stop being the same fact
  the moment a second engine exists. DuckDB's rendered output is pinned
  byte-for-byte by a golden test, so adding an engine cannot change the first
  one.
- **ClickHouse-backed datasets** (`kind="clickhouse"`, `pip install
  'laurelin[clickhouse]'`, `PUT /datasets/{name}/clickhouse`): read-only,
  scanned in place by **chdb** — ClickHouse embedded in the process, so there
  is no server to run. Four divergences from DuckDB were measured, and every
  one of them is a leak rather than a wrong number: DuckDB's identifier quoter
  resolves a ClickHouse column to a *different* column and returns its data;
  the named-parameter channel is not byte-preserving (`a\nb` arrives three
  bytes, not four); `NULLIF(c, c)` leaves NaN unmasked, since NaN ≠ NaN; and
  because ClickHouse resolves `WHERE` against `SELECT` aliases, a flat
  statement evaluates the row policy against the **mask** — a total row-policy
  bypass that fails open.
- **StarRocks-backed datasets** (`kind="starrocks"`, `pip install
  'laurelin[starrocks]'`, `PUT /datasets/{name}/starrocks`): read-only, read
  over the MySQL wire protocol with the row policy and column masks compiled to
  StarRocks SQL and pushed down. This is the first engine that is a *server*
  rather than a library, and that changes the threat model more than it changes
  the syntax. Stacked statements execute — `SELECT 1; INSERT INTO t VALUES
  (99)` on one `execute()` runs the INSERT — so a policy value reaching SQL as
  text would be a remote **write**, not a wrong read. `StarRocksDialect.
  literal()` therefore **raises**, and no escaper ships even unused; every
  query goes through a prepared cursor, which StarRocks refuses to let express
  an INSERT at all (error 1295). Point it at an account holding `SELECT` and
  nothing else.
- **Type portability is checked, not assumed.** A row policy is a comparison of
  a column's *text* and a hash mask is a digest of it, and the four
  stringifiers involved — Arrow's row key, Arrow's digest input, and each SQL
  engine's — do not agree. Measured: a `decimal(12,2)` tenant key with policy
  value `'1.1'` returned nothing from `/datasets/{name}/rows` and *another
  tenant's rows* from `/query`. So each dialect declares the Arrow types it
  renders identically to the Arrow reference and the renderer **refuses**
  everything else, because the alternative is a policy that admits a different
  set of rows depending on which engine ran it. `null` and `redact` masks need
  no text rendering and stay available on every column of every type. Bool is
  a portable row key on DuckDB and ClickHouse but **not** on StarRocks, where
  `CAST(b AS STRING)` is `'1'` and Arrow says `'true'`.
- **Fixed: DuckDB and StarRocks over-claimed decimals.** Both said the whole
  decimal family was portable. Past scale 6 it is *Arrow* whose rendering
  changes — a `Decimal` whose adjusted exponent falls below -6 prints in
  scientific notation, so pyarrow gives `'0E-7'` where both engines give
  `'0.0000000'` — and the corpus sampled scales 0, 2 and 6, one step short of
  the boundary. Measured on a live StarRocks server and on DuckDB. Decimals are
  now claimed only at scale 0–6; hash-masking a wider decimal was already
  emitting a token that would not join.

**Positioning, stated once:** StarRocks is the serving tier Laurelin is built
toward — querying Iceberg is a first-class path there, so "open at rest"
survives the serving tier rather than being traded for it; it joins natively,
and an ontology link *is* a join; and it has primary-key tables with real
upserts, which is what an operational store needs. ClickHouse is a fully
supported peer, not a lesser one. Both shipped in this slice. DuckDB remains
the embedded default for medium data and Trino/Dremio/Databricks remain the
federation and delegation path; none of that changed.

**Not verified, and load-bearing enough to say so.** The StarRocks read path
was measured against a StarRocks container locally, but the opt-in CI job that
runs those suites has **never executed on GitHub Actions** — it is written from
that container's behaviour. Reading a StarRocks **Iceberg external catalog** is
untested: the three-part `catalog.db.table` scan expression works, but the
type-agreement tables were measured on native StarRocks columns and the
Iceberg→StarRocks mapping could move DECIMAL scale or DATETIME precision. And
the **StarRocks object store has only ever run against an in-memory double** —
no part of it has touched a real server. It is also not selectable by
configuration: `OntologyService` constructs `MetadataObjectStore`, so every
deployment today runs the default store and the StarRocks one is a seam with
an implementation behind it, not a switch an operator can throw.

### Security

Three fixes in code that **predates both new backends** — they were found while
building the dialect seam, not caused by it.

**Nobody is exposed.** `0.2.0` was never tagged or published and `0.1.0`
existed only in the source tree, so there is no release anyone could have
installed that carries any of these. This is a changelog note, not a security
disclosure, and there is nothing to upgrade from. It is here because a
governance layer that quietly fixes its own fail-open bugs is not one.

- **`GET /datasets` and `GET /datasets/{name}` returned source config
  unredacted.** A federated PostgreSQL dataset handed
  `postgresql://user:password@host/db` to anyone who could *see* the dataset —
  the `viewer` role is enough, and both endpoints are viewer-readable by
  design. The credentials were in the `source` dict, which was never meant to
  leave the server. Redaction now happens at the single point where a
  `DatasetInfo` is serialized rather than in each route, so a new dataset kind
  cannot reopen the same hole by forgetting to opt in.
- **`SqlPolicy.render` could emit `select_list='*'` with masks still pending.**
  When column discovery returned nothing — an unreachable source, an empty
  `DESCRIBE` — the renderer joined an empty list and fell back to `*`. That is
  an *unmasked* read of a dataset that has masks to apply: fail open, in the
  one place in the codebase that must fail closed. It now refuses the read and
  explains why. A related fail-open went with it: a mask whose column name
  differed from a real column only in **case** silently masked nothing, and is
  now a refusal — while a mask on a genuinely dropped column still passes, so
  schema evolution does not start denying datasets. Both rules apply to every
  dialect, not just the one that surfaced them.
- **`is_federated` was used where `scans_at_source` was meant, at five call
  sites.** "Who owns the table" and "where does the scan happen" were the same
  question until Iceberg made them different — Iceberg is owned and versioned
  like a managed dataset but read at the source like a foreign one — and three
  of the five were live Iceberg defects, each shipped and each unhit only
  because nothing exercised that combination: a SQL transform reading an
  Iceberg input went down the local-Parquet path, which has no parts to scan;
  `GET /datasets/{name}/rows` paired an Iceberg table's *current* rows with an
  *old* version's row count; and an ontology object type could bind to an
  Iceberg-backed dataset, which the federated-only check existed to prevent.
  `DatasetInfo.scans_at_source` is now the predicate every read path branches
  on, and the kind→dialect and kind→reader maps are **total with no default** —
  an unknown kind raises rather than falling back to DuckDB, because the engine
  that would get read with the wrong dialect is always the newest and
  least-checked one, and `source_table`'s dialect-mismatch guard cannot catch
  that case: both sides would say "duckdb".

### Fixed

- **Created objects bypassed row-level security on every read path.** The edit
  overlay was applied with no policy at all, so an object created with
  `realm='beleriand'` was returned to a user restricted to `realm='valinor'`.
  Created objects now pass the backing dataset's row policy before they are
  visible, failing closed when the payload omits the policy column. Updates
  remain a narrower guarantee — see `_policy_admits` for exactly what is and is
  not covered.
- **`reindex` materialized under the calling user's policy**, and the rebuild
  endpoint is only EDITOR-gated, so a policied editor baked their narrowed view
  into the index everyone reads. It now builds under a system identity.
- **Every created object shared one ordinal** (`2**63-1`), leaving them tied
  under `ORDER BY` so paging between them was arbitrary; and a create for a key
  that already existed emitted *both* rows in the SQL path, so the pushdown
  counted one more object than the in-memory path did.
- **The search mirror was rewritten for the whole object type on every sync**,
  which would have made each single-row edit O(objects) — invisible on
  Postgres, which needs no mirror at all.

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
