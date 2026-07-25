# What Laurelin is, what it isn't, and how far it goes

Most data platforms describe their scale in adjectives. This page uses numbers,
including the ones that aren't flattering. If you are evaluating Laurelin for
real work, the fastest way to a good decision is to know where it stops.

Reproduce everything here with:

```bash
python bench/benchmark.py --rows 10000,100000,1000000,5000000
```

---

## What this is

- **A single-node, medium-data platform** with governance and an ontology
  layer that OSS mostly doesn't have. Its job is to make a team's data
  modeled, governed, and operable — not to be a warehouse.
- **Open at every layer.** Parquet data, YAML ontology, Python pipelines,
  SQLite/Postgres metadata, a documented REST API. Every artifact is readable
  without Laurelin running, and there is no proprietary format anywhere in the
  stack.
- **Governance-complete for its size class.** Local/OIDC/SAML SSO, SCIM,
  RBAC + per-dataset ACLs, row-level security, column masking, and mandatory
  classification markings that propagate through lineage.

## What this isn't

- **Not a distributed compute engine.** Compute is DuckDB in one process.
  There is no cluster, no shuffle, no Spark. If your working set is
  hundreds of GB, use a warehouse and point Laurelin at it.
- **Not a streaming platform.** Ingestion is batch pulls and file drops. No
  CDC, no Kafka, no sub-second freshness.
- **Not a multi-tenant SaaS with hostile tenants.** Pipelines are Python the
  server executes; an editor who can write a pipeline has code execution.
  `--lock-pipelines` exists for exactly this, but the honest posture is:
  editors are trusted colleagues. See [SECURITY.md](../SECURITY.md).
- **Not battle-tested.** It is early. It has ~281 tests and a coherent design;
  it does not have years of production hours behind it.

---

## Measured performance

One 24-core Linux box, Python 3.14, DuckDB 1.5.4, PyArrow 24, local NVMe.
An 8-column `orders` table (ints, floats, strings, a text note). Best of 3
runs. **Your hardware will differ — the shape of the curve is the point, not
the absolute milliseconds.**

### The query path — strong, and sub-linear

| Rows | Parquet | Aggregate<br>(GROUP BY) | Filter + top-N | Point lookup | Row page<br>(UI grid) |
|---:|---:|---:|---:|---:|---:|
| 10 K | 0.2 MB | 12 ms | 12 ms | 11 ms | 12 ms |
| 100 K | 1.4 MB | 18 ms | 16 ms | 18 ms | 14 ms |
| 1 M | 13 MB | 40 ms | 32 ms | 49 ms | 15 ms |
| 5 M | 73 MB | 71 ms | 74 ms | 44 ms | 15 ms |

500× the rows costs ~6× the time on an aggregate, and the UI's row page is
flat. That's projection and filter pushdown into the Parquet scan doing its
job: memory tracks the *result*, not the dataset.

**Verdict: the SQL workbench and dashboards are comfortable into the tens of
millions of rows.** This is the part of Laurelin that scales the way you'd
hope.

### Ingest and builds — fine

| Rows | Ingest (write a version) | Two-stage build (Python filter → SQL rollup) |
|---:|---:|---:|
| 10 K | 6 ms | 29 ms |
| 100 K | 22 ms | 60 ms |
| 1 M | 133 ms | 182 ms |
| 5 M | 695 ms | 737 ms |

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
| 100 K | 33 ms | 3.0 ms | 11× | 1.5 MB → 27 KB |
| 1 M | 233 ms | 5.0 ms | 47× | 13.4 MB → 186 KB |
| 5 M | 926 ms | 12.8 ms | 72× | 77.6 MB → 787 KB |

**The advantage widens as the dataset grows**, because the rewrite is linear
in total size and the append is linear in the delta. That's the difference
between a nightly sync that gets slower every month and one that doesn't.

Connector syncs use it: set `mode: "append"` with a `cursor_column` and each
sync pulls only rows above the last high-water mark, then appends them. A
sync with nothing new is a no-op and mints no version.

The trade-off is part accumulation — many small files slow scans — so
`POST /datasets/{name}/compact` merges them back into one file when you want
to pay that cost deliberately.

**The caveat:** a `@transform` receives its input as one in-memory
`pyarrow.Table`. Peak memory during a Python transform is roughly the
uncompressed size of its inputs plus its output — for the table above, about
100 MB per million rows. SQL transforms stream through DuckDB and don't pay
this. If a Python transform is going to hold 50 GB, it won't.

### Row-level security — now essentially free

| Rows | Aggregate, no policy | Aggregate, RLS active | Cost |
|---:|---:|---:|---:|
| 1 M | 39 ms | 33 ms | **1.0×** *(was 2.5×)* |
| 5 M | 72 ms | 71 ms | **1.0×** *(was 3.6×)* |

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

Ontology objects benefit too: a viewer restricted by a row policy now gets an
object page over a 1 M-row dataset in **195 ms**, against ~6.7 s before, while
seeing only their permitted rows.

### The ontology — was the ceiling, now ~26× faster

Object queries used to materialize the entire backing dataset in Python on
every request. They now push filtering, search, counting and paging into
DuckDB over the Parquet parts, and merge the (small) edit overlay there:

| Objects | Browse a page (25) | Search | Get by primary key |
|---:|---:|---:|---:|
| 10 K | 19 ms *(was 52)* | 20 ms *(was 57)* | 19 ms |
| 100 K | 54 ms *(was 572)* | 57 ms *(was 590)* | 25 ms |
| 1 M | 300 ms *(was 6.7 s)* | 349 ms *(was 7.2 s)* | 75 ms |
| 5 M | 1.4 s *(was 36 s)* | 1.5 s *(was 38 s)* | 279 ms |

Point lookups get an extra win: a predicate on the primary key is pushed
*inside* the de-duplication window, so navigating to an object prunes row
groups instead of scanning — 279 ms at 5 M objects, against 36 s before.

Two honest caveats:

1. **It's still a scan, not an index.** Browsing and search remain linear in
   dataset size; the constant is just ~26× smaller. A real object index
   (sorted keys, zone maps) would make these sub-linear and is still on the
   roadmap.
2. **Row-level security rides along.** A row policy is pushed into the same
   scan, so a policied viewer gets a page over 1 M objects in ~195 ms. Only
   hash masking still forces the exact in-memory path.

Sizing guidance now: **≤ 1 M objects per type is comfortable**, 5 M is usable
for lookups and tolerable for browsing. Modeling *entities* in the ontology
and leaving high-volume *events* in datasets is still the right discipline —
it's just no longer a hard requirement at the low end.

---

## Sizing guidance

| You have | Laurelin today |
|---|---|
| < 1 M rows/dataset, < 1 M objects/type, a team | Comfortable. This is the sweet spot. |
| 1–50 M rows/dataset, entities modeled separately | Works well. Sync incrementally (`mode: append`); keep Python transforms' inputs in RAM-sized chunks; expect the RLS tax. |
| A large table with a small daily delta | Fine — appends cost the delta, not the dataset. Compact periodically. |
| > 100 M rows, or > 5 M objects/type | Not yet. Query it in a warehouse; use Laurelin over aggregates. |
| Hostile multi-tenancy | Use `--lock-pipelines` and separate workspaces — or wait for stronger isolation. |
| Sub-second streaming freshness | Wrong tool. |

**Concurrency.** The API is stateless and scales horizontally behind a load
balancer (see [DEPLOYMENT.md](DEPLOYMENT.md)); a Postgres control plane makes
the identity tier HA. But each query occupies a worker for its duration, and
DuckDB uses multiple cores per query — so a single replica comfortably serves
a team doing interactive analysis, not hundreds of concurrent heavy scans.
Per-workspace data still lives on a shared volume, which is the one piece of
state that needs `ReadWriteMany` above one replica.

## Known limitations, plainly

1. **Still no object index** — ontology browse/search is now pushed into
   DuckDB (~26× faster) but remains linear in dataset size. Point lookups do
   prune.
2. **Column masking loses column pruning**, and hash masking materializes
   (no Arrow sha256). Row policies push down fully and are free.
3. **Python transforms are in-memory** — input size bounded by RAM. SQL
   transforms stream and don't pay this.
4. **Builds are in-process** — a worker pool per replica, not a distributed
   queue; no cron/event triggers or incremental transforms yet.
5. **No query resource limits** — DuckDB runs without a `memory_limit` or
   statement timeout, and there's no admission control. One deliberately
   expensive query can degrade a replica for everyone. Treat the SQL
   workbench as available to trusted users.
6. **The audit log grows without bound** — no rotation or partitioning. It's
   small per event, but plan for it on a long-lived busy workspace.
7. **Edits are an overlay** — object writes don't flow back into Parquet
   unless you write a transform that does it.
8. **Single data plane** — no object-storage-backed workspaces yet, so the
   data volume is shared state and multi-replica needs `ReadWriteMany`.
9. **Compaction is manual** — appends accumulate parts until you call
   `/compact`; there's no automatic policy yet.

Every one of these is a roadmap item, and none of them is hidden in a footnote
because you'd rather find out now than in month three.

**Recently fixed:** write amplification — every write used to cost O(dataset),
so a 1% daily delta rewrote the whole thing. Appends are now O(delta); see the
incremental-writes numbers above.
