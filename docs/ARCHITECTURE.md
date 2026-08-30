# Laurelin architecture

Laurelin is a local-first, ontology-driven data platform. One **workspace
directory** holds all state: Parquet data, a SQLite metadata store, Python
pipelines, YAML ontology. Modules communicate through the models in
`laurelin/core/models.py` — that file is the source of truth for all types.

```
laurelin/
├── core/        models.py, config.py (Workspace), db.py (MetadataStore),
│                permissions.py, audience.py + serialize.py (field audiences),
│                failure.py (structured failures), redaction.py, fileperms.py,
│                dialects.py + clickhouse.py + starrocks.py + federation.py,
│                limits.py, auth.py / oidc.py / saml.py, scheduler.py, iceberg.py
├── catalog/     versioned Parquet dataset storage + DuckDB access
├── transforms/  @transform / @sql_transform, DAG builder, lineage, expectations
├── ontology/    YAML loader, object queries, links, actions, edit overlay,
│                store.py (the pluggable object materialization)
├── connectors/  postgres / http / file source pulls
├── export/      workspace export, import and the governance fingerprint
├── mcp/         MCP server + the REST client it drives
├── api/         FastAPI app: REST + static UI mount
├── ui/          React + TS app (webapp/), built to one self-contained
│                static/index.html — no CDN
├── demo.py      generates the aviation demo workspace
└── cli.py       typer CLI (`laurelin` entry point = laurelin.cli:main)
```

## Module contracts

### `laurelin/catalog` — `DatasetCatalog`

```python
class DatasetCatalog:
    def __init__(self, workspace: Workspace, store: MetadataStore): ...
    def create_dataset(self, name: str, description: str = "") -> DatasetInfo
    def write(self, name: str, table: pa.Table, source: str = "upload",
              build_id: str | None = None, description: str = "") -> DatasetVersionInfo
    def append(self, name, table, ...) -> DatasetVersionInfo
    def append_batches(self, name, chunks, ...) -> DatasetVersionInfo
    def compact(self, name, ...) -> DatasetVersionInfo
    def read(self, name: str, version: int | None = None) -> pa.Table
    def version_files(self, name, version=None) -> list[str]   # absolute part paths
    def parquet_glob(self, name: str, version: int | None = None) -> str
        # SQL list literal of the version's parts, for duckdb read_parquet(...)
    def rows(self, name: str, limit: int = 100, offset: int = 0,
             version: int | None = None) -> list[dict]      # via duckdb, JSON-safe values
    def upload_file(self, name, path, description="", mode="replace") -> DatasetVersionInfo
        # CSV (duckdb read_csv_auto) or Parquet by extension; mode="append" adds rows
```

Rules:
- Dataset names: `^[a-z][a-z0-9_]*$`, validate on create/write. Raise `ValueError` otherwise.
- `write()` writes each part to a unique key under `data/<name>/parts/` and
  *then* inserts the manifest row — that insert is the commit, so a crash
  leaves an unreferenced part (garbage) rather than a registered-but-missing
  version. This replaced an `os.rename` of a version directory, which had no
  equivalent on object storage. Auto-creates the dataset row if missing.
- Versions are immutable; `read` with no version = latest. Missing dataset/version
  raises `KeyError` (api layer maps to 404).
- **A version is a manifest of Parquet parts** (`DatasetVersionInfo.files`,
  workspace-relative). `write` produces one part; `append`/`append_batches`
  write *only the delta* as a new part and carry forward the previous
  version's parts by reference, so appending costs O(delta) rather than
  O(dataset) — ~70× faster than a rewrite on a 5 M-row dataset with a 1%
  delta. Version dirs are immutable and never deleted, so inherited
  references stay valid. An empty `files` list means the pre-manifest layout
  (glob the version dir) and is still read correctly.
- `compact()` merges the latest version's parts back into one file; appends
  are cheap but accumulate parts, and many small files slow scans. An Iceberg
  dataset dispatches to the Iceberg path below — the layout being compacted is
  the table's, not a directory of Laurelin parts.
- `rows()` must convert non-JSON-safe values (timestamps, bytes, Decimal, NaN) to strings/None.
- Schema captured as `ColumnSchema(name, type=str(arrow_type))`.

#### Iceberg-backed datasets

A dataset's `kind` is `managed` (Laurelin owns versioned Parquet parts),
`federated` (the bytes live elsewhere; no versions), `iceberg` — **owned
and versioned like managed, read at source like federated** — `clickhouse`
(read-only, scanned in place by embedded ClickHouse) or `starrocks`
(read-only, scanned by a StarRocks server). Both of the last two are below.

The mapping from kind to dialect is **total and has no default**
(`models._SQL_DIALECTS`), and so is the mapping from kind to reader
(`DatasetCatalog._SOURCE_READERS`). A kind missing from either raises, because
the alternative — falling back to DuckDB — puts the *newest*, least-checked
engine behind the quoter and statement shape that were never tested against
it, and `source_table`'s dialect-mismatch guard cannot catch that: both sides
would say "duckdb".

That split is the whole design. `DatasetInfo.scans_at_source` is what the read
paths branch on, because what matters to a reader is not who owns the table
but where the scan happens. So an Iceberg table is read by the same code that
reads a foreign one, through `iceberg_scan(?)`, which means `PolicyPlan.to_sql`
already covers it — row filters and column masks reach Iceberg with no second
implementation of what a policy means. Writes are the only new code.

Each write is an Iceberg snapshot *and* a `dataset_versions` row carrying its
`snapshot_id`, so a Laurelin version number and an Iceberg snapshot name the
same point in history and `read(name, version=N)` time-travels.

The catalog is the database Laurelin already runs: pyiceberg's `SqlCatalog`
takes `LAURELIN_DATABASE_URL` (or a SQLite file beside the metadata), so
adopting Iceberg adds no service to operate and a Postgres control plane
becomes a catalog shared across replicas. `LAURELIN_ICEBERG_WAREHOUSE` points
the data at object storage.

One asymmetry worth knowing: the SQL path prunes inside `iceberg_scan`, but
the Arrow path (`scan_for`, used by the ontology) materializes through
pyiceberg and applies the policy exactly. Correct, not lazy.

#### ClickHouse-backed datasets (`kind="clickhouse"`)

*Role: the serving tier — a governed, read-only path over a table the serving
engine owns. StarRocks (below) is the flagship of that role; ClickHouse is a
fully supported peer, and the engine that proved the seam bends.*

A second SQL dialect, added to prove that "one decision, N renderers" survives
an engine whose rules genuinely differ from DuckDB's. It reads a Parquet path
through **chdb** — ClickHouse embedded in this process — so there is no
ClickHouse service to run.

`DatasetInfo` gained `sql_dialect` alongside `scans_at_source`, because until
now those were the same fact wearing one name ("read via the source
expression" also implied "DuckDB renders the SQL"). They diverge here, and in
a governance layer a derivation that drifts is a leak rather than a wrong
number.

`laurelin/core/dialects.py` owns the divergences. Each was measured against
chdb 4.2.1, and each is a silent leak if you assume SQL is SQL:

| | DuckDB | ClickHouse |
|---|---|---|
| identifier quoting | `"a""b"` | `` `a\`b` `` — backslash is an escape, so DuckDB's rule resolves a column to a *different* column and returns its data, no error |
| policy values | bound parameters (`?`) | escaped literals: there is no positional placeholder, and the named `{p:String}` channel is **not byte-preserving** (`a\nb` arrives 3 bytes, not 4) |
| null mask | `NULLIF(c, c)` | `if(0, c, NULL)` — `NULLIF` leaves NaN unmasked, since NaN ≠ NaN |
| hash mask | `substr(sha256(CAST(c AS VARCHAR)), 1, 16)` | `substring(lower(hex(SHA256(toString(c)))), 1, 16)` |
| statement shape | flat | filter **nested strictly below** the projection: ClickHouse resolves `WHERE` against `SELECT` aliases, so a flat statement evaluates the row policy against the *mask* — a full row-policy bypass that fails open |
| portable types | String, Bool, integer, Date, **Decimal**; plus Float64 for hash masks | String, Bool, integer, Date only |

That last row is the one that is not about spelling. A row policy is
`<column as text> IN (<allowlist>)` and a hash mask is `sha256(<column as
text>)`, so both mean whatever the engine's stringifier means — and there are
four stringifiers, no two the same:

| | function |
|---|---|
| Arrow, row key | `pc.cast(col, string)` |
| Arrow, digest input | `str(value)` |
| DuckDB | `CAST(c AS VARCHAR)` |
| ClickHouse | `toString(c)` |

On Decimal, Float64 above 1e10 and every temporal type they disagree, and the
disagreement changes *which rows a user sees*: measured on a `decimal(12,2)`
tenant key with the policy value `'1.1'`, `/datasets/{name}/rows` returned
nothing and `/query` returned another tenant's rows. So each dialect declares
the Arrow types it renders identically to the reference
(`SqlDialect.row_key_matches_arrow` / `hash_text_matches_arrow`) and
`SqlPolicy.render` **refuses** everything else, rather than enforcing a
different policy here than the row API enforces. Masks `null` and `redact`
need no rendering and stay available on every column of every type, which is
the escape hatch the error message names.

`tests/test_text_agreement.py` re-derives both tables from the live engines
every run, in both directions — a dialect that over-claims leaks, and one that
under-claims denies service for nothing.

`tests/test_dialects.py` pins DuckDB's output byte-for-byte against literals
copied from before the seam existed, so adding an engine cannot quietly change
the first one. `tests/test_clickhouse_governance.py` asserts the property that
matters — every (policy, user) returns the same rows and values as
`apply_table_policy` — plus each divergence above as its own named threat.

**What this is not**, stated because the name implies all of it:

- **No writes.** `catalog.write`/`append`/`upload_file` refuse any
  source-scanned dataset outright. Laurelin does not write to a serving engine:
  the engine serves, and Laurelin governs the read.
- **No server mode.** chdb only. `clickhouse-connect`, TLS, credential storage
  and a settings-profile threat analysis are not in this slice.
- **No versions or time travel** (`latest_version` stays `None`), and no
  ontology object types (refused, as for federated).
- **No filesystem sandbox.** `file('/etc/passwd', LineAsString)` succeeds under
  chdb, and `readonly=1` rejects the whole query rather than restricting the
  filesystem. Registering a source is therefore "may read any file the server
  process can read", which is why it is admin-only and behind the same opt-in
  workbench gate as federated. This is **parity with the federated path, not a
  step down from it**: `federation.connect` sets `disabled_filesystems` only
  when `is_local_source(source)` is false, and a local Parquet path — the one
  source type ClickHouse supports — is a local source, so DuckDB reads it
  unrestricted too. The primary control is the same on both: only
  server-generated SQL reaches the engine, because callers get an Arrow table
  and never a connection.

**What a text-defined policy still cannot promise.** Where an engine agrees
with the Arrow reference, a hash token is the same token everywhere and joins
across engines. Where it does not, the read is refused rather than served with
a token that silently only matches itself — so the guarantee is "portable or
refused", never "quietly different". Bool is the one type where *both* SQL
engines disagree with the digest input (`str(True)` is `'True'`; both engines
say `'true'`), which predates ClickHouse; hash masks on Bool columns are
refused on both, and `redact` is the answer.

**Branches** are named pointers into the snapshot history, so cutting one
copies nothing. Merging fast-forwards main and records a Laurelin version —
without that row the merge would be invisible to lineage, builds and time
travel, which all speak in versions. Only fast-forward: merging two diverged
histories needs a row-level conflict policy, and inventing one would silently
pick a winner between two people's writes.

**Schema evolution** is additive by default, because Iceberg tracks columns by
id and old snapshots stay readable. Dropping or renaming needs
`allow_breaking=True`, and the refusal lists the transitive downstream
datasets from lineage.

**Compaction** rewrites the whole table into one new snapshot and records it as
a version pinned to that snapshot. It goes through `write_iceberg`, not the
managed path: `write()` is exempted from the scanned-at-source refusal for
Iceberg, so compacting an Iceberg dataset used to write a local Parquet part
nothing reads and register a version with `snapshot_id = NULL` — which reads as
"the table as it is now" forever, so time travel to a compacted version
returned the rows there were *later*. It reclaims scan cost, not disk: earlier
snapshots keep their data files, which is what keeps history readable.

Not implemented: tags, hidden partitioning, row-level deletes, incremental
`rewrite_data_files` (compaction reads the whole table), expiring old snapshots
— so nothing here ever frees storage.

#### StarRocks-backed datasets (`kind="starrocks"`)

*Role: the serving tier, and its flagship. Querying Iceberg is a first-class
path in StarRocks, so "open at rest" survives the serving tier instead of being
traded away for it; it joins natively, and an ontology link* is *a join; and it
has primary-key tables with real upserts, which is what an operational store
needs. Read-only from Laurelin's side, like every source-scanned kind.*

A third dialect, and the first one that is a **server** rather than a library.
`laurelin/core/starrocks.py` reads a StarRocks table over the MySQL wire
protocol (`mysql-connector-python`, the `starrocks` extra) with the row/column
policy compiled to StarRocks SQL and pushed down.

The change of shape matters more than the change of syntax. chdb is embedded,
holds no credentials and cannot be written to. StarRocks has an account, and
two things were measured that decide the design:

- **Stacked statements execute.** `SELECT 1; INSERT INTO t VALUES (99)` on one
  `execute()` runs the INSERT, and asking the client for
  `-ClientFlag.MULTI_STATEMENTS` reports the flag off while the INSERT still
  lands. Feeding the policy value `us') OR 1=1; INSERT … --` through naive
  concatenation returned every row *and* wrote one.
- **Parameters bind faithfully.** `length(?)` equalled `len(value.encode())`
  for all 334 hostile values fuzzed — NUL bytes, newlines, lone backslashes,
  300 control characters.

So `StarRocksDialect.literal()` **raises**, and no escaper ships even unused:
on ClickHouse an escaping defect leaks a read, and here it would be a remote
write. Defence in depth on top: Laurelin runs everything through a *prepared*
cursor, and StarRocks rejects INSERT in the prepared protocol outright (error
1295), so the library's own execution channel cannot express a write at all.
Point it at an account holding `SELECT` and nothing else.

| | DuckDB | ClickHouse | StarRocks |
|---|---|---|---|
| identifier quoting | `"a""b"` | `` `a\`b` `` | `` `a` `` — a backtick is **unspellable**: doubling *drops* it, so the quoter refuses rather than addressing the wrong column. DuckDB's `"s"` is a string *literal* here, which fails **open** |
| policy values | bound (`?`) | escaped literals | bound (`?`); `literal()` raises |
| null mask | `NULLIF(c, c)` | `if(0, c, NULL)` | `if(FALSE, c, NULL)` |
| hash mask | `substr(sha256(CAST(c AS VARCHAR)), 1, 16)` | `substring(lower(hex(SHA256(toString(c)))), 1, 16)` | `substr(sha2(CAST(c AS STRING), 256), 1, 16)` — `sha2` already returns hex, so porting ClickHouse's mandatory `lower(hex(…))` yields 128 characters and every token stops joining |
| statement shape | flat | nested | nested, and the derived table **must be aliased** (error 1248) |
| portable row keys | String, Bool, integer, Date, Decimal | String, Bool, integer, Date | String, integer, Date, Decimal — **not Bool**: `CAST(b AS STRING)` is `'1'` where Arrow says `'true'`, so the one type that is portable on both other engines is not portable here |

Column discovery uses `DESC`, not the result-set metadata and not
`information_schema` — both were measured to report a `BOOLEAN` as a
`TINYINT`, which would make it look like an integer and hand it back its
row-key portability. `information_schema` also reports a 128-bit `LARGEINT` as
`bigint(20) unsigned`.

Budgets ride in a `/*+ SET_VAR(query_timeout=…, query_mem_limit=…) */` hint
(there is no trailing `SETTINGS` clause). `query_timeout` must be integral —
`7.5` is error 1232 — so a fractional budget is rounded up rather than dropped.

**What this is not.** No writes, no Stream Load, no primary-key upserts, no
object store, no versions, no ontology object types (refused as for every
source-scanned kind). Scalar columns only: `ARRAY`/`MAP`/`STRUCT`/`JSON`/
`BITMAP`/`HLL`/`VARBINARY` are refused **by name** at registration, because a
guessed Arrow type is a guessed text form and a row policy is a comparison of
text.

**Not verified, and load-bearing enough to say so.** Reading a table through a
StarRocks **Iceberg external catalog** is untested: the three-part
`catalog.db.table` scan expression works, but the type-agreement tables were
measured on *native* StarRocks columns, and the Iceberg→StarRocks mapping
could move DECIMAL scale or DATETIME precision. The opt-in CI job that runs
these suites has also never executed on GitHub Actions — it is written from the
local container's behaviour.

#### Dashboard panels

A panel draws from exactly one of two sources, enforced by a model validator:
`sql` (raw SQL over datasets) or `object_type` + `metrics` (an aggregation
over ontology objects).

The second exists because the first is wrong for anything the ontology models.
SQL reads the *backing dataset*, which does not include the edit overlay — so
after an action, a SQL panel and the object list beside it disagree, and
nothing in the chart says so. Measured on a four-order workspace: after
marking one order shipped, the SQL panel still reported 3 open / 1 shipped
while the object panel reported 2 / 2.

Both reduce to `{columns, rows}` in the client, so the chart never learns which
source fed it. Panels still execute with the *viewer's* credentials, so an
object panel is filtered per viewer exactly as a SQL one is — but the execution
moved: `POST /dashboards/{name}/panels/{id}/run` runs the **stored** panel on
the server **as the caller** rather than handing the client a query to POST.
The privilege story is unchanged (same ACL, same row-level security, same
masking, per caller); what changed is that the viewer no longer has to be given
the query in order to see its result.

### `laurelin/transforms`

`laurelin/transforms/__init__.py` re-exports: `transform, sql_transform, Input, Output, TransformRegistry, Builder, collect_transforms`.

```python
@dataclass
class Input:  dataset: str
@dataclass
class Output: dataset: str; description: str = ""

@dataclass
class TransformSpec:
    name: str                    # function name, or a flow's name
    output: Output
    inputs: dict[str, Input]     # param name -> Input
    kind: str                    # "python" | "sql" | "remote" | "flow"
    fn: Callable | None          # python transforms: fn(**{param: pa.Table}) -> pa.Table
    query: str | None            # sql transforms: SELECT over input aliases as table names
    params: list                 # sql transforms: values bound to `query`'s `?`
    flow: FlowDef | None         # flow transforms: the declarative IR (see below)
    expectations: list[Expectation]   # assertions the output must satisfy

def transform(output: Output, **inputs: Input)   # decorator for python transforms
def sql_transform(output: Output, inputs: dict[str, Input], query: str,
                  params: list | None = None)    # decorator (fn body ignored)
def expect(*expectations: Expectation)           # applied ABOVE @transform

class TransformRegistry:
    def register(self, spec) / get(self, name) / all(self) -> list[TransformSpec]
    def by_output(self, dataset: str) -> TransformSpec | None

def collect_transforms(pipelines_dir: Path) -> TransformRegistry
    # exec each *.py in pipelines_dir (sorted); decorators register into the
    # *active* registry via a module-level context (use a contextvar or module
    # global set/reset around exec). Files import `from laurelin.transforms import ...`.
    # THEN load each *.flow.json into the SAME registry as kind="flow". A
    # .flow.json is never exec'd — that is the whole point of it.

class Builder:
    def __init__(self, workspace, catalog, store, registry): ...
    def plan(self, targets: list[str] | None = None) -> list[TransformSpec]
        # None = all outputs. Topo order: include each target's transform plus any
        # upstream transforms whose outputs are inputs (recursively). Raise
        # ValueError on cycles or unknown targets.
    def build(self, targets: list[str] | None = None) -> BuildInfo
        # Synchronous: plan (raises before creating the record), create, execute.
    def execute(self, build_id: str, targets: list[str] | None = None) -> BuildInfo
        # Executes an already-created record — the async path. The API validates
        # the plan, creates the pending build, returns it, and submits
        # execute() to app.state.build_executor (a ThreadPoolExecutor,
        # LAURELIN_BUILD_WORKERS, default 2). POST /builds {wait:true} keeps the
        # blocking behavior for scripts/tests.
```

`execute()` behavior: mark running with `started_at`; for each
spec in topo order: read inputs (python: `catalog.read`; sql: duckdb with each
alias registered as a view over `parquet_glob`), execute, `catalog.write(...,
source="transform", build_id=...)`, `store.upsert_build_task`,
`store.replace_lineage_for_transform(spec.name, edges)`. Inputs with no
registered producing transform must already exist as datasets (else the task
fails with a clear error). A failed task marks the build failed but later
independent tasks may still run; final status failed if any task failed.
Audit-log build start/finish.

#### Data expectations

`@expect(not_null(c), unique(c), row_count(min=, max=), accepted_values(c, vs),
expression(name, predicate))`, each with `severity="error"` (default, fails the
build) or `"warn"` (recorded, build continues).

Every check is SQL counting *offending* rows — zero means it holds — which
makes the failure message a count rather than a boolean, and lets every check
share one code path.

The important part is **when** they run. `catalog.write/append/write_batches`
take a `validate` callback that `_commit_version` invokes after the Parquet
parts are written and before the manifest row is inserted. Since that row
insert *is* the publication, an expectation that raises means the failing
version never existed for any reader: nothing downstream consumes it, and the
caller deletes the orphaned parts. Checking after the commit would instead mean
deciding what to do about data people can already see.

The checks run against `storage.dataset(files)` — a lazy pyarrow dataset
registered into DuckDB — so validating a streaming transform's output doesn't
materialize what streaming just avoided materializing.

Results are stored per build task (`build_tasks.expectations_json`), passes
included, and rendered on the Builds page.

#### Flows: transforms without code

`laurelin/transforms/flow_{ir,compile,files,governance}.py`, and
`laurelin/ui/webapp/src/views/Flows.tsx` — for this feature the screen is the
product, and the Python below it is plumbing for it.

A **flow** is a declarative pipeline stored as `pipelines/<name>.flow.json`: a
small DAG of steps (`source`, `filter`, `select`, `rename`, `derive`, `cast`,
`join`, `aggregate`, `dedupe`, `sort`) over a closed expression IR of exactly
three node types — `col`, `lit`, `op` — with every enum drawn from a fixed
vocabulary. There is no node that carries free text destined for SQL, at any
nesting depth.

**A file in `pipelines/`, not a row in `metadata.db`,** and that is a deliberate
trade. It inherits workspace export, workspace import, credential scanning and
the imported-pipelines acknowledgement gate by extending one suffix tuple at
three call sites; a table would have to re-derive all four, and omitting one
silently regresses coverage. It is also git-diffable.

**It compiles onto the existing build path, not beside it.** `collect_transforms`
puts flows into the same `TransformRegistry` as `pipelines/*.py`, so duplicate
name / duplicate output detection, planning, cycle detection, lineage, marking
propagation, expectations-before-publish, build leases, scheduler targets,
`GET /transforms` and `GET /lineage` all cover flows without
knowing they exist. `Builder._execute_flow` compiles and then hands the result
to the *same* DuckDB executor `kind="sql"` uses. Flow *authoring* is locked by
its own flag, `--lock-flows`, not by `--lock-pipelines`: the Python lock closes
code execution, which a flow — bound, schema-checked SQL over a closed IR —
cannot reach. Ejecting to Python writes a `.py` and refuses if either flag is
set.

Two tiers of validation, split by cost:

* **Tier A — structural, schema-free.** Enum membership, literal type coercion,
  arity, node-id syntax, DAG shape, invented-identifier syntax. Runs on every
  registry collection, which happens on every API request, so it must not touch
  a remote system. This tier alone determines `spec.inputs`, and therefore
  lineage and markings — derived from the IR's `source` nodes, never by scanning
  generated text.
* **Tier B — schema binding.** Every referenced column and dataset checked for
  membership in the *live* schema, plus a coarse type check. Runs at
  `PUT /flows/{name}`, at `POST /flows/preview`, and inside `Builder`
  immediately before execution. A schema that drifts after authoring therefore
  fails the build, loudly, rather than being caught only at edit time.

##### The compiler's contract

> The compiled SQL text is a pure function of the flow's structure — node kinds,
> enum choices, and identifiers resolved against a live schema — and contains
> not one byte derived from any author-supplied value.

Asserted directly: two flows differing only in their literal values compile to
byte-identical SQL, with different parameter lists. Every position is one of
three things and there is no fourth:

* a **bound value** (`?`) — filter and formula literals, `IN` elements, `LIKE`
  patterns, `date_trunc` units, expectation values, the preview `LIMIT`;
* a **closed enum** — cast targets, aggregate functions, join types, sort
  directions and null placement, all keywords this module already contains
  (measured: `CAST(x AS ?)` is a parser error and `ORDER BY ?` is refused, so
  these genuinely cannot be bound);
* an **identifier** — checked for membership in the live schema and only then
  quoted. Membership, never a regex: `a"b` and `total (USD)` are legal Parquet
  column names, and a regex would both reject those and admit names the data
  does not have.

`flow_compile.py` contains **no function that converts a value to SQL text** —
no `literal()`, no escaper, no quote-doubling on a value. That is a stronger
property than "our escaper is correct", and it is why `StarRocksDialect.
literal()` raising is not a hazard here: there is nothing to call it from.

Identifier *collisions* are folded with `permissions.confusable_identifier` —
NFKC, strip, casefold — the same function the policy resolver uses. This is
load-bearing rather than tidy: DuckDB resolves identifiers case-insensitively
and binds the first match, so a name the compiler thought was new could be a
name the engine thought it already had, and a mask on `pay` was laundered by a
derived column called `PAY`. One fold, shared, so the two cannot drift.

##### Governance (`flow_governance.py`)

Stricter than the Python transform path, deliberately — the point of the feature
is to make *every analyst* an editor, and the Python path launders ACLs, row
policies and column masks (documented, tested-as-stated, unchanged). A flow:

* may only read datasets its recorded **author** can view, re-checked at build
  time against that author, because a scheduled build has no request user;
* is refused if any source carries a **row policy** — there is no policy algebra
  that survives an aggregate, and inventing one silently is worse than refusing;
* is refused if any source carries a **column mask** on a column the flow reads
  or emits, matched under the same fold as above;
* may only replace an **output dataset** its author can view *and* edit, which is
  also what stops a flow being re-pointed at a public source to declassify what
  it used to produce;
* produces an output granted to its **author alone** when any source is
  restricted — never the union of the inputs' grants, which would hand each
  input's readers the other's data.

`POST /flows/{name}/eject` runs every one of those checks and applies the author
restriction itself, because after ejecting there is no flow left for the Builder
to apply it for.

##### Preview

`POST /flows/preview` runs the draft IR through `_execute_sql` — the one SQL
execution path — so it inherits ACL-as-unknown-table, row-level security, column
masks, the sandbox pragma and admission control with no new policy code. Three
consequences the UI states permanently rather than leaving to be discovered:
preview is policied and the build is not (fail-safe direction, but they can
differ); a masked column previews as `'***'`, so arithmetic over one is nonsense
in the preview and correct in the build; and preview reads the latest version
now while the build reads whatever is latest then.

The preview `LIMIT` is appended **only at the previewed terminal node**, never
pushed into an upstream CTE: an aggregate over a limited input is not the
build's aggregate, and a preview that quietly answers a different question is
worse than a slow one. It asks for one row more than it shows, so "this is all
of it" and "this is the first page" are distinguishable.

#### Quick chart: point-and-click data-to-chart

`laurelin/ui/webapp/src/views/analyses/QuickChart.tsx` (the Analyses landing
page's zero-commitment entry — formerly the standalone Explore screen;
`/explore` redirects here param-for-param), the shared shaping layer
`views/shaping/model.ts` + `views/shaping/ShapingCards.tsx`, one route
(`POST /explore/preview`), and two `DashboardPanel` fields (`flow`, `top`).
This is the Contour/Quiver half of the product: an analyst who cannot write SQL
picks a dataset or an object type, shapes it by clicking — filter, group by
(with date buckets and numeric bins), summarise, order, top-N — watches the
chart update live, and saves it to a dashboard.

**The quick chart has no representation of its own.** There is no ExploreSpec
type on the server, on the wire, or in storage. The screen's state is
synthesized into a linear `FlowDef` (`exploreFlow` in `shaping/model.ts`:
source → [filter] → [cast]\* → [derive]\* → aggregate → [sort]) and that
FlowDef is both the preview's wire format and the saved panel's stored format.
Everything below the synthesis seam is the Flow stack byte for byte: Tier-A
validation, the one compiler with every value bound, `_execute_sql`. A date
bucket over a *text* column — the single most common shape real data arrives
in — synthesizes the compiler's own `cast` step to timestamp first; a numeric
bin is `mul(floor(div(col, w)), w)` as a derive. No new IR either way.

`POST /explore/preview` is `/flows/preview` minus exactly one call:
`check_flow_governance` is **not** run, because its row-policy and
referenced-mask refusals guard *materialization* ("a flow's result is a new
dataset without that policy") and the quick chart materializes nothing — every result
is computed by `_execute_sql` under the caller's own ACL, row policy and
masks, and a saved flow panel re-compiles and re-executes per **viewer**, so
two viewers get different rows from the same panel. That divergence is pinned
by `test_explore_preview_allows_a_row_policied_source_because_nothing_is_materialized`
so it cannot later be "discovered" as a bug. Everything else is inherited:
sources are view-checked before the compiler can name a column, 200-row cap
with an honest `truncated` flag, first-party refusal sentences, laundered
engine errors, the shared admission slots.

A saved panel's `flow` and `top` are OPERATIONAL like `sql`: a viewer's `GET`
carries only the presentation half, and the chart arrives via `/run`. Saving
a panel validates the bindings (`x`/`y`/`series`) against the compiled schema,
which is what catches drift after a dataset changes. The object-type path
saves today's object panel verbatim and involves no SQL synthesis at all; its
aggregate response names `masked_properties` so the pickers can grey out what
the caller's masks cover instead of offering a property whose only group is
`"***"`.

**Charts are hand-rolled SVG** (`src/charts.tsx` — the zero-CDN single-file
bundle forbids a chart library, permanently) with one governing rule: a mark
is a claim about a measured value. NULL renders as a gap and is counted in a
note, never coerced to a zero the tooltip then asserts; numeric bins sit on a
numeric axis so empty bins are visible width; scatter axes fit the data
instead of forcing zero; sub-0.01 domains get tick labels derived from the
tick step rather than a fixed two decimals; a nonzero KPI never rounds to
"0"; pies fold their tail into "other" past 12 slices.

**What the quick chart deliberately cannot do:** no heatmap, dual axes, KPI
deltas, boxplots, or maps (tiles fight the zero-CDN constraint — a separate
future decision); no percentiles beyond median; no viewer-facing shaping
(viewers consume saved panels); no pushdown of quick-chart/flow SQL to
StarRocks or ClickHouse; and a flow whose shape the Flow builder authored
beyond the linear grammar reopens with "cannot edit here" rather than being
silently flattened. Two measures of very different scales share one axis and get a
visible warning, not a second axis.

#### Analyses: the multi-cell governed notebook

`laurelin/ui/webapp/src/views/Analyses.tsx`, `views/analyses/model.ts`, the
`analyses` table, `AnalysisInfo`/`AnalysisCell` in `core/models.py`, and the
`/api/v1/analyses` routes. This is Foundry's Code Workbook minus the code: a
saveable, shareable document of cells, where each cell is EITHER a governed
SQL query (the SQL page, inline) OR a shaping step (the shared card stack the
quick chart uses, from `views/shaping/`),
each renders a result table and an optional chart, and a later shaping cell
may take an earlier shaping cell's output as its source.

**Chaining is one statement, one policy pass.** Nothing is materialized per
cell — per run or preview the server synthesizes ONE `FlowDef` from the
target cell's ancestor closure (`_analysis_closure`: step ids namespaced
`{cell_id}_{step}`, a `cell:<id>` input rewritten to the upstream cell's
terminal), compiles it through the one Flow compiler with every value bound,
and executes it through the one `_execute_sql` path **as the caller**. The
whole chain — every upstream cell included — is therefore computed under the
current caller's ACL, row policy and masks in a single pass, and no
intermediate result ever exists outside that statement; a cell structurally
cannot show a viewer rows only the author's policy would have allowed. No
result is ever persisted: a cached result would be a silent RLS bypass, so
two viewers get different rows from the same stored chain. SQL cells are
non-chainable in both directions, because the IR is closed to raw SQL and
both bridging mechanisms measurably fail (DuckDB refuses parameters in
views; textual composition of raw and compiled SQL misaligns ordinals).

R2 splits a cell exactly as it splits a dashboard panel: a viewer receives
`{id, title, chart, x, y, series, stacked, width}` and rows from the run
route; `sql`/`flow`/`inputs`/`top` are withheld, and run failures go through
`stored_instruction_error` so the error path is not an oracle for the fields
the read path withholds. Writes follow the `_preserve_operational` contract
for **every** field, both halves: on a whole-record PUT (and the per-cell
PUT), a field the request does not mention inherits the stored value —
absence is "unchanged", never "blank it" — so a reorder sent as
`[{"id":"c2"},{"id":"c1"}]` keeps titles and chart bindings as well as the
instructions. Editor-facing refusals from the compiler are rewritten
server-side into the vocabulary the product speaks (`_cell_vocabulary`:
"Cell 2 ('Revenue by region')'s Summarise card refers to…", never
"Step 'c2_a1'"), so scripts and MCP agents hear the same sentences the
bundled UI shows.

**What an analysis deliberately cannot do: run code.** There is no
arbitrary-Python (or any code-execution) cell, and this is the decision that
makes the feature safe to give every analyst: a code cell is Foundry's actual
"Code" workbook, i.e. the RCE surface `--lock-pipelines` exists to close, and
it would launder ACLs, row policies and column masks exactly as Python
transforms do. Everything an analysis runs compiles to governed SQL through
paths that were already built and attacked. If a code cell is ever proposed,
it must gate on the same lock model as Python transforms and its output must
pass full materialization governance — nothing in the analyses IR, routes or
models may be loosened to accommodate it. Also out of scope, recorded as
notes rather than half-built: materializing a cell's output as a dataset
(would need `check_flow_governance`'s output rules or it is a mask-laundering
hole), exporting a cell to a dashboard panel (a shaping cell's closure *is* a
`DashboardPanel.flow`, so a later "save as panel" is a copy), scheduling,
per-analysis ownership, and cross-analysis references.

### `laurelin/ontology`

`laurelin/ontology/__init__.py` re-exports `load_ontology, OntologyService`.

```python
def load_ontology(ontology_dir: Path) -> OntologyDef
    # merge all *.yml/*.yaml (sorted); keys: object_types, link_types, actions.
    # LinkTypeDef uses YAML keys `from:` / `to:` (model aliases handle this).
    # Duplicate api_names -> ValueError.

class OntologyService:
    def __init__(self, workspace, catalog, store, ontology: OntologyDef): ...
    def list_object_types(self) -> list[ObjectTypeDef]
    def query(self, type_name: str, search: str | None = None,
              filters: dict[str, str] | None = None,
              limit: int = 100, offset: int = 0) -> dict
        # {"objects": [...], "total": int, "total_capped": bool}
        # total saturates at SEARCH_TOTAL_CAP when `search` is set; browsing is exact
    def get(self, type_name: str, pk: str) -> dict | None
    def linked(self, type_name: str, pk: str, link_name: str) -> list[dict]
        # follows link in either direction (link where from==type or to==type)
    def apply_action(self, action_name: str, pk: str | None,
                     parameters: dict, actor: str = "anonymous") -> ObjectEdit
    def edits(self, type_name: str, live_only: bool = True) -> list[ObjectEdit]
    def reindex(self, type_name: str) -> int      # full rebuild
    def catch_up(self, type_name: str) -> int     # replay the pending delta
    def store_is_caught_up(self, ot: ObjectTypeDef) -> bool   # alias: index_is_fresh
    def verify_digest(self, ot: ObjectTypeDef) -> bool
    def writeback(self, type_name: str, actor: str = "anonymous",
                  allow_transform_backed: bool = False) -> dict
```

Object materialization = **base + overlay**: base rows from the backing
dataset's latest version; then apply `store.list_object_edits(type)` in order:
`create` adds a row (payload must include primary key), `update` shallow-merges
payload into the row with that pk, `delete` removes it. `search` =
case-insensitive substring over string-typed properties; `filters` = equality
on property values (compared as strings). pk values compared as `str(value)`.

`query()` resolves that definition through **three paths, in order**, all of
which must return the same answer:

1. **Operational object store** (`_index_query`, `laurelin/ontology/store.py`) —
   one row per object, materialized. Used only when the type is materialized,
   the store proves it is level with the log, the request carries no row-level
   security, and any filter is on the primary key (a real indexed column).
   Never touches Parquet.
2. **SQL pushdown** (`_sql_query`) — filtering, search, counting and paging
   run in DuckDB over the Parquet parts, with the edit overlay merged in as
   typed Arrow tables and the row policy rendered into the same `WHERE`.
3. **In-memory scan** — the original path, for hash masking and anything the
   other two decline. Also the oracle the other two are tested against.

### The object store is a materialization, and the log is the truth

The store is **pluggable**: `MetadataObjectStore` (the default, co-located with
the edit log, zero new dependencies) or `StarRocksObjectStore` (for scale, via
Stream Load — see the caveat below). Either way the edit log in the metadata
store is the source of truth and the store is a materialization of it.

"Pluggable" today means *there is a seam*, not *an operator can switch stores*.
`OntologyService` constructs `MetadataObjectStore` and takes no env var, route
or config key that swaps it; the only construction of `StarRocksObjectStore`
anywhere is a test assigning `svc.object_store` directly. So the default store
is what every deployment runs, and no documentation should imply otherwise.

An edit **upserts** the materialization; it does not invalidate it. What used to
be an `edit_count` invalidation flag is now an `applied_seq` **catch-up
watermark**, and `object_edits.edit_seq` is a gapless per-type position
allocated inside the append transaction (a UNIQUE index serializes concurrent
appends). Gapless is what makes "replay everything above the watermark" a
*complete* description of the lag rather than a hopeful one; the ordering column
cannot serve, because Postgres allocates identity values before commit, so a
cursor parked at 6 can skip a 5 that commits later.

A store is **caught up** iff a state row exists, its `dataset_version` matches
the dataset's latest, its `applied_seq` is level with the log, and (for the
metadata store) the page and the state were read in one transaction. Anything
else — including *unreachable*, a missing state row, or a dropped table — means
**behind**, and a store that is behind returns nothing so the read falls through
to (2) and (3). Both read the log directly, so both are always correct. Never an
empty page: "these objects do not exist" is a valid-looking answer the write
path's existence check would act on.

Ordering: the log commits first, the materialization catches up, the watermark
advances last. A watermark behind its rows costs one idempotent replay; a
watermark ahead of them is a silent permanent stale read.

A new **dataset version still invalidates outright** — it can rewrite arbitrary
base rows and renumbers every ordinal, so no delta expresses it. So does a
change to the **object-type definition**: `object_index_state.type_fingerprint`
hashes the key, the title property and the declared properties with their types,
and a mismatch means behind. Without it, withdrawing a property from the
ontology took effect on every other read path (they project to the declared set
on every read) and not on the materialization, which kept serving it — and each
subsequent write copied it forward, because the write merges onto the stored
bag. A build or a definition change are the only things that invalidate.

Those two states also *accumulate lag*, because edits keep landing in a log the
store will never replay — which made "behind by N edits" a reading that promised
a catch-up that could never happen. `GET /ontology/object-types/{name}` therefore
reports the built-at `dataset_version` beside the dataset's current one, plus
`stale_version` and `stale_definition`, so an operator is told which of the three
they have and only the self-healing one says "the next write fixes this". The
version numbers are not withheld from a policied caller the way the object and
lag counters are: a version number counts versions, not rows.

**The rows an edit writes are built inside the transaction that writes them.**
`commit_edit` takes a `build(pre_image, seq)` callback rather than finished
rows, and the implementation reads the pre-image and allocates `seq` before
calling it, under a lock on the type's state row. This is the whole correctness
argument for concurrent writes, and it was originally missing: building the rows
outside made every edit a read-modify-write with the read on another connection,
which lost an update when two people edited one object, resurrected a deleted
object when a delete raced an update, gave four concurrent creates the same
ordinal, and computed the divergence digest from a superseded base. Every one of
those was **silent** — the watermark advanced normally, so the store was not
behind, it was wrong while level, and rule 2 never applied.

The lock is taken with a no-op `UPDATE` rather than `SELECT ... FOR UPDATE`:
Python's sqlite3 driver only opens a transaction at the first DML statement, so
on SQLite a leading `SELECT` reads in autocommit — outside the transaction it
was meant to be protected by.

`commit_edit` is **all or nothing**: either the edit is logged and its position
returned, or nothing is logged and it raises. The caller's recovery path appends
the edit itself, so a store that logged first and then failed would produce
either a duplicate or — as StarRocks did — an exception reported to a user whose
write was durable and visible to every reader, with no audit record of an action
that took effect.

Ordinals: `ord(base row i) = i`, `ord(created object) = 2**62 + edit_seq`. The
column is `BIGINT`, and that is load-bearing rather than tidy: `INTEGER` is
64-bit on SQLite and 32-bit on PostgreSQL, so on the default production control
plane the first object create overflowed it, the write path swallowed the error
and fell back to a log-only append, the user was told the write succeeded, and
the materialization was dead from then on with reads silently back on the full
scan. A rebuild hit the same overflow. A
create for a key that already exists keeps that key's ordinal (it is a
replacement); a create after a delete of the same key takes a new one. A rebuild
recomputes the *same* numbers rather than renumbering, or paging would reshuffle
on every rebuild.

Divergence: a watermark cannot detect it (a drifted store can be perfectly
caught up by position), so each materialization also carries a **digest** — XOR
of per-row SHA-256 over `(pk, applied_seq, canonical props JSON)`, maintained
incrementally, order-independent, computed in Python and stored rather than
recomputed by the engine. `verify_digest()` recomputes and compares.

**StarRocks object store: UNVERIFIED against a real server.** The engine
behaviours it relies on were measured (Stream Load is byte-faithful with no
escaping, upsert-by-pk works, a bad row aborts the whole load, data rows plus a
sentinel land in one transaction), but the class itself has only run against an
in-memory double. It writes exclusively via Stream Load — never SQL, because
`StarRocksDialect.literal()` raises by construction — and keeps its watermark in
StarRocks as a reserved `ord = -1` row excluded from every page.

Having no shared transaction, it cannot take the state-row lock, so it uses the
watermark instead: an edit is loaded **only** when the watermark is exactly one
position behind it. Anything else means an unaccounted-for edit could already
have changed the row this pre-image describes, so it skips the load, stays
behind, and lets `catch_up` replay in log order — which *is* single-writer. A
failed load is caught rather than raised, because the log commit already
happened. **NOT IMPLEMENTED there:** strict per-row position ordering on the row
itself (StarRocks resolves duplicate keys by load order and the DDL declares no
sequence column); the watermark gate above is what stands in for it.

`apply_action` validates: action exists; for update/delete pk must reference an
existing object; required parameters present; parameters must be declared;
update/create payload keys must be declared properties of the object type
(reject unknown, except pk on create). Coerce parameter values per declared type
(integer/float/boolean). Writes `ObjectEdit` + audit log. Raise `ValueError`
with a clear message on any validation failure (api maps to 400/404).

The existence check stays on the **policied** path; the row written to the store
is built from the **store's own** pre-image. Both halves matter: pointing the
existence check at an unfiltered store would make it an enumeration oracle over
other tenants' keys, and taking the pre-image from a policied read would write
one user's masked, row-filtered view into a table everyone shares.
`MetadataStore.add_object_edit` still exists and is still correct, but it is
log-only and therefore a footgun; `commit_object_edit` is the public write.

A **create for a key that already exists is a replacement** — it takes over that
object's ordinal and a fold writes it over that row in the dataset. For a caller
under a dataset policy that is a cross-tenant destructive write, so it is
refused: `_policy_admits` closed the half where a policied user *reads* an
overlay create, and this closes the half where they *write* one. The refusal is
uniform over existing keys, visible or hidden, so it does not distinguish
"yours" from "someone else's"; it does remain an existence oracle over a key the
caller already named, which is inherent to a shared unique key and is stated
rather than papered over. Unpoliced callers keep create-as-replacement.

An **update is re-checked as the merged row**, for the same reasons one step
later. The overlay is applied *after* the policied scan, so an update to a
masked property was read straight back in plaintext by its author — and was a
blind overwrite of a value they were never shown. And the row filter runs on the
*base* value, so an update could push a row into another tenant's partition and
keep showing it to the editor who moved it. `_refuse_policy_escaping_update`
rebuilds the row the edit would produce, runs it back through the caller's own
policy, and **refuses** if any written column comes back masked or the merged
row comes back filtered out. Refuses, rather than dropping the offending
properties: reporting success for a write that did not happen is worse than
either bug. The base row it needs is read through the system view (as `reindex`
does) and never reaches the caller — not even in the error message.

The `index` block of `GET /ontology/object-types/{name}` reports `objects`,
`lag` and `applied_seq` as `null` to callers the backing dataset's policy
narrows: those counters describe the shared, unpoliced materialization, so a
tenant seeing three of six objects was being told there were six. The
`dataset_version` / `stale_version` / `stale_definition` fields beside them are
*not* narrowed — see the store section above for why.

### Writeback (manual only)

`POST /ontology/object-types/{name}/writeback` folds the overlay into a new
dataset version — `catalog.write(..., source="writeback")`, never `append` (an
overlay delete has no expression as an appended row) — then marks exactly the
edits it captured as folded. Marked, not deleted: `folded_into_version` is what
makes a folded version reproducible, and the audit log is not a substitute
because `prune_audit` trims it by design.

Three races, handled: an edit arriving mid-build is not in the captured id list
so it stays live and applies on top of the new version; the write happens before
the marking, so a crash between them replays idempotently rather than losing
edits; and the fold publishes with **compare-and-set** —
`catalog.write(..., expect_version=base)` takes exactly `base + 1` or registers
nothing and raises `StaleBaseVersion`. It was a read-the-version-then-write
check, and the gap between the two was reachable by two operators, a double
click, a client retry or a nightly build: one fold marked an edit folded while
the other published a version rebuilt from the old base without it (the live log
was then empty, so nothing could restore it), and a concurrent build lost its
whole version the same way.

It runs unpoliced (a fold through a policy would rewrite the dataset as one user
sees it) and reads **all** columns (an object type rarely declares every column;
folding through the projection would delete the rest — as would emitting a
create's row wholesale over an existing one, which is why a create contributes
only its *declared* columns and inherits the rest from the row it replaces).
Overlay updates merge with a per-column "was assigned" flag rather than
`COALESCE`, which cannot tell an assigned NULL from an absent one — so clearing
a property survives a fold, and keeps working afterwards.

It refuses a backing dataset that is scanned at the source; a backing whose
declared primary key is **not unique**, because the object view's last-wins
dedup would otherwise be materialized into the dataset and delete rows no edit
referenced; and a transform-produced backing by name unless explicitly
overridden — otherwise the next build overwrites the folded edits silently,
hours later. "Transform-produced" looks at *every* version's source, not the
latest: writeback stamps its own version `writeback` and compaction stamps
`compact`, so the latest-only test erased the evidence and the guard fired
exactly once per dataset.

**NOT IMPLEMENTED:** automatic unfolding when a rebuild supersedes a folded
version (which is *why* `folded_into_version` is a column), and automatic
writeback triggers. Writeback on its own bounds **read cost, not disk** —
pruning is what bounds disk, and it is below.

#### Pruning the edit log

The log is truth and nothing trimmed it, so a workspace using the ontology as
an application database grew `object_edits` forever. `prune_plan(type, keep)`
and `prune_object_edits(type, keep)` bound it, and the accounting is the
feature: the plan is pure, it names what would go and *why each retained edit
stayed*, and the UI shows the size before it offers the button.

An edit may be deleted only when five things hold, each checked against the
world as it is now rather than inferred from how the edit got here:

1. it is **folded** — an unfolded edit is not history, it is the current value
   of those objects and every read replays it;
2. the version it was folded into is **still in the backing dataset's history**
   and its source is `writeback`. This is what ties the edit to *this* dataset:
   rebind an object type and the fold's version number means something else in
   the new one, so the check fails and nothing goes;
3. **no version written since the fold could have superseded it.** `writeback`
   builds on the previous version and `compact` rewrites the same rows, so both
   carry a fold forward; anything else (a transform build, an upload, a sync, a
   merge) may have replaced those rows, and then the log rows are the only
   surviving record of the hand edits. This is the same hazard the unfolding
   entry above is about — until unfolding exists, those edits are load-bearing;
4. it is **not the row holding `MAX(edit_seq)`**. The allocator is `MAX + 1` and
   a materialization's watermark is compared against `max_edit_seq`; delete the
   top row and the next edit re-uses a number the store already claims to have
   applied — `catch_up` skips it forever while the freshness check says fresh.
   Enforced again in `MetadataStore.delete_object_edits`, next to the allocator;
5. it is outside the operator's retention window (`keep` newest folded edits).

Conditions 1 and 4 are re-checked in SQL inside the deleting transaction, so a
plan that goes stale removes *fewer* rows rather than the wrong ones. What
pruning costs is real and is not correctness: the answer to "what changed in
version N and who did it" for the pruned window. `POST .../edit-log/prune` is
ADMIN for that reason, a rank above the fold that made the edits redundant.

Automatic pruning is off unless `LAURELIN_EDIT_LOG_MAX_FOLDED` is set, in which
case a fold prunes to that many folded edits afterwards — the same convention
as `LAURELIN_AUDIT_MAX_EVENTS`, and for the same reason. A prune that fails
never fails the fold: the dataset already holds the edits.

#### Aggregation

`aggregate(type, group_by, metrics, filters, search, limit)` groups the same
object set `query()` pages. Both go through `_object_scan`, which yields
`(con, sql, params)` for "the objects of this type" — paging and aggregation
are that question asked twice, and two definitions of it would eventually
disagree about a deleted row or a policy.

Metric ops are an **allowlist** mapping to SQL functions; anything unlisted is
rejected rather than interpolated. Results are ordered by the first metric
descending, so the group cap (`MAX_GROUPS`, 1000) keeps the interesting rows
and `truncated` says when it bit.

When the scan declines (hash masking, an untypeable overlay) the aggregate is
computed over the exact in-memory objects instead. Slower, never wrong — and
never a way to read rows the object list wouldn't show.

### `laurelin/api` — `create_app(workspace: Workspace) -> FastAPI`

Construct catalog/store once; build registry + ontology **per request group**
(cheap; re-reads pipelines/ontology so edits show up without restart — use
FastAPI dependencies). Static UI mounted at `/` (html=True) from
`laurelin/ui/static`; API under `/api/v1`; docs at `/docs`. CORS: allow all.
Auth: see **Authentication & authorization** below. `actor` for
audit/edits = the authenticated username (or `X-Laurelin-User` header /
"anonymous" only in `--no-auth` mode).

#### Authentication & authorization

Auth state lives in `metadata.db` (new tables, managed by `MetadataStore`):

```sql
users(id TEXT PK, username TEXT UNIQUE COLLATE NOCASE, password_hash TEXT,
      role TEXT CHECK(role IN ('viewer','editor','admin')), created_at TEXT,
      disabled INTEGER DEFAULT 0)
sessions(token_hash TEXT PK, user_id TEXT, created_at TEXT, expires_at TEXT)
api_tokens(id TEXT PK, name TEXT, token_hash TEXT UNIQUE, user_id TEXT,
           created_at TEXT, last_used_at TEXT)
```

`laurelin/core/auth.py` provides:
- `hash_password` / `verify_password` — stdlib `hashlib.scrypt` (n=2**14, r=8,
  p=1, 16-byte random salt), format `scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>`;
  verify with `secrets.compare_digest` (via `hmac.compare_digest`).
- `new_token() -> str` — `secrets.token_urlsafe(32)`; stored only as
  `sha256(token).hexdigest()`.
- `AuthService(store)` — `create_user`, `authenticate(username, password)`
  (constant-time-ish: always run scrypt even for unknown users; in-memory
  throttle: ≥5 consecutive failures per username → locked 30s → return
  "throttled" sentinel), `login() -> (session_token, User)` (7-day expiry),
  `logout(token)`, `resolve_session(token) -> User|None` (checks expiry +
  disabled), `resolve_api_token(token) -> User|None` (updates last_used_at),
  user/token management helpers. Expired sessions are purged opportunistically.

`User` pydantic model in core/models.py: `id, username, role, created_at,
disabled` (never expose password_hash through the API). Roles are ordered
`viewer < editor < admin`.

**Single vs multi-workspace.** `serve --workspace X` (single) binds one
workspace; identity lives in its `metadata.db` and a user's role is their
account role — the original behavior. `serve --root R` (multi) hosts many
workspaces: global identity + a workspace registry + per-workspace membership
live in the control store — `<root>/control.db` (SQLite) by default, or
PostgreSQL via `--control-db postgresql://…` / `LAURELIN_CONTROL_DATABASE_URL`
(`laurelin/core/control.py`, `ControlStore`; the backend is abstracted in
`laurelin/core/backend.py` so both stores run on either engine). Each workspace
is `<root>/<slug>/` with its own data, ACLs, groups, and audit. Ships as a Docker
image + `docker-compose.yml` (Laurelin + Postgres); the prebuilt UI means the
image needs no Node toolchain.
The active workspace is chosen per request via the `X-Laurelin-Workspace`
header or `laurelin_workspace` cookie; `laurelin/api/context.py` resolves it and
builds/caches that workspace's store+catalog. A user's *effective* role is their
membership role in the active workspace (or `admin` if they are a
**superadmin** — a server administrator who manages workspaces + global users
and is admin everywhere). First-run setup in multi mode creates the first
superadmin. `require_identity` = the global user (control-plane routes);
`require_user` = identity + effective workspace role (workspace-scoped routes);
`require_superadmin` = server admin (workspace admin in single mode). Control-
plane endpoints: `/api/v1/workspaces` (CRUD + `/{slug}/members`), superadmin
only, 404 in single mode. `/api/v1/users` is superadmin (global) in multi mode.
Auth `/status` and `/me` include the user's workspaces (+ role in each) so the
UI can render a switcher. Isolation is enforced at the route layer: a user only
reaches a workspace they are a member of (or a superadmin). The workspace-
existence check is gated behind authentication so anonymous callers can't
enumerate slugs. **Deleting** a workspace only *unregisters* it (reversible) —
its files stay under `<root>/<slug>/`; reusing a slug re-exposes that data, so
purge the directory before reusing a slug for a different tenant.

**Classification markings** (`markings`/`dataset_markings`/`clearances` tables;
`PermissionService._has_clearance`). Mandatory access control layered on top of
the discretionary ACLs: an admin defines markings (e.g. `pii`, `confidential`),
assigns them to datasets, and grants users *clearances*. A non-admin sees a
dataset only if they hold clearance for **every** effective marking on it
(deny-by-default); admins/superadmins are the data stewards and bypass. The
differentiator is **lineage propagation**: `store.recompute_all_markings()` (run
after every build and on any marking change) recomputes each dataset's effective
markings as its explicit markings ∪ the union of its lineage upstreams' effective
markings — so a derived dataset inherits its inputs' classifications and
classified data can't be laundered through a transform. Enforced everywhere via
`dataset_permission` (row API, SQL page, ontology objects). Admin API:
`/markings`, `/dataset-markings`, `/datasets/{name}/markings`,
`/users/{username}/clearances`.

**SCIM 2.0 provisioning** (`laurelin/api/scim_routes.py`, enabled by
`LAURELIN_SCIM_TOKEN`). An IdP pushes users + groups and, importantly,
*deprovisions* them — setting a SCIM user inactive/deleted disables the Laurelin
user, which immediately invalidates their sessions and API tokens. SCIM maps
onto the identity store (users/groups); the IdP authenticates with the bearer
token. A minimal-but-real subset: `/scim/v2/Users` + `/Groups` (list/create/get/
put/patch/delete) and `ServiceProviderConfig`.

**SAML 2.0 SSO** (`laurelin/core/saml.py`, pysaml2 + xmlsec1; enabled by
`LAURELIN_SAML_IDP_METADATA` + `LAURELIN_SAML_SP_ENTITY_ID`). SP-initiated
(`/auth/saml/login` → IdP) and IdP-initiated (unsolicited POST to
`/auth/saml/acs`); IdP assertions must be signed (verified via xmlsec1). Valid
responses JIT-provision an identity and map group attributes to a role, then
issue the normal session — same downstream path as OIDC. `/auth/saml/metadata`
serves SP metadata for the IdP. `/auth/status` reports `saml:{enabled,name}`.

**OIDC SSO** (`laurelin/core/oidc.py`). When `LAURELIN_OIDC_ISSUER` +
`_CLIENT_ID` + `_CLIENT_SECRET` are set, an authorization-code + PKCE flow is
enabled: `/api/v1/auth/oidc/login` stores per-flow state/nonce/verifier
(`oidc_flows` table, single-use, ~10-min TTL) and redirects to the IdP;
`/api/v1/auth/oidc/callback` exchanges the code, validates the id_token
(signature via JWKS, `iss`/`aud`/`exp`/`nonce`), JIT-provisions a local identity
(`AuthService.provision_oidc_user`), maps IdP group claims to a role
(`LAURELIN_OIDC_ROLE_MAP`, e.g. `admins:admin,editors:editor`; optional
`_SUPERADMIN_GROUP`), and issues the normal session cookie — so RBAC,
workspaces, and ACLs are unchanged. `/auth/status` reports
`oidc:{enabled, provider_name}` so the UI shows a "Sign in with <provider>"
button. Existing users keep their local role (a local admin can override the IdP
mapping); `disabled` still blocks SSO login.

**Modes.** Auth is ON by default. `laurelin serve --no-auth` (or env
`LAURELIN_NO_AUTH=1`) disables it for local development — `/api/v1/auth/status`
then reports `{"auth_required": false}` and every request acts as an implicit
admin. With auth on and **zero users**, the server is in *setup mode*: every
data endpoint returns 401 `{"detail": "setup required"}`; only
`/api/v1/auth/status` and `POST /api/v1/auth/setup` (creates the first admin,
409 once any user exists) are reachable. The old `LAURELIN_TOKEN` env mechanism
is REMOVED.

**Credentials.** Either an httpOnly session cookie `laurelin_session`
(SameSite=Lax, Path=/, Max-Age 7d; `Secure` when `--secure-cookies` or
`X-Forwarded-Proto: https`) or `Authorization: Bearer <api-token>`. Static UI
files, `/health`, and auth endpoints listed below stay reachable without
credentials; `/docs`, `/redoc`, `/openapi.json` and all other `/api/` routes
require auth (when enabled).

**CSRF.** Mutating requests (POST/PUT/PATCH/DELETE) authenticated via session
cookie: if an `Origin` header is present it must match the request host, else
403. Bearer-token requests are exempt (no ambient credential).

**RBAC.** viewer: most GETs. editor: viewer + POST datasets / upload / builds /
action apply, plus the GETs listed below. admin: editor + user & token
management. Enforced via a `require_role(...)` dependency; violations → 403
`{"detail": ...}`.

**Route gating is only half of it.** A route says who may call it; it does not
say who may read each *field* of what comes back, and "viewer: all GETs" was how
a dashboard panel's SQL and a driver's exception text reached people who could
not have written them. Field audiences (`laurelin/core/audience.py`) are the
other half, enforced at the single serialization point
(`laurelin/core/serialize.py`):

> A field is readable by a principal iff the principal's effective role is at
> least the field's audience role. The audience role is `viewer` **iff** the
> field is explicitly declared `PRESENTATION`; otherwise it is the authoring
> role of the record type that contains it. **If you cannot write it, you
> cannot read it.**

Anything not annotated is `OPERATIONAL`, and any model that has not declared a
`laurelin_author_role` is admin-only — so a field or model added tomorrow fails
closed. The role reaches the serializer through a `ContextVar`
(`serialize._effective_role`) set by the `audience_middleware` HTTP middleware
in `laurelin/api/app.py` — a middleware and not a dependency, because FastAPI
runs sync dependencies and sync handlers in *separate* threadpool context
copies. Its default is `viewer`: a route that arranges nothing still gets
filtering, and forgetting redacts more rather than less.

**This is not free, and the cost is published rather than assumed.** Running a
per-field decision on every record of every response made serialization 3–11×
more expensive than what it replaced; memoizing the decision on the shape it
depends on recovered about half, and the residual — ~1.2× for a viewer, 2.9–4.9×
for an admin, who pays more precisely because a viewer's projection drops fields
before they are walked — is measured in
[SCALE.md](SCALE.md#what-the-audience-projection-costs) and reproducible with
`bench/serialize_cost.py`. Note the shape of that: the projection is *cheaper*
the less it discloses.

Three refinements, each of which is a defect that was reproduced on a running
server before it was a design note.

**A field can be authored above its record.** `DatasetInfo` is editor-authored,
but `DatasetInfo.source` is written only by the three ADMIN registration routes,
so the *field* crosses a privilege boundary that its *record* does not — and
until it said so, a plain editor read five live credentials out of
`GET /datasets/{name}` with nothing but the free-text matcher in the way.
`audience.AuthoredBy(Role.admin)` on the field is the declaration. It is not a
third audience: there are still exactly two, and this is the same `Role`
vocabulary already used for records, applied at the granularity the data has.
A field cannot be made *more* readable by annotating it — only less. What a
lower-privileged reader gets instead is `DatasetInfo.source_descriptor`, built
by `models.source_descriptor` from an allowlist of shape keys with an
identifier-shaped gate on every value: a key not on the list is absent whatever
it is called, and a value that is not identifier-shaped is absent whatever it
contains. Both questions are decidable, which is the entire point.

**Recursion narrows; it must never widen.** A nested model re-evaluates its own
author role on the way in, so it can be *stricter* than its parent — but if the
reader could not author the parent, the nested record is forced to its
projection regardless. Without that, a `Failure` (editor-authored) inside a
`SourceInfo` (admin-authored) was dumped in full to an editor who had correctly
been handed only the parent's projection, restoring the admin's `endpoint`
through a field annotated `PRESENTATION`.

**A record's author role can be per-instance.** `AuditEvent.min_read_role` is a
level the row's *writer* declared, and it is the honest author role of that
row's `details`. Before `laurelin_record_author_role` existed, two mechanisms
answered the same question and the second cancelled the first: `list_audit`
chose which rows an editor was offered, and then the serializer dropped
`details` from every one of them because the class says `admin`. All five
`min_read_role=Role.editor` declarations in the tree were dead code — while the
VIEWER-gated `/audit/mine` re-attached the raw bag by hand and served it whole.
`details` is an open `dict`, so the serializer cannot reach inside it; writers
put `Failure.audit_projection()` there rather than the record.

Three GET routes were raised out of "all GETs" by the audience rule:
`/pipelines` and `/pipelines/{name}` (they return `exec`-ed Python) and `/audit`
(an open details bag written by admin-level callers). `/audit/mine` and
`GET /transforms` / `GET /lineage` serve the viewer's actual needs.

**Error paths are read paths.** A route's `HTTPException(detail=…)` never
travels through `serialize.dump`, so R2 has no jurisdiction over it unless the
route asks. Three confirmed disclosures came back in **4xx bodies**, quoting the
very stored instruction the read path withholds — a panel's `group_by`, an
object app's `filters`, a connector's endpoint. Two helpers close it:

- `serialize.detail_for(failure, author_role)` renders a `Failure` in full above
  the level that authored the *configuration it describes*, and briefly below
  it — `Failure.render_brief()` drops `endpoint`, the one field whose value
  comes from somebody's config rather than a closed set.
- `routes.stored_instruction_error(exc, subject=…, author=…)` decides who may
  read the sentence that names a broken stored instruction. Above the authoring
  level, the message — they can repair it, so they must be told which field.
  Below it, a `Failure` with code `definition_stale` and a `detail_ref`.

And a net under R1's catch sites, because `@app.exception_handler(ValueError)`
turns anything uncaught into a body: `failure.is_first_party(exc)` decides on
the **deepest traceback frame** — where the `raise` is written, a fact — rather
than on the exception's type, which lies (`pyarrow.lib.ArrowInvalid` is a
`ValueError`; `ArrowKeyError` is a `KeyError`). `failure.safe_detail(exc)` is
that rule as a one-liner, and every route-level `except ValueError` that used to
return `str(exc)` now calls it — a no-op when the exception is ours, and a
`Failure` when it came out of pyiceberg, pyarrow or a driver.

**The guard.** `tests/test_audience.py` drives **every method of every route at
two privilege levels**, with sentinels planted at admin- and editor-authoring
level in every stored operational field, and asserts on the response body
whatever the status code is. The earlier version swept GET only, checked 200s
only, and silently skipped any route that 404'd — 24 of 144 routes, with
`GET /dashboards/{name}` never checked once because the fixture's dashboard had
a different name from `PATH_PARAMS["name"]`. Every seeded record now shares one
name, and a GET that 404s even for an admin fails a companion test unless it is
listed with a reason.

Auth endpoints (under `/api/v1/auth`, all except status/setup/login require a
valid credential):

```
GET  /api/v1/auth/status   -> {"auth_required": bool, "setup_required": bool,
                               "user": User|null}          (never 401)
POST /api/v1/auth/setup    {username, password} -> User    (only in setup mode, else 409)
POST /api/v1/auth/login    {username, password} -> User + Set-Cookie
                           (401 bad creds/disabled, 429 throttled)
POST /api/v1/auth/logout   -> {"ok": true} + cookie cleared
GET  /api/v1/auth/me       -> User
GET  /api/v1/users                       -> [User]                       (admin)
POST /api/v1/users         {username, password, role} -> User           (admin)
PATCH /api/v1/users/{username}  {role?, password?, disabled?} -> User   (admin;
                           an admin cannot disable/demote themselves)
DELETE /api/v1/users/{username}          -> {"ok": true}                (admin, not self)
GET  /api/v1/tokens        -> own tokens (admin: all) [{id,name,username,created_at,last_used_at}]
POST /api/v1/tokens        {name} -> {id, name, token}   (token shown ONCE; editor+)
DELETE /api/v1/tokens/{id} -> {"ok": true}   (own; admin: any)
```

Password rules: min 8 chars (400 otherwise). Usernames: `^[a-z0-9_.-]{2,32}$`
case-insensitive-unique. Login failures and lockouts, user create/update/delete,
token create/revoke are all audit-logged (never log passwords or tokens).

CLI: `laurelin users create USERNAME [--role r] [--password p]` (hidden prompt
when --password omitted; first user may also be created this way), `users list`,
`users passwd USERNAME`, `users role USERNAME ROLE`, `users disable|enable USERNAME`,
`users delete USERNAME`; `laurelin tokens create NAME --user USERNAME` (prints
token once), `tokens list`, `tokens revoke ID`; `laurelin serve --no-auth
--secure-cookies`.

REST endpoints (all JSON; errors as `{"detail": str}` with 400/404):

```
GET  /health                                  -> {"status":"ok","version":...}
GET  /api/v1/workspace                        -> {name, description} (+ `root`
                                 for an ADMIN only: it is the server's
                                 filesystem layout, which a viewer cannot act
                                 on and was never meant to have)
GET  /api/v1/datasets                         -> [DatasetInfo]
POST /api/v1/datasets                         {name, description?} -> DatasetInfo
GET  /api/v1/datasets/{name}                  -> DatasetInfo + versions: [DatasetVersionInfo]
                                 (`source` — the connection config — is ADMIN
                                  only, via audience.AuthoredBy; everyone else
                                  gets `source_descriptor`: which table, in
                                  which format, from an allowlist)
GET  /api/v1/datasets/{name}/schema?version=  -> [ColumnSchema]
GET  /api/v1/datasets/{name}/rows?limit=&offset=&version= -> {"rows":[...],"row_count":N}
POST /api/v1/query        {sql, max_rows?} -> {columns,rows,row_count,truncated}
                                 (read-only DuckDB; each dataset is a view; viewer+)
POST /api/v1/datasets/{name}/upload?mode=replace|append
                                              multipart file (.csv/.parquet) -> DatasetVersionInfo
POST /api/v1/datasets/{name}/compact          -> DatasetVersionInfo (merge appended parts; edit)
PUT  /api/v1/datasets/{name}/federated        {source, description?} -> DatasetInfo  (admin)
                                 (source: iceberg|delta|parquet {path} or
                                  postgres {url, table}; validated and probed
                                  before storing; secrets redacted in responses)
PUT  /api/v1/datasets/{name}/clickhouse       {source, description?} -> DatasetInfo  (admin)
                                 (source: parquet {path}, read by embedded
                                  chdb; probed before storing; read-only —
                                  upload/append to it is a 400; 501 without
                                  the `clickhouse` extra)
PUT  /api/v1/datasets/{name}/starrocks        {source, description?} -> DatasetInfo  (admin)
                                 (source: table {url: starrocks://user:pw@host:9030/db,
                                  table: "db.tbl" or "catalog.db.tbl"}; probed
                                  before storing, DSN redacted in responses;
                                  read-only — upload/append is a 400; 501
                                  without the `starrocks` extra)
GET  /api/v1/lineage                          -> {"nodes":[{id,type:"dataset"|"transform"}],"edges":[{from,to}]}
                                                 (dataset->transform->dataset graph derived from lineage_edges)
GET  /api/v1/transforms                       -> [{name, output, inputs:[dataset], kind}]
POST /api/v1/builds                           {targets?: [str], wait?: bool} -> BuildInfo
                                 (default async: returns the pending build, a
                                  worker executes it; wait=true blocks)
GET  /api/v1/builds                           -> [BuildInfo]
GET  /api/v1/builds/{id}                      -> BuildInfo
GET  /api/v1/dashboards                       -> [DashboardInfo]  (viewer)
GET  /api/v1/dashboards/{name}                -> DashboardInfo    (viewer)
                                 (a viewer receives the LAYOUT — id, title,
                                  chart, x/y, width — and not `sql` or the
                                  object aggregation: those are OPERATIONAL,
                                  see laurelin/core/audience.py)
POST /api/v1/dashboards/{name}/panels/{id}/run {max_rows?}
                                              -> {columns, rows, row_count, truncated}
                                 (viewer; the server runs the STORED panel **as
                                  the caller**, applying that caller's ACL /
                                  row-level security / column masking. This is
                                  how a viewer gets the data without the query,
                                  and why "a stored dashboard grants nobody new
                                  read access" still holds.
                                  ONE shape for both panel kinds: an object
                                  panel's `{groups, group_count}` is normalized
                                  to `{columns, rows}` here, because the client
                                  used to do that from the panel's own
                                  `group_by`/`metrics` and no longer receives
                                  them.)
PUT  /api/v1/dashboards/{name}                {title?, description?, panels} -> DashboardInfo (editor)
                                 (whole-board replace. An OPERATIONAL field left
                                  absent or empty on a panel whose id already
                                  exists inherits the stored value, so a
                                  read-modify-write client cannot blank a query
                                  it never received. Response carries
                                  `warnings: [{field, hint}]` — non-blocking
                                  authoring hints, NOT a security control.)
POST   /api/v1/dashboards/{name}/panels             DashboardPanel -> DashboardInfo (editor)
PUT    /api/v1/dashboards/{name}/panels/{id}        DashboardPanel -> DashboardInfo (editor)
DELETE /api/v1/dashboards/{name}/panels/{id}        -> DashboardInfo (editor)
                                 (per-panel edits, so a client never has to
                                  re-PUT the whole board)
DELETE /api/v1/dashboards/{name}              -> {deleted}  (editor)
GET  /api/v1/apps                             -> [ObjectAppInfo]  (viewer; filtered
                                 to apps whose object type the caller may see)
GET  /api/v1/apps/{name}                      -> ObjectAppInfo    (viewer; 403 mirrors
                                 the object type's own permission. `filters` is
                                 OPERATIONAL and is not sent below admin.)
GET  /api/v1/apps/{name}/objects?search=&limit=&offset=
                                              -> ObjectQueryResult (viewer)
                                 (the app's objects, scoped by its STORED
                                  filters. Necessary because the client used to
                                  apply them and no longer receives them —
                                  without this an app shows its whole object
                                  type. `filter.*` in the query string is
                                  ignored: the scope comes from the definition
                                  and nowhere else. Adds no access of its own —
                                  the object type's permission, row policy and
                                  column masks apply as on the generic route.)
PUT  /api/v1/apps/{name}                      {object_type, columns?, filters?,
                                               actions?, links?, title?} (admin;
                                 validated against the live ontology on save)
DELETE /api/v1/apps/{name}                    -> {deleted}  (admin)
GET  /api/v1/schedules                        -> [ScheduleInfo]  (editor)
GET  /api/v1/schedules/{name}                 -> ScheduleInfo    (editor)
PUT  /api/v1/schedules/{name}                 {trigger, cron|upstream_dataset,
                                               action, targets|source, enabled}
                                              -> ScheduleInfo (editor; validated on save)
DELETE /api/v1/schedules/{name}               -> {deleted}  (editor)
POST /api/v1/schedules/{name}/run             -> {queued, due_at}  (make it due now)
GET  /api/v1/sources                          -> [SourceInfo]  (editor; `config` ADMIN only)
GET  /api/v1/sources/{name}                   -> SourceInfo    (editor; `config` ADMIN only)
PUT  /api/v1/sources/{name}                   {type, dataset, config} -> SourceInfo  (admin)
                                 (types: postgres {url, table|query, batch_size?},
                                  http {url, format?, headers?}, file {path, format?})
DELETE /api/v1/sources/{name}                 -> {deleted}  (admin)
POST /api/v1/sources/{name}/sync              -> DatasetVersionInfo  (needs edit on the
                                 target dataset; failures recorded on the source, 502)
GET  /api/v1/ontology/object-types            -> [ObjectTypeDef]  (only viewable
                                 types; each carries permissions:{can_view,can_edit})
GET  /api/v1/ontology/object-types/{name}     -> ObjectTypeDef + links + actions +
                                 permissions:{can_view,can_edit}  (403 if not viewable)
POST /api/v1/ontology/object-types/{name}/index -> {object_type,objects:N,state}  (editor)
DELETE /api/v1/ontology/object-types/{name}/index -> {"dropped": name}  (editor)
POST /api/v1/ontology/object-types/{name}/writeback -> {object_type, folded:N,
                                 version, row_count, objects}  (editor on the
                                 object type AND edit on the backing dataset —
                                 this rewrites a dataset, which is a different
                                 privilege from recording an edit)
GET  /api/v1/ontology/object-types/{name}/edit-log?keep=N -> {edits,live,folded,
                                 payload_bytes, prunable, prunable_bytes,
                                 retained:[{reason,edits,bytes}], withheld}
                                 (editor on the type; counters withheld from a
                                 caller whose row policy narrows the dataset)
POST /api/v1/ontology/object-types/{name}/edit-log/prune?keep=N&dry_run=
                                 -> the same plan plus {pruned:N}  (ADMIN:
                                 deletes the record of who changed what)
GET  /api/v1/ontology/objects/{type}?search=&limit=&offset=&filter.<prop>=<val>
                                     -> {"objects":[...],"total":N,"total_capped":bool}  (403 if not viewable)
POST /api/v1/ontology/objects/{type}/aggregate {group_by,metrics,filters,search,limit}
                                     -> {groups:[...],group_count:N,truncated:bool}
GET  /api/v1/ontology/objects/{type}/{pk}     -> object dict (404 if absent)
GET  /api/v1/ontology/objects/{type}/{pk}/links/{link} -> {"objects":[...]}
GET  /api/v1/ontology/actions                 -> [ActionDef]  (viewable types only)
POST /api/v1/ontology/actions/{name}/apply    {pk?, parameters} -> ObjectEdit  (needs edit)
GET  /api/v1/ontology/permissions             -> [{object_type, grants:[Grant]}]  (admin)
PUT  /api/v1/ontology/permissions/{type}      {grants:[Grant]} -> {object_type,grants}  (admin)
GET  /api/v1/dataset-permissions              -> [{dataset, grants:[Grant]}]  (admin)
PUT  /api/v1/datasets/{name}/permissions      {grants:[Grant]} -> {dataset,grants}  (admin)
GET  /api/v1/dataset-policies                  -> [{dataset, policy:DatasetPolicy|null}]  (admin)
PUT  /api/v1/datasets/{name}/policy            {row_policy?, column_masks?} -> {dataset,policy}  (admin)
GET  /api/v1/groups                           -> [{name, members:[username]}]  (admin)
POST /api/v1/groups                           {name} -> {name, members:[]}  (admin)
PUT  /api/v1/groups/{name}/members            {members:[username]} -> {name,members}  (admin)
DELETE /api/v1/groups/{name}                  -> {ok:true}  (admin)
GET  /api/v1/pipelines                        -> [{name, transforms:[str], failed, bytes}]  (editor)
GET  /api/v1/pipelines/{name}                 -> {name, content, failure}  (editor)
                                 (both raised from viewer: they return `exec`-ed
                                  Python. A viewer's lineage need is served by
                                  GET /transforms and GET /lineage, which stay
                                  viewer — names and edges, not authored prose.)
PUT  /api/v1/pipelines/{name}                 {content} -> {name,transforms,collect_error}  (editor)
DELETE /api/v1/pipelines/{name}               -> {ok:true}  (editor)
POST /api/v1/pipelines/from-query             {sql, output, name?} -> {name,...}  (editor)
GET  /api/v1/audit?limit=                     -> [AuditEvent]  (editor; rows are
                                  further filtered by `audit_log.min_read_role`,
                                  which defaults to admin so a new log_audit
                                  call site discloses to nobody below admin)
GET  /api/v1/audit/mine?limit=                -> [AuditEvent]  (viewer; only rows
                                  this user is the actor of, returned whole —
                                  you cannot learn a secret from a row you
                                  wrote)
```

**Grant** = `{subject_kind: "everyone"|"role"|"group"|"user", subject: str,
can_view: bool, can_edit: bool}` (`subject` empty for `everyone`).

#### Fine-grained ontology permissions (`laurelin/core/permissions.py`)

Each object type has a list of grants. Model: **admins bypass**; a type with
**no grants** inherits global RBAC (any authed user views, editor+ edits — so
existing workspaces are unchanged); a type with **any grant** becomes an
allowlist — a user may view/edit only via a matching grant (by everyone / their
role / a group they belong to / their username), and `can_edit` implies view.
Grants both restrict (hide a type) and elevate (let a specific viewer edit one
type). Enforced at the route layer: object reads need view, action apply needs
edit, listings are filtered. Groups are named user sets, admin-managed, usable
as a grant subject.

**Dataset ACLs & composition.** The same grant model applies per dataset
(`dataset_grants`, `PermissionService.dataset_permission`), enforced on **every**
data path: `/datasets` (list filtered), `/datasets/{name}`, `/schema`, `/rows`
(view), `/upload` (edit), and `/query` — the query registers only the datasets
the caller can view, so a blocked dataset is simply an unknown table. Every
dataset is registered through `DatasetCatalog.scan_for`, which resolves a
`PolicyPlan` (`PermissionService.arrow_policy_fn`):

- **no policy** → the bare lazy Arrow dataset (DuckDB pushes projection and
  filters into the Parquet scan; memory tracks the result, not the dataset);
- **row policy** → `Dataset.filter(expr)`, which stays a *Dataset*, so
  pruning still applies and enforcement is effectively free;
- **column masks** → a Scanner with computed columns (fixes the projection,
  so pruning is lost);
- **hash masking** → materialize and run the exact policy engine (no Arrow
  sha256 equivalent).

Whichever branch is taken, the rows and values are identical to the
materializing path — `tests/test_rls_pushdown.py` asserts that equivalence
across policy shapes and users. External access stays disabled either way, so
the SQL can never touch the filesystem.

**Two renderers, one decision.** `PermissionService.decide()` resolves *what* a
policy does for a user (allowed values, resolved masks) independently of how
it will run. `_plan()` renders that as Arrow filter+projection for managed
data; `sql_policy_fn(user, dialect=…)` renders it as a SELECT list + WHERE for
datasets scanned in place — federated (Iceberg/Delta/Parquet/Postgres via
DuckDB, `laurelin/core/federation.py`), ClickHouse (via chdb,
`laurelin/core/clickhouse.py`) and StarRocks (over the MySQL wire protocol,
`laurelin/core/starrocks.py`). One place interprets the rules, so a second or
third execution engine cannot grow a second interpretation of them; a *dialect*
(`laurelin/core/dialects.py`) chooses only how the decision is spelled, and
`catalog.source_table` refuses a policy rendered for the wrong one. The SQL
renderers are strictly more capable than Arrow — both engines have SHA-256, so
they express hash masking inline where Arrow must materialize.

Two rules that were fail-*open* before ClickHouse forced them into the light,
and now apply to every dialect: an empty column list is a **refusal** (it used
to render `select_list='*'` with masks pending), and a mask whose column name
differs from a real one only in **case** is a refusal rather than a silent
no-op (a mask on a genuinely dropped column still passes, so schema evolution
does not start denying datasets). Object-type
access is now **composed**: effective view = ontology-view AND backing-dataset-
view; effective edit = that view AND ontology-edit. So locking a dataset also
hides its objects, and there is no longer a path (query / dataset rows) to read
data behind a hidden object type. Builds remain editor-gated (a build runs
trusted pipeline code); per-dataset build enforcement is future work.

**Row-level security & column masking** (`laurelin/core/permissions.py`,
`dataset_policies` table). Each dataset can carry a `DatasetPolicy`:
- *row policy* — a column plus per-subject rules; a non-admin sees a row only if
  a rule matches them AND the row's column value is in that rule's `values`. No
  matching rule ⇒ no rows (fail-closed; NULL column values are excluded).
- *column masks* — per column a mode (`null`/`redact`/`hash`) and exempt
  subjects; everyone else sees the value masked.
Admins are exempt. Enforcement is a single choke point,
`PermissionService.apply_table_policy(user, dataset, table)`, applied to the
same in-memory Arrow table by **all three** read paths — the row API (filter
then page, so counts reflect visible rows), the SQL page (each registered
dataset is filtered/masked, so aggregates respect RLS), and ontology object
materialization (objects are rows, so there is no read-around). Policy is
managed by admins via `/dataset-policies` + `/datasets/{name}/policy`.

#### Pipeline (transform) authoring (`laurelin/transforms/authoring.py`)

`PipelineFiles` reads/writes `pipelines/*.py`. **SECURITY:** writing a pipeline
file is code-execution-equivalent (it is `exec`'d on every build/collection).
Reads *and* writes/deletes are **editor** — `GET /pipelines` and
`GET /pipelines/{name}` were raised out of viewer by the audience rule, because
they return `exec`-ed Python that a viewer could not have authored; a viewer's
lineage need is served by `GET /transforms` and `GET /lineage`. The whole
Python surface is disabled by
`serve --lock-pipelines` / `LAURELIN_LOCK_PIPELINES=1` (flows have their own
`--lock-flows` / `LAURELIN_LOCK_FLOWS=1`; the boot probe `GET /auth/status`
reports both so the UI can say so up front). Writes validate syntax
(`compile`) before an atomic write and return the file's transforms plus any
cross-file `collect_error` (e.g. a duplicate output). Module names must match
`^[a-z][a-z0-9_]*$` (no paths/dots — no traversal). `from-query` wraps a
workbench SQL query as a `@sql_transform`, auto-detecting input datasets by
name. Executed transform code is **not** sandboxed (a roadmap item).

### `laurelin/ui` — single-page app

**React + TypeScript**, built with Vite (`laurelin/ui/webapp`) and emitted by
`vite-plugin-singlefile` as one self-contained `laurelin/ui/static/index.html`
— **zero external resources, no CDN**, which is why serving it needs no Node
toolchain. Dark, clean, information-dense. Fetch from `/api/v1/...` (same
origin) through `src/api.ts`.

*(This section described a vanilla-JS `index.html` + `app.css` + `app.js` app
until the React shell replaced it at feature parity — see the roadmap's Phase 1.
`static/` now holds the built bundle and nothing else.)*

Sidebar navigation (`src/Layout.tsx`, `NAV_GROUPS`), role-filtered and grouped
by job: **Data** (Datasets, Ontology) / **Analyze** (Dashboards, Analyses,
SQL, Apps) / **Build** (Pipelines (editor), with Visual and
Python tabs) / **Operate** (Builds, Schedules (editor), Health) / **Govern**
(Audit, Admin for admins, Workspaces for superadmins in multi-workspace mode).
A group whose every item is role-hidden disappears with its header. In
multi-workspace mode a switcher sits above the nav.

- Datasets: list w/ latest version + row counts; detail = schema table, version
  history, paged row preview, and the registration panels for federated /
  Iceberg / ClickHouse / StarRocks kinds.
- Builds (formerly "Pipeline"; /pipeline redirects): lineage graph (layered
  left-to-right SVG: dataset nodes as rounded rects, transform nodes as pills;
  simple longest-path layering), pipelines list, "Build now" button
  (POST /builds) + build history w/ per-task status and expectation results.
- Ontology: object types; per type a searchable object table; object detail
  panel with properties, linked objects, action forms (inputs per parameter,
  submit → POST apply, then refresh); object-store health and writeback.
- Dashboards: SVG chart grid (table / bar / line / area / stat / pie /
  scatter); a panel's rows come from
  `POST /dashboards/{name}/panels/{id}/run`, not from the panel's query.
- Analyses: one door from question to shared answer. The landing page's
  **quick chart** (formerly the standalone Explore screen; /explore
  redirects) is the point-and-click data-to-chart entry — source rail,
  shaping cards, live preview, save to dashboard, "keep going" into an
  analysis. Its shaping state survives a reload via sessionStorage; a saved
  flow panel's Edit reopens the exact state. Below it: the multi-cell
  documents (see the Analyses subsection).
- Admin: users, groups, dataset + ontology access, row & column security,
  markings and clearances, workspace files on disk, and Portability
  (export / import / governance fingerprint diff).
- Audit: recent events table.

**Every withheld value states that it is withheld and which role receives it.**
A blank is the one rendering that is not allowed: an operator reads "nothing
configured" from an empty field and retypes the credential.

### `laurelin/cli.py` — typer

`main()` = entry point that invokes the typer app.

```
laurelin init PATH [--name] [--description]
laurelin serve [--workspace PATH | --root DIR] [--host 127.0.0.1] [--port 8787]
               [--no-auth] [--secure-cookies] [--lock-pipelines] [--lock-flows]
               [--control-db URL]
laurelin build [TARGETS...] [--workspace PATH]
laurelin datasets list|show NAME [--workspace PATH]
laurelin upload NAME FILE [--workspace PATH]
laurelin demo [PATH=demo-workspace] [--build/--no-build]   # default: build
laurelin mcp --url URL --token TOKEN                       # stdio MCP server
laurelin export ARCHIVE|- [--fingerprint] [--metadata-only] [-w PATH]
laurelin import ARCHIVE|- [-w PATH] [--merge --confirm SHA] [--rename-prefix P]
laurelin verify-governance --baseline ARCHIVE [-w PATH]
laurelin users create|list|passwd|role|disable|enable|delete ...
laurelin tokens create|list|revoke ...
```

`--workspace` defaults to `Workspace.find()` discovery.

### `laurelin/demo.py`

`create_demo(path: Path, build: bool = True) -> Workspace` — deterministic
in-code aviation dataset (no downloads): `raw_aircraft` (~12 rows: tail_number,
model, operator, status, year_built), `raw_flights` (~60 rows: flight_id,
tail_number, origin, destination, scheduled_departure, delay_minutes, status —
include a few malformed rows for the cleaning step to drop). Writes
`pipelines/aviation.py` (clean_aircraft, clean_flights python transforms;
`flight_stats` sql_transform aggregating delay by aircraft) and
`ontology/aviation.yml` (aircraft + flight object types; `aircraft_flights`
link one_to_many on tail_number; actions: `update_aircraft_status` (update on
aircraft: status required), `cancel_flight` (update on flight: status+reason),
`add_aircraft` (create on aircraft)). Demo files are written as string
templates from demo.py.

## Testing

`tests/` uses pytest + fastapi TestClient (httpx). Each module ships unit
tests (`test_catalog.py`, `test_transforms.py`, `test_ontology.py`) using tmp_path
workspaces, plus `test_api.py` (end-to-end over the demo workspace: demo →
build → datasets/rows → lineage → ontology query/link → action → audit) and
`test_cli.py` (typer CliRunner: init/demo/build/datasets). Tests must not
depend on network and must pass with `.venv/bin/python -m pytest`.

## Design principles

1. **Everything is a file** — data (Parquet), metadata (SQLite), ontology (YAML), pipelines (Python).
2. **No hidden state** — every mutation lands in metadata.db (versions, builds, edits, audit).
3. **Open query surface** — REST + OpenAPI; DuckDB reads the same files users can read.
4. **Local-first** — single process, no services required; token auth only when exposed.
