# What Laurelin is, what it isn't, and how far it goes

Most data platforms describe their scale in adjectives. This page uses numbers,
including the ones that aren't flattering. If you are evaluating Laurelin for
real work, the fastest way to a good decision is to know where it stops.

Reproduce most of it with:

```bash
python bench/benchmark.py --rows 10000,100000,1000000,5000000   # the ms tables
python bench/regression.py --report                             # the six claims, as ratios
python bench/serialize_cost.py                                  # the audience projection
```

Not *everything*: `benchmark.py` does not measure the object index, so that one
table comes from elsewhere and says so where it appears.

---

## What "medium data" means here

The band between *small enough that a spreadsheet or one file is fine* and
*large enough to need a cluster*. Concretely for Laurelin: **~1M–50M rows per
dataset is the sweet spot, ~100M is workable.** At 5M rows an 8-column table
is 73 MB of Parquet and aggregates in 72 ms; 1B rows would be ~15 GB, past the
design.

Since replicas share Postgres and object storage, the *total* data under
management is effectively unbounded — many workspaces, many datasets. The
medium-data limit applies to **the working set of a single query**, because
each query runs in one DuckDB process on one replica. "Can Laurelin hold our
data?" is largely yes; "can one query scan 500M rows?" is still no.

That describes the **embedded default** — one of four compute roles, and the
one every deployment gets for free. The other shapes are reached through the
federation and serving roles; see
[Four roles, one governance layer](#four-roles-one-governance-layer).

For the data that *is* too big, see **federated datasets** below: Laurelin
governs the table without holding it, and you reduce at the boundary.

## What this is

- **A medium-data platform** with governance and an ontology layer that OSS
  mostly doesn't have. Its job is to make a team's data modeled, governed, and
  operable — not to be a warehouse.
- **A governance layer over data it doesn't own**, when the data is too big to
  hold (federated datasets).
- **Open at every layer.** Parquet data, YAML ontology, Python pipelines,
  SQLite/Postgres metadata, a documented REST API. Every artifact is readable
  without Laurelin running, and there is no proprietary format anywhere in the
  stack.
- **Governance-complete for its size class.** Local/OIDC/SAML SSO, SCIM,
  RBAC + per-dataset ACLs, row-level security, column masking, and mandatory
  classification markings that propagate through lineage.

## What this isn't

- **Not a distributed compute engine, by design.** Compute is DuckDB in one
  process — no cluster, no shuffle, no Spark, and none planned. For data past
  that, Laurelin *delegates* to an engine that is already distributed and
  governs the result (see "delegated compute"). Building a worse Trino is not
  on the roadmap. A serving tier does not change this: Laurelin runs no
  cluster, and now reads two more engines it does not operate.
- **Not a streaming platform.** Ingestion is batch pulls and file drops. No
  CDC, no Kafka, no sub-second freshness.
- **Not a multi-tenant SaaS with hostile tenants.** Pipelines are Python the
  server executes; an editor who can write a pipeline has code execution.
  `--lock-pipelines` exists for exactly this, but the honest posture is:
  editors are trusted colleagues. See [SECURITY.md](../SECURITY.md).
- **Not battle-tested.** It is early. It has 3,132 tests, run against both
  SQLite and PostgreSQL, and a coherent design; it does not have years of
  production hours behind it.

---

## Measured performance

One 24-core Linux box (Ryzen 9 5900X, 128 GB), Python 3.14.6, DuckDB 1.5.4,
PyArrow 24.0.0, local NVMe. An 8-column `orders` table (ints, floats, strings,
a text note). Best of 3 runs. **Your hardware will differ — the shape of the
curve is the point, not the absolute milliseconds.**

**Re-measured 2026-08-14**, on the same box, after the security work that put
an audience projection on every serialized response, a policy check on every
ontology read, and structured `Failure` conversion at every catch site. Every
table in this section was re-run with `bench/benchmark.py` on that tree; the
previous numbers had been taken before that work and nobody had checked them
since. What the re-run found, stated plainly:

- **The service layer did not regress.** Query, ingest, build, incremental and
  ontology numbers all landed within 0.85–1.13× of the pre-security tree when
  the two were benchmarked back to back. The one number outside that band
  (`GROUP BY` on high-cardinality keys at 5 M, 1.42×) is a single sample on a
  shared box and is not reproducible.
- **The serialization layer did regress, badly, and it was never in this
  document.** See [What the audience projection
  costs](#what-the-audience-projection-costs) — that section is new, because
  the cost is new.
- **Two numbers below had already rotted before the security work** and are
  corrected here: the UI row page was never flat, and the ontology
  get-by-primary-key column was optimistic. Both are wrong in the *pre*-security
  tree too, so neither is this session's doing — they are the third and fourth
  numbers in this repo to rot unnoticed, which is why the re-measurement column
  now names the date it was taken.

A caveat on the conditions, because it changes how much these are worth. The
box was **not idle**: other work ran on it throughout, and the 1-minute load
average sat between 1.2 and 3.3 during the runs rather than at 0. The defence
was to benchmark the two trees **back to back in the same window** and to take
minima across repeats, so contention lands on both arms and the *ratios*
survive it. The ratios are trustworthy. The absolute milliseconds are honest
readings of a moderately busy machine and a quiet one would beat them,
particularly on the disk-bound rows (index builds especially).

### The query path — strong, and sub-linear

| Rows | Parquet | Aggregate<br>(GROUP BY) | Filter + top-N | Point lookup | Row page<br>(UI grid) |
|---:|---:|---:|---:|---:|---:|
| 10 K | 0.2 MB | 13 ms | 12 ms | 13 ms | 11 ms |
| 100 K | 1.4 MB | 19 ms | 15 ms | 18 ms | 16 ms |
| 1 M | 13 MB | 38 ms | 37 ms | 48 ms | 50 ms |
| 5 M | 73 MB | 77 ms | 76 ms | 41 ms | 73 ms |

500× the rows costs ~6× the time on an aggregate. That's projection and filter
pushdown into the Parquet scan doing its job: memory tracks the *result*, not
the dataset.

**Correction:** this table used to print 15 ms for the row page at 1 M and 5 M
and called it "flat". It is not flat and it never was — it grows roughly with
the square root of the dataset, to 50 ms at 1 M and 73 ms at 5 M. The
pre-security tree measures 45 ms and 86 ms on the same probe, so this is a
number that rotted at some earlier point and was carried forward, not a
regression from the security work. Still comfortably interactive; just not the
constant it was advertised as.

**Verdict: the SQL workbench and dashboards are comfortable into the tens of
millions of rows.** This is the part of Laurelin that scales the way you'd
hope.

### Ingest and builds — fine

| Rows | Ingest (write a version) | Two-stage build (Python filter → SQL rollup) |
|---:|---:|---:|
| 10 K | 7 ms | 32 ms |
| 100 K | 22 ms | 53 ms |
| 1 M | 134 ms | 214 ms |
| 5 M | 706 ms | 923 ms |

Roughly linear, ~7 M rows/sec on ingest. A 50 M-row dataset ingests in about
7 s and builds in about 8 s — slow enough to want the async build path
(the default), fast enough not to be the problem.

### Incremental writes — O(delta), not O(dataset)

A dataset version is a *manifest of Parquet parts*. `append` writes only the
new rows as one part and references the previous version's parts, so adding
today's data costs today's data — not a rewrite of everything you already
have. Same delta (1% of the dataset), two ways:

| Dataset | Full rewrite | Append | Speedup | Bytes written |
|---:|---:|---:|---:|---|
| 100 K | 24 ms | 3.4 ms | 7× | 1.5 MB → 27 KB |
| 1 M | 216 ms | 5.4 ms | 40× | 13.4 MB → 186 KB |
| 5 M | 918 ms | 12.1 ms | 76× | 77.6 MB → 787 KB |

**The advantage widens as the dataset grows**, because the rewrite is linear
in total size and the append is linear in the delta. That's the difference
between a nightly sync that gets slower every month and one that doesn't.

Connector syncs use it: set `mode: "append"` with a `cursor_column` and each
sync pulls only rows above the last high-water mark, then appends them. A
sync with nothing new is a no-op and mints no version.

The trade-off is part accumulation — many small files slow scans — so
`POST /datasets/{name}/compact` merges them back into one file when you want
to pay that cost deliberately.

### Streaming transforms — memory tracks a batch, not the dataset

By default a `@transform` receives its input as one in-memory `pyarrow.Table`,
so peak memory is roughly its inputs plus its output. Add `streaming=True` and
it receives an *iterator* of batches and yields batches, which flow straight
to the Parquet writer:

```python
@transform(output=Output("clean_orders"), streaming=True, orders=Input("raw_orders"))
def clean_orders(orders):
    for batch in orders:
        yield batch.filter(pc.field("status") != "returned")
```

Measured on a 3 M-row table (~216 MB uncompressed), same filter both ways:

| Transform | Peak RSS delta |
|---|---:|
| whole-table | 121 MB |
| `streaming=True` | **21 MB** |

The streaming figure barely moves with dataset size; the whole-table one
scales with it. Two constraints, both deliberate:

- **Exactly one input.** Two independent batch streams have no meaningful
  alignment, and pretending otherwise would silently produce wrong results.
  The decorator refuses at import time.
- **No aggregation.** A running total across batches works (the transform owns
  the loop), but anything needing all rows at once should be a **SQL
  transform** — DuckDB streams and spills those natively.

### Row-level security — now essentially free

| Rows | Aggregate, no policy | Aggregate, RLS active | Cost |
|---:|---:|---:|---:|
| 1 M | 38 ms | 35 ms | **0.9×** *(was 2.5×)* |
| 5 M | 77 ms | 70 ms | **0.9×** *(was 3.6×)* |

Re-measured 2026-08-14 and still free — `bench/regression.py` puts the same
claim at 0.95× on a 400 K-row aggregate, against a gate of 2.0×.

Row policies used to materialize the whole table and filter it in the policy
engine before DuckDB saw it, costing 3–4× at scale. A policy that can be
expressed as an Arrow filter is now applied with `Dataset.filter()`, which
keeps the object a *Dataset* — so DuckDB still pushes its own column pruning
and predicates through it. A filtered scan costs about what an unfiltered one
does (sometimes less: there's less data to aggregate).

Doing this with a pre-built `Scanner` instead would freeze the column set and
cost ~3× — the measured difference between 126 ms and 38 ms on a 3 M-row
aggregate. The distinction matters.

Two things still take the exact, materializing path:

- **Hash masking**, which has no Arrow compute equivalent (sha256).
- **Any masking** loses column pruning, because computed columns need a
  Scanner. Row filtering alone does not.

Ontology objects benefit too: a viewer restricted by a row policy sees only
their permitted rows and still gets a paged read rather than a materialization.
*(The 195 ms figure this paragraph used to quote, for a page over a 1 M-row
dataset, was not re-measured on 2026-08-14 — the probe used there was 400 K
rows, where a policied page costs 79 ms against 47 ms unpolicied. Treat 195 ms
as an older reading of a different size, not a current one.)*

### The ontology — was the ceiling, now ~26× faster

Object queries used to materialize the entire backing dataset in Python on
every request. They now push filtering, search, counting and paging into
DuckDB over the Parquet parts, and merge the (small) edit overlay there:

| Objects | Browse a page (25) | Search | Get by primary key |
|---:|---:|---:|---:|
| 10 K | 22 ms *(was 52)* | 21 ms *(was 57)* | 16 ms |
| 100 K | 56 ms *(was 572)* | 58 ms *(was 590)* | 25 ms |
| 1 M | 317 ms *(was 6.7 s)* | 386 ms *(was 7.2 s)* | 107 ms |
| 5 M | 1.37 s *(was 36 s)* | 1.56 s *(was 38 s)* | 334 ms |

The get-by-primary-key column used to read 75 ms at 1 M and 279 ms at 5 M. It
is 107 ms and 334 ms. The pre-security tree measures 117 ms and 345 ms on the
same probe — *slower* than this tree — so those two cells had rotted before
this work as well, and the "was" column is the only part of the row that was
still accurate.

Point lookups get an extra win: a predicate on the primary key is pushed
*inside* the de-duplication window, so navigating to an object prunes row
groups instead of scanning — 334 ms at 5 M objects, against 36 s before.

Row-level security rides along: a row policy is pushed into the same scan
rather than materializing ahead of it. Measured at 400 K objects, a policied
page costs 79 ms against 47 ms unpolicied. Only hash masking still forces the
exact in-memory path. *(The ~195 ms this paragraph used to quote for 1 M
objects was not re-measured; see the note in the row-level security section.)*

#### And an actual index, for types that earn one

The scan above is fast but still linear. An object type can be *indexed* —
materialized into the metadata store, one row per object — after which queries
stop touching Parquet at all. It is opt-in per type (`POST
/ontology/object-types/{name}/index`), because it costs storage and a refresh:

| Objects | Browse a page (25) | Get by primary key | Search (selective) | Build the index |
|---:|---:|---:|---:|---:|
| 200 K | 24 ms | **1.3 ms** | 4.7 ms | 3.9 s |
| 800 K | 91 ms | **1.3 ms** | 13.5 ms † | 16.7 s |

**A key lookup is constant time — 1.3 ms whether the type holds 200 K objects
or 800 K.** That is the claim this index exists to make, it is the one the
README quotes, and it is the one cell here that re-measures exactly: the
previous 1.4 ms figure reproduces, and the pre-security tree gives 1.38/1.42 ms
against this tree's 1.32/1.30, so the security work did not touch it.

**The rest of this table did not reproduce, and the old numbers are gone rather
than corrected.** Browsing a page measured 24 ms and 91 ms against a published
4 ms and 27 ms; building the index took 3.9 s and 16.7 s against 2.4 s and
11 s. The pre-security tree measures the same as this one (1.00× across every
cell), so this is **not** a regression from the security work — but neither is
it a number anybody can currently defend. Two things are true at once and both
matter:

- `bench/benchmark.py` **does not measure the object index at all.** The
  original figures came from a script that was never committed, so there is no
  way to re-run what produced them, and no way to tell a rotted number from a
  differently-shaped probe. That is the actual defect here, and it is why these
  cells now carry what a committed harness measures instead of what an
  uncommitted one once did.
- The runs above were taken on a box under moderate load, and index build in
  particular is disk-bound. A quiet machine will beat 3.9 s / 16.7 s.

† The selective-search cell is **not comparable** to the 5.9 ms it replaces.
The probe used here (`SKU-00001` against `SKU-{i mod 5000}`) matches a *fixed
fraction* of the type, so the result set quadruples from 200 K to 800 K along
with the data — it is not the fixed-size result the word "selective" implies.
The growth in that column is the probe's, not necessarily the index's, and the
constant-time claim once made for it is **withdrawn pending a probe that holds
the match count fixed** rather than restated with a new number.

Search is *trigram*-indexed, not full-text, and that is deliberate: object
search means a case-insensitive **substring** match, a definition shared with
the DuckDB scan path. Trigram indexing makes `LIKE '%…%'` fast while matching
exactly what it always matched. Full-text search would have quietly redefined
it — token matching finds `minas` in "Minas Tirith" but never `inas Ti`.

It is best-effort on both dialects: SQLite uses an FTS5 trigram table,
PostgreSQL a `pg_trgm` GIN index. An old SQLite without FTS5, or a managed
Postgres that won't grant `CREATE EXTENSION`, falls back to an unindexed
`LIKE` — same answers, less speed.

What the index deliberately does *not* accelerate:

- **Filters on any other property.** Properties are stored as one JSON blob,
  and extracting one per row measured *slower* than the DuckDB scan the index
  was meant to beat. Those queries are handed straight back to the scan, which
  prunes row groups instead.
- **Counting every match of a broad search.** This is the one thing no index
  makes cheap — the index must visit every match to count it. So a search stops
  counting at 10,000 and reports `10,000+`; browsing is never capped and stays
  exact. *(The figures this bullet used to quote — 130 ms vs a 63 ms scan, and
  7.9× faster with the cap — came from the same uncommitted script as the index
  table above and were **not** reproduced in the 2026-08-14 re-measurement. The
  behaviour is real and tested; the numbers are withdrawn rather than
  restated.)*
- **Anything a row policy touches.** The index is shared across users, so a
  request carrying row-level security never reads it. This is the safety
  property, not an omission.

**A stale index is never used.** Freshness is checked on every query against
the backing dataset's version and a catch-up watermark over the edit log. If
either says the materialization is behind, it is bypassed and the scan answers.
Builds refresh the indexes of types they affect; a refresh that fails drops the
index rather than leaving a confident wrong answer in place.

#### An edit no longer throws the index away

That watermark used to be an edit *count*, which made it an invalidation flag:
any single hand edit changed the count, so the whole index for that object type
was discarded and every later read fell back to a scan **that also replayed the
entire edit log**. Read cost grew with total write history — which is what
"slow as an application database" actually meant.

An edit now upserts the materialization instead, in the same transaction that
appends it to the log. Measured by `bench/regression.py` on 100 K objects, as a
ratio to the zero-edit baseline so the runner's speed cancels out:

| Edits in the log | Read a page of 25 |
|---:|---:|
| 0 | 1.00× (baseline) |
| 100 | 1.01× |
| 1 000 | 0.97× |
| 10 000 | 0.96× |

The write side is measured the same way: **edit #1 000 costs 1.00×** what edit
#1 did, so nothing about a write grows with history either.

Flat, not merely faster. Both claims are guarded at a deliberately loose 3.0× —
this exists to catch a regression in complexity class (an accidental fallback to
the scan, a rebuild creeping back into the write path), not a 20% drift.

The safety property that makes it usable: a materialization that cannot prove
it has applied every committed edit **returns nothing**, and the read falls
through to the scan. Unreachable counts as behind. Slower, never wrong.

**A policied read of a type with pending edits now costs 1.18× what it did.**
That is the one service-layer regression the security work introduced, and it
is a deliberate purchase. An overlay row is merged *after* the policied scan,
so until this change nothing had held it to the same rules as the rows it
merges onto: a mask-exempt editor could set a masked column through an update
action and a non-exempt editor then read the plaintext back. Holding the
overlay to the same masks and row filter costs a policy resolution per read.
Measured on 100 K objects with 1 000 pending edits: 50.2 ms → 59.3 ms. With no
policy bound, or with no pending edits, the cost is zero to measurement
precision (0.98–1.00×) — the ontology page numbers above are unchanged.

**Writeback** (`POST /ontology/object-types/{name}/writeback`) folds the overlay
into a new dataset version so the log stops growing. Be clear about what it
bounds: **read cost, not disk**. Folded edits are marked, not deleted, because
`folded_into_version` is what makes a folded version reproducible. Pruning them
is a separate, explicit act — `prune_object_edits`, ADMIN-gated, with
`GET /ontology/object-types/{name}/edit-log` reporting what would be reclaimed
and why each retained edit stayed. See
[ARCHITECTURE.md](ARCHITECTURE.md#pruning-the-edit-log) for the five conditions
an edit must satisfy before it can go.

Sizing guidance now: **≤ 1 M objects per type is comfortable** on the scan
alone, 5 M is usable for lookups and tolerable for browsing, and an index makes
key lookups flat at any size in that range. Modeling *entities* in the ontology
and leaving high-volume *events* in datasets is still the right discipline —
it's just no longer a hard requirement at the low end.

### What the audience projection costs

Every model that becomes a response body now goes through a per-field
disclosure decision (`laurelin/core/serialize.py` — a field is readable only by
a principal who could have written it). That work did not exist before, it runs
on every serialized record, and **nothing in this document measured it**,
because every table above measures the service layer directly and
`bench/benchmark.py` never goes through the API. So it regressed unobserved.

`bench/serialize_cost.py` now measures it, and exists so this table cannot rot
the way the index table did. It reproduces the "Now" column below; it cannot
reproduce the first two, which require the pre-security tree checked out beside
this one.

Measured the same way as everything else here — the pre-security tree and this
tree benchmarked back to back, minima over seven interleaved repeats:

| Serialized response | Before | After the security work | Now |
|---|---:|---:|---:|
| 100 datasets, read by an admin | 0.33 ms | 1.63 ms *(4.98×)* | 0.96 ms *(2.93×)* |
| 100 datasets, read by a viewer | 0.32 ms | 0.92 ms *(2.89×)* | 0.39 ms *(1.23×)* |
| 1 000 datasets, read by an admin | 2.95 ms | 15.37 ms *(5.21×)* | 8.95 ms *(3.03×)* |
| 1 000 datasets, read by a viewer | 3.01 ms | 8.75 ms *(2.90×)* | 3.66 ms *(1.21×)* |
| 100 dashboards × 6 panels, admin | 0.95 ms | 10.73 ms *(11.34×)* | 4.63 ms *(4.89×)* |
| 100 dashboards × 6 panels, viewer | 1.03 ms | 8.03 ms *(7.81×)* | 3.32 ms *(3.22×)* |

The "Now" column is after two fixes, both of which only remove repeated work
and neither of which changes a disclosure decision:

- The per-field decision is **memoized on `(class, reader role, author role,
  narrowed)`**, which is everything it depends on. Deciding it per field per
  record was 68% of serialization time: the metadata scan behind
  `field_author_role` and the pydantic `model_fields` property were both being
  re-entered once per field per record. Actual pydantic serialization was 7%.
- `_holds_model`, which decides whether a value needs projecting or can be
  copied, gets an **exact-type** fast path for `str`/`int`/`float`/`bool`/`None`.
  After the first fix it was 41% of what remained.

**The residual is real and is not going away by tuning.** A viewer's response
costs ~1.2× what it did; an admin's costs 2.9–4.9×. Two honest reasons:

- **An admin pays the most, which is the opposite of intuition.** A viewer's
  projection *drops* most fields, so there is less left to walk. An admin
  receives every field and therefore pays the per-field check on all of them.
- **Roughly half the remaining `DatasetInfo` cost is not the projection at
  all.** `redacted_source` — the free-text credential redactor that runs on the
  admin's copy of a dataset source — went from 0.90 ms to 3.55 ms per 1 000
  calls (**3.96×**) in the same security work, measured on its own. It is
  reached only for `DatasetInfo`, only for an admin, and
  `serialize._apply_dataset_source_rule` documents that it is a courtesy and
  explicitly **not** a security boundary — which makes it the obvious next
  thing to cut, and it is not cut yet.

Put in proportion: 1 000 datasets in one response is a large page, and 9 ms of
serialization sits behind a 38 ms query. The projection is not the bottleneck
in any request measured here. It is, however, **5–10× more expensive than the
thing it replaced**, on a path taken by every response, and that is worth
knowing before it is put in front of a wider fan-out.

---

## Four roles, one governance layer

| Role | What runs it | Bytes live | For |
|---|---|---|---|
| **Embedded default** | DuckDB, in process | Laurelin | Medium data — the default, and the only role you need to start |
| **Federation** | DuckDB attach, and Flight SQL engines | Iceberg / Delta / S3 / Postgres, or a warehouse | Large but *selective* (pruning does the work), or huge and non-selective (the cluster reduces) |
| **Serving tier** | StarRocks, ClickHouse | That engine | Low-latency governed reads over a table the serving engine owns |
| **Operational store** | The metadata store (optionally StarRocks) | Laurelin's metadata database | The materialized state of ontology objects |

Same catalog, ACLs, markings and lineage across all four. For the first three
the role is a **per-dataset property, not a deployment mode** — one workspace
mixes managed, federated and served datasets freely. The fourth is not a
dataset kind at all: it is how the ontology materializes object state.

Two boundaries worth stating up front, because the word "tier" invites the
wrong reading. Laurelin **reads** a serving engine; it does not operate one and
it does not load one. And the operational store is pluggable in the sense that
there is a seam with a second implementation behind it — not in the sense that
an operator can switch stores today (see limitation 13).

## Federation: governing bytes Laurelin doesn't hold

The two sections below are the same role. Federated datasets keep the scan at
the source; delegated compute pushes the whole reduction to a cluster. Both
exist so Laurelin governs data it never holds.

### Genuinely big and non-selective: delegated compute

Laurelin runs no distributed engine, and won't. Organizations with data at
that scale already have one; what they lack is a governed semantic layer over
it. So a *remote transform* submits SQL to that engine and stores what comes
back:

```python
@remote_transform(
    output=Output("revenue_by_region"),
    engine="warehouse",
    query="SELECT region, sum(amount) AS total FROM events GROUP BY region",
)
def revenue_by_region(): ...
```

Trino (or Dremio, Databricks — anything speaking Flight SQL) aggregates five
billion rows across its own nodes; Laurelin receives five and stores them as an
ordinary managed dataset with lineage back to the engine, markings, ACLs, and
everything downstream working normally. `pip install 'laurelin[engines]'`;
transport is ADBC over Flight SQL, an open protocol rather than a vendor SDK.

**Guardrail:** delegation exists so the *cluster* reduces. A result above
`LAURELIN_ENGINE_MAX_ROWS` (5 M default) is refused — a query returning
millions of rows hasn't reduced anything, and pulling it defeats the purpose.

**Explicit non-goals**, because these are what make a distributed engine large
and a half-built version would be worse than the ones that exist: no shuffle,
no distributed joins, no cluster manager, no cross-node query planner.

**One v1 gap, stated rather than hidden:** ad-hoc workbench SQL can't be pushed
to a delegated engine, because Laurelin doesn't parse user SQL and so can't
rewrite `FROM events` into a remote query. Delegated compute is reached through
remote transforms; interactive querying happens against the managed result.

### When the data is large but selective: federated datasets

Importing a 5-billion-row event table that already lives in Iceberg would be
wasteful and pointless. Register it instead:

```bash
curl -X PUT localhost:8787/api/v1/datasets/events/federated \
  -H 'Content-Type: application/json' \
  -d '{"source": {"type": "iceberg", "path": "s3://warehouse/analytics/events"}}'
```

Laurelin now governs that table — catalog entry, ACLs, classification
markings, lineage, transform input — while the bytes stay put and the scan
happens at the source. Sources: `iceberg`, `delta`, `parquet` (path or glob,
local or object storage) and `postgres`.

**Policy still applies.** Row policies and column masks are compiled to SQL
and wrapped around the remote scan, from the *same decision* that drives the
Arrow path — so a policy means the same thing whether it filters a local
Parquet file or a remote Iceberg table. A policy that cannot be compiled
refuses the query rather than running unfiltered.

**The intended workflow is to reduce at the boundary:**

```
iceberg: 5B rows  ──federated──►  transform (aggregate)  ──►  managed 2M-row rollup
                                                                     │
                                                       ontology · dashboards · actions
```

The big table never moves; what lands in Laurelin is medium-sized, which is
where the ontology and dashboards are fast. Lineage and markings span the
boundary.

Three deliberate limits:

- **Ontology object types cannot bind to a federated dataset.** Every object
  page would become a full remote scan. Materialize with a transform and bind
  to that — the error says so explicitly.
- **Federated datasets are hidden from the ad-hoc SQL workbench by default**
  (`LAURELIN_FEDERATION_WORKBENCH=1` to expose them). Enabling federation
  should not silently widen what every viewer can reach. Transforms can always
  use them.
- **No versioning.** A federated table has no immutable snapshots, because
  Laurelin doesn't control its writes.

## The serving tier: StarRocks and ClickHouse

The third role. A serving engine owns a table and answers low-latency queries
over it; Laurelin registers that table as a dataset, compiles the row policy
and column masks into the engine's own SQL, and pushes them down. The catalog
entry, ACLs, markings and lineage are the same ones every other dataset gets.

**StarRocks is the flagship.** Querying Iceberg is a first-class path there, so
"open at rest" survives the serving tier instead of being traded away for it;
it joins natively, and an ontology link *is* a join; and it has primary-key
tables with real upserts, which is what an operational store needs. ClickHouse
is a fully supported peer that shipped in the same slice — and it is the engine
that proved the dialect seam bends.

**Where the boundary is.** Laurelin *reads* a serving engine. It does not
operate one, and it does not load one: both kinds are read-only, with no
`INSERT`, no ingest and no upload/append. The single write path anywhere near a
serving engine is the ontology object store's Stream Load, which is the
unverified one — see limitation 13.

**There is no performance number here.** No benchmark in `bench/` covers either
engine, so there is no measured latency, no throughput figure and no comparison
against DuckDB. Every number elsewhere on this page was produced by
`bench/benchmark.py` on the managed DuckDB path and stays attached to it.

### StarRocks (flagship)

```bash
pip install 'laurelin[starrocks]'
curl -X PUT localhost:8787/api/v1/datasets/events/starrocks \
  -H 'Content-Type: application/json' \
  -d '{"source": {"type": "table",
                  "url": "starrocks://reader:pw@fe-host:9030/analytics",
                  "table": "analytics.events"}}'
```

`kind="starrocks"` reads over the MySQL wire protocol
(`mysql-connector-python`). It is the first engine that is a **server** rather
than a library, and the change of shape matters more than the change of syntax:

- **`StarRocksDialect.literal()` raises, by construction.** Stacked statements
  execute — `SELECT 1; INSERT INTO t VALUES (99)` on one `execute()` runs the
  INSERT — so a policy value reaching SQL as text would be a remote *write*,
  not a wrong read. No escaper ships even unused.
- **Every query goes through a prepared cursor**, and StarRocks refuses to let
  the prepared protocol express an INSERT at all (error 1295). Defence in
  depth. Point it at an account holding `SELECT` and nothing else.
- **Bool is not a portable row key here.** `CAST(b AS STRING)` is `'1'` where
  Arrow says `'true'` — the one type that is portable on both other engines is
  not portable on this one. Portable row keys: String, integer, Date, Decimal.
- **Complex column types are refused by name at registration** —
  `ARRAY`/`MAP`/`STRUCT`/`JSON`/`BITMAP`/`HLL`/`VARBINARY`. A guessed Arrow
  type is a guessed text form, and a row policy is a comparison of text.
- **Budgets ride in a `/*+ SET_VAR(...) */` hint** (there is no trailing
  `SETTINGS` clause), with `query_timeout` rounded **up** to a whole second
  because StarRocks rejects a fractional one.

**What is not verified, stated plainly:**

- The read path was measured against a StarRocks container **locally**. The
  opt-in CI job that runs those suites has **never executed on GitHub
  Actions** — every documented behaviour is written from that container.
- Reading through a StarRocks **Iceberg external catalog is untested**. The
  three-part `catalog.db.table` scan expression works, but the type-agreement
  tables were measured on *native* StarRocks columns, and the Iceberg→StarRocks
  mapping could move DECIMAL scale or DATETIME precision.
- The StarRocks **object store** (the fourth role, not this one) has **only
  ever run against an in-memory double — never a real StarRocks server** — and
  is not selectable by configuration. Keep the two statuses separate: the read
  path met a real server, the object store did not.

### ClickHouse (supported peer)

```bash
pip install 'laurelin[clickhouse]'
curl -X PUT localhost:8787/api/v1/datasets/events/clickhouse \
  -H 'Content-Type: application/json' \
  -d '{"source": {"type": "parquet", "path": "/data/events/*.parquet"}}'
```

`kind="clickhouse"` reads through **chdb** — ClickHouse embedded in the
Laurelin process — so there is no ClickHouse service to run and no cluster to
size. ClickHouse came first because it stresses the seam hardest, and it stayed
because it is a serving engine a lot of teams already run. The same row
policies and column masks apply, compiled to ClickHouse SQL instead of DuckDB
SQL, and `tests/test_clickhouse_governance.py` asserts row-for-row equality
with the Arrow reference across the policy space.

What it does **not** do, and will not pretend to:

- **Read-only from Laurelin:** no `INSERT`, no MergeTree ingest, no
  upload/append — those refuse. The engine serves; Laurelin governs the read.
- **Embedded only.** Connecting to a real ClickHouse server is not
  implemented; `clickhouse-connect` is deliberately not a dependency. "Fully
  supported peer" is about the governance path, not about server mode.
- **No versions, no time travel, no ontology object types** (same as
  federated, and the same as StarRocks).
- **No filesystem sandbox** — the same as the federated path for a local file
  (DuckDB's `disabled_filesystems` applies only to non-local sources), so
  ClickHouse datasets are admin-registered and sit behind the same
  `LAURELIN_FEDERATION_WORKBENCH` gate. Neither serving-engine path is
  sandboxed; this is parity with federation, not a step down from it.
- **Row policies and hash masks only work on types the engine spells the way
  Arrow does.** Both compare the column's *text*, and the engines' stringifiers
  differ: `decimal(12,2)` `0.00` is `'0.00'` to Arrow and DuckDB but `'0'` to
  ClickHouse, and `1e10` is `'1e+10'` to Arrow and `'10000000000'` to
  ClickHouse. Portable set: String, Bool, integer and Date on both engines,
  plus Decimal on DuckDB (and Float64 for DuckDB hash masks). **Anything else
  is refused**, because the alternative is a policy that admits a different set
  of rows depending on which engine ran it. Mask modes `null` and `redact`
  need no text rendering and work on every column of every type.

## Sizing guidance

| You have | Laurelin today |
|---|---|
| < 1 M rows/dataset, < 1 M objects/type, a team | Comfortable. This is the sweet spot. |
| 1–50 M rows/dataset, entities modeled separately | Works well. Sync incrementally (`mode: append`), use `streaming=True` for row-wise transforms, SQL transforms for aggregation. |
| A large table with a small daily delta | Fine — appends cost the delta, not the dataset. Compact periodically. |
| > 100 M rows, or > 5 M objects/type | Not in the embedded role. Federate or delegate it, or serve it from StarRocks/ClickHouse and govern the read; then use Laurelin over the reduced result. |
| Hostile multi-tenancy | Use `--lock-pipelines` and separate workspaces — or wait for stronger isolation. |
| Sub-second streaming freshness | Wrong tool. |

**Concurrency and resource limits.** Each query occupies a worker for its
duration and DuckDB uses multiple cores per query, so one replica comfortably
serves a team doing interactive analysis — not hundreds of concurrent heavy
scans. Three controls keep one query from degrading the replica for everyone:

| Control | Default | Behaviour at the limit |
|---|---|---|
| Memory per query | 2 GB | DuckDB raises rather than being OOM-killed → **400**, "narrow the query" |
| Wall clock | 60 s | A watchdog interrupts the query → **504** |
| Concurrent queries | 8 per replica | Refused fast with `Retry-After` → **503** |

Builds get a separate, looser budget (4 GB, no timeout) — a build that runs for
minutes is fine; a dashboard panel that does is broken. Federated scans carry
the interactive budget, since they can be expensive on someone else's
infrastructure too.

The audit log is bounded by `LAURELIN_AUDIT_MAX_EVENTS` (unlimited by default),
trimmed after each build.

**Horizontal scaling.** With a PostgreSQL control plane, each workspace's
metadata lives in its own schema in that database, so every replica shares one
consistent store — this replaced the per-workspace SQLite file that made
multi-replica unsafe. Set `LAURELIN_DATA_URI` to an object store and dataset
Parquet leaves the volume too, at which point nothing is node-local and
replicas are interchangeable. Builds are leased, so exactly one replica
executes each. See [DEPLOYMENT.md](DEPLOYMENT.md).

Embedded mode (a SQLite control plane) is still **one replica** — correct for
a laptop or a single VM, and unchanged.

## Known limitations, plainly

1. **Search ranking is positional, not linguistic.** Hits are ordered by
   where the term appears in the object's title, with body-only matches
   after. There is no stemming, no synonyms, and no phrase or boolean
   operators — substring matching already covers prefixes (`order` finds
   `orders`), which is most of what stemming would buy on identifier-heavy
   data, but it is still the wrong primitive for prose.
2. **The object index covers paging, search and key lookups only.** Filters
   on other properties fall back to the DuckDB scan — deliberately, because
   the JSON-per-row alternative measured slower. Filterable secondary columns
   would need a schema-per-type index.
3. **A search total saturates at 10,000.** Counting every match of a broad
   term is the one thing an index can't make cheap. Browsing is exact.
4. **Column masking loses column pruning**, and hash masking materializes
   (no Arrow sha256). Row policies push down fully and are free.
5. **Whole-table Python transforms are still RAM-bound** — that's the default
   for convenience. `streaming=True` removes the bound for row-wise work; SQL
   transforms stream natively. Only whole-table aggregation in Python is
   genuinely limited.
6. **Builds don't spread across replicas.** Each replica runs its own worker
   pool and leases prevent double-execution, but there is no queue that hands
   a backlog on one replica to an idle one.
7. **Incremental transforms need an append-only input.** The delta path
   triggers when a new version's manifest extends the previous one; a rewrite
   or a compaction falls back to a full recompute, which is correct but not
   cheap.
8. **Edits are an overlay** — object writes don't flow back into Parquet
   unless you fold them (`/writeback`) or write a transform that does it.
   Writes to one object type serialize on a lock on that type's state row.
   That is what makes a concurrent edit correct rather than a lost update, and
   it is a real throughput ceiling: object edits are hand edits, and this is
   sized for hand edits. A fold refuses outright if the dataset moved
   underneath it, so a fold contending with a build is a retry, not a merge.
9. **Horizontal scaling needs Postgres** — embedded (SQLite) mode is
   single-replica by construction. Object storage is opt-in via
   `LAURELIN_DATA_URI`; without it, replicas still share a volume for Parquet.
10. **Auto-compaction is off by default** — set `LAURELIN_AUTO_COMPACT_PARTS`
   to a part count, or keep calling `/compact` yourself. There's no
   size-aware or tiered policy, just a threshold.
11. **Iceberg merges are fast-forward only**, and there are no tags, no
   hidden partitioning and no row-level deletes. Branches and additive schema
   evolution do work. Compaction works too, as of this release — but it is a
   **whole-table rewrite** into one new snapshot, not Iceberg's incremental
   `rewrite_data_files`, so it costs the whole table and reclaims *scan cost,
   not disk*: earlier snapshots keep their data files and nothing expires them
   yet. (Before this release it did not work at all, and did not say so — it
   wrote a Parquet part nothing read and pinned no snapshot. See the CHANGELOG.)
12. **The serving tier is read-only from Laurelin's side.** No writes into
   StarRocks or ClickHouse, no versions, no time travel, and no ontology
   object types on any source-scanned kind. ClickHouse is embedded (chdb)
   only — talking to a real ClickHouse server is not implemented. Neither
   path has a filesystem sandbox for local sources, which is why both are
   admin-registered and sit behind `LAURELIN_FEDERATION_WORKBENCH`.
13. **The StarRocks object store has only run against an in-memory double**,
   and is not selectable by configuration. `OntologyService` constructs
   `MetadataObjectStore`; no env var, route or config key swaps it. So the
   metadata store is what every deployment actually uses, and "pluggable"
   today means there is a seam with an implementation behind it — not a
   switch an operator can throw. Strict per-row position ordering is also not
   implemented there (StarRocks resolves duplicate keys by load order and the
   DDL declares no sequence column). What stands in for it: having no shared
   transaction to lock, the store loads an edit only when its watermark is
   exactly one position behind it, and otherwise stays behind and lets
   `catch_up` replay in log order. That is sound, and it is *slower* than the
   metadata store under concurrency by design — every interleaved writer costs
   a skipped load and a later replay.
14. **StarRocks has no CI coverage, and its Iceberg external catalog is
   untested.** The opt-in job that runs the StarRocks suites has never
   executed on GitHub Actions; the documented behaviour comes from a local
   container. Reading through a StarRocks Iceberg external catalog is
   untested — the three-part `catalog.db.table` scan expression works, but
   the type-agreement tables were measured on native columns and the
   Iceberg→StarRocks mapping could move DECIMAL scale or DATETIME precision.

Every one of these is a roadmap item, and none of them is hidden in a footnote
because you'd rather find out now than in month three.

**Recently fixed:** write amplification (appends are now O(delta), not
O(dataset)); the ontology full-scan ceiling (~26× faster — 36 s to 1.37 s at
5 M objects — and flat for key lookups once indexed, at 1.3 ms from 200 K to
800 K; the matching claim for *selective search* is withdrawn until a probe
exists that holds the match count fixed, see the index table);
the row-level security tax (3.6× → 0.9×); the
single-replica limit (Postgres schemas + object storage + build leases); the
absence of query resource limits (a runaway query is now interrupted rather
than left to degrade a replica); the lack of cron and on-upstream triggers
(leased, so firing is exactly-once across replicas); manual-only
compaction; the object index being thrown away on every edit (an edit now
upserts the materialization); and the SQL renderer being DuckDB's SQL with a
seam drawn around it — there is now a real dialect seam with ClickHouse and
StarRocks behind it. Three pre-existing **fail-open** bugs surfaced while
building that seam and are fixed; see the Security section of
[CHANGELOG.md](../CHANGELOG.md) for exactly what they were and why nobody is
exposed.
