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

### Row-level security — a real, measurable tax

| Rows | Aggregate, no policy | Aggregate, RLS active | Cost |
|---:|---:|---:|---:|
| 10 K | 12 ms | 14 ms | 1.2× |
| 100 K | 18 ms | 22 ms | 1.3× |
| 1 M | 40 ms | 101 ms | 2.5× |
| 5 M | 71 ms | 256 ms | 3.6× |

When a dataset has an active row policy or column mask for the caller,
Laurelin reads the table and filters it through the policy engine *before*
DuckDB sees it — so that path loses pushdown and pays for materialization.
Un-policied datasets keep the fast lazy scan.

This is a deliberate trade: one enforcement choke point that every read path
shares, at the cost of speed on policied datasets. **Budget ~3–4× on
policy-protected datasets at multi-million-row scale.** Pushing predicates
into the scan for simple row policies is a known optimization we haven't done.

### The ontology — this is the ceiling

| Objects | Browse a page (25) | Search |
|---:|---:|---:|
| 10 K | 52 ms | 57 ms |
| 100 K | 572 ms | 590 ms |
| 1 M | 6.7 s | 7.2 s |
| 5 M | 36 s | 38 s |

**Linear, and it will not surprise you pleasantly.** Object queries
materialize the whole backing dataset and apply the edit overlay on every
request. There is no object index.

Read that table as a boundary:

- **≤ 100 K objects per type** — fine. Sub-second.
- **~1 M** — usable for occasional lookups, too slow to browse.
- **≥ 5 M** — don't. Query the backing dataset with SQL instead.

Ontology indexing is the single highest-value performance item on the roadmap.
Until it lands: model your *entities* in the ontology (customers, aircraft,
cases — usually thousands to hundreds of thousands) and leave your *events* in
datasets (orders, flights, log lines — usually millions), querying them with
SQL. That's a reasonable modeling discipline anyway, but right now it's also a
performance requirement, and you should know that before you build on it.

---

## Sizing guidance

| You have | Laurelin today |
|---|---|
| < 1 M rows/dataset, < 100 K objects/type, a team | Comfortable. This is the sweet spot. |
| 1–50 M rows/dataset, entities modeled separately | Works well. Sync incrementally (`mode: append`); keep Python transforms' inputs in RAM-sized chunks; expect the RLS tax. |
| A large table with a small daily delta | Fine — appends cost the delta, not the dataset. Compact periodically. |
| > 100 M rows, or > 1 M objects/type | Not yet. Query it in a warehouse; use Laurelin over aggregates. |
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

1. **No object index** — ontology reads are full scans (numbers above). The
   top-priority fix.
2. **RLS loses pushdown** — 3–4× on policied datasets at scale.
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
