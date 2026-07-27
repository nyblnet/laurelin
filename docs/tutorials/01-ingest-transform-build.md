# 1 · Ingest → transform → build

**Goal:** get a CSV into Laurelin, clean it with a Python transform, aggregate
it with SQL, and see the lineage that connects them.

**You'll learn:** workspaces, dataset versions, the two kinds of transform, and
what a build actually does.

---

## Create a workspace

A workspace is a directory. Everything — data, metadata, pipelines, ontology —
lives inside it.

```bash
laurelin init orders-workspace --name "Orders"
cd orders-workspace
ls
# data  laurelin.yml  ontology  pipelines
```

(`metadata.db` — the SQLite file holding versions, builds, lineage, and audit —
appears as soon as you write something.)

## Get some data in

Save this as `orders.csv` (or use your own CSV — adjust the column names as you
go):

```csv file=orders.csv
order_id,customer,region,status,amount
1001,Aule Foundry,us-east,shipped,240.50
1002,Yavanna Seeds,eu-central,open,88.00
1003,Aule Foundry,us-east,returned,240.50
1004,Nienna Textiles,apac,shipped,412.75
1005,Yavanna Seeds,eu-central,shipped,150.25
1006,Ulmo Shipping,us-west,,97.00
```

Upload it as a dataset:

```bash
laurelin upload raw_orders orders.csv --workspace .
# Uploaded orders.csv -> raw_orders v1 (6 rows)
```

(Or drag the file onto **Datasets** in the web UI, which shows you the inferred
schema before it creates anything. The CLI is used here so the tutorial stays
copy-pasteable.)

Look at what you got:

```bash
laurelin datasets show raw_orders --workspace .
```

Two things to notice:

- **`v1`.** Every write creates a new immutable version. Uploading again makes
  `v2`; `v1` is still there, still readable. Nothing is ever overwritten in
  place.
- **The files are just Parquet.** A version is a *manifest* of one or more
  Parquet parts under `data/<dataset>/parts/`:

```bash
ls data/raw_orders/parts/
```

  Those open in pandas, DuckDB, Spark, or anything else. If you walk away from
  Laurelin tomorrow, your data is not trapped.

## Write a transform

Transforms are plain Python files in `pipelines/`. Save this as
`pipelines/orders.py`:

```python file=pipelines/orders.py
"""Clean raw orders, then aggregate them by region."""

import pyarrow as pa
import pyarrow.compute as pc

from laurelin.transforms import Input, Output, sql_transform, transform


@transform(output=Output("clean_orders", description="Valid, non-returned orders"),
           orders=Input("raw_orders"))
def clean_orders(orders: pa.Table) -> pa.Table:
    # Drop rows with no status, and returns (they aren't revenue).
    has_status = pc.and_(pc.is_valid(orders["status"]),
                         pc.not_equal(orders["status"], ""))
    not_returned = pc.not_equal(orders["status"], "returned")
    return orders.filter(pc.and_(has_status, not_returned))


@sql_transform(
    output=Output("revenue_by_region", description="Revenue rollup per region"),
    inputs={"o": Input("clean_orders")},
    query="""
        SELECT region,
               count(*)    AS orders,
               sum(amount) AS revenue
        FROM o
        GROUP BY region
        ORDER BY revenue DESC
    """,
)
def revenue_by_region(): ...
```

Two flavors, same DAG:

- **`@transform`** receives each input as a `pyarrow.Table` and returns one.
  Use it when you want real Python — branching, libraries, custom logic.
- **`@sql_transform`** runs DuckDB SQL, with each input bound to the alias you
  name in `inputs`. The function body is ignored; it exists so the decorator
  has something to attach to.

For a Python transform over a dataset too big to hold in memory, add
`streaming=True`: the function then receives an *iterator* of Arrow batches
and yields batches, so memory tracks one batch rather than the whole dataset.

```python
@transform(output=Output("clean_orders"), streaming=True, orders=Input("raw_orders"))
def clean_orders(orders):
    for batch in orders:
        yield batch.filter(pc.not_equal(batch["status"], "returned"))
```

It takes exactly one input, and anything needing all the rows at once (a
`GROUP BY`, a total) belongs in a SQL transform — DuckDB streams and spills
those for you.

You never declare the DAG. Laurelin reads it from the `Input`/`Output`
declarations — `revenue_by_region` depends on `clean_orders` because it reads
the dataset that `clean_orders` writes.

## Build

```bash
laurelin build --workspace .
```

```text
Build 2d8896e8ef48: succeeded
TRANSFORM          OUTPUT             STATUS     ROWS  VERSION
-----------------  -----------------  ---------  ----  -------
clean_orders       clean_orders       succeeded  4     1
revenue_by_region  revenue_by_region  succeeded  3     1
```

Laurelin planned the order (topological — `clean_orders` had to run first),
executed both, wrote a new version of each output, and recorded lineage. Build
a single target with `laurelin build revenue_by_region --workspace .`; it will
still build `clean_orders` first if it needs to.

Check the result:

```bash
laurelin datasets show revenue_by_region --workspace .
```

```text
region      orders  revenue
----------  ------  -------
apac        1       412.75
us-east     1       240.5
eu-central  2       238.25
```

Four regions went in, three came out, and `us-east` shows 1 order rather than
2: order 1003 was a return, and order 1006 (the only `us-west` row) had an
empty status. Both were filtered by `clean_orders` — which is exactly the kind
of quiet, load-bearing assumption you want written down in a transform instead
of living in someone's notebook.

## See it in the UI

```bash no-run
laurelin serve --workspace . --no-auth
```

Open <http://127.0.0.1:8787>.

> `--no-auth` makes every request an admin. It's for local development only —
> tutorial 3 turns authentication on.

Worth a click:

- **Datasets** — versions, schema, and a row preview per dataset.
- **Pipeline** — the lineage graph (`raw_orders → clean_orders →
  revenue_by_region`) and build history. Hit **Run build** and watch it go
  from `running` to `succeeded` — builds run on a worker pool, so the request
  returns immediately.
- **SQL** — a workbench over every dataset. Try:

  ```sql
  SELECT region, revenue FROM revenue_by_region ORDER BY revenue DESC
  ```

  Switch the result to a **bar** chart, then **Add to dashboard** to keep it.

## Bring in real data

Uploading files is fine for a tutorial; for a real source, configure a
connector. In the UI: **Datasets → Data sources → Add source**, or via the API:

```bash
curl -X PUT localhost:8787/api/v1/sources/orders_pull \
  -H 'Content-Type: application/json' \
  -d '{"type": "postgres", "dataset": "raw_orders",
       "config": {"url": "postgresql://user:pw@db:5432/shop",
                  "table": "public.orders"}}'
```

Registering a source doesn't connect to anything. Point the `url` at a database
you can actually reach, then pull:

```bash no-run
curl -X POST localhost:8787/api/v1/sources/orders_pull/sync
```

Sources also come in `http` (fetch a CSV/Parquet export) and `file` (a path or
glob on the server, for data landed on a mounted volume). A sync writes an
ordinary new dataset version, so everything downstream — this pipeline, its
lineage, and the access controls in tutorial 3 — applies with no changes.

Credentials are stored server-side and redacted in every API response.

### Sync only what's new

A recurring full refresh gets expensive: re-pulling and re-writing the whole
table every night is wasteful when 1% of it changed. Add `mode: "append"` and
a `cursor_column`:

```bash
curl -X PUT localhost:8787/api/v1/sources/orders_pull \
  -H 'Content-Type: application/json' \
  -d '{"type": "postgres", "dataset": "raw_orders",
       "config": {"url": "postgresql://user:pw@db:5432/shop",
                  "table": "public.orders",
                  "mode": "append", "cursor_column": "order_id"}}'
```

Now each sync pulls only rows above the highest `order_id` it has already
seen, and appends them — writing one small Parquet part instead of rewriting
the dataset. A sync that finds nothing new is a no-op and doesn't mint a
version.

The saving is proportional to how much bigger your dataset is than your daily
delta. On a 5 M-row table with a 1% delta, appending is **~70× faster and
writes ~1% of the bytes** of a full rewrite ([SCALE.md](../SCALE.md)).

Appends accumulate parts, and many small files eventually slow scans. Merge
them when convenient:

```bash
curl -X POST localhost:8787/api/v1/datasets/raw_orders/compact
```

Uploads take the same flag — `POST /datasets/{name}/upload?mode=append` adds a
file's rows instead of replacing the dataset.

## Make it run without you

So far everything happens when you ask. A schedule binds a trigger to an
action, so the pipeline keeps itself current:

```bash
# every night at 02:00
curl -X PUT localhost:8787/api/v1/schedules/nightly \
  -H 'Content-Type: application/json' \
  -d '{"trigger": "cron", "cron": "0 2 * * *", "action": "build"}'

# …or whenever the source data actually lands
curl -X PUT localhost:8787/api/v1/schedules/on_new_orders \
  -H 'Content-Type: application/json' \
  -d '{"trigger": "upstream", "upstream_dataset": "raw_orders", "action": "build"}'
```

The `upstream` trigger is usually the better one: the pipeline follows its
inputs instead of guessing when they arrive. Pair it with a `sync` action on a
connector and the whole chain — pull, transform, publish — runs itself.

`POST /schedules/{name}/run` fires one immediately without waiting for its
window. A schedule that fails is recorded and *still* reschedules, so one bad
night doesn't silently disable the pipeline; and an overdue schedule fires
once rather than once per missed window.

---

**What you built:** an immutable, versioned dataset; a two-stage pipeline
mixing Python and SQL; and a lineage graph Laurelin derived rather than one you
maintained.

**Next:** [Model an ontology and act on it →](02-ontology-and-actions.md)
