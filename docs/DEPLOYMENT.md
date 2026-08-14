# Deploying Laurelin

Laurelin has two deployment shapes from one codebase.

## Embedded (single process)

Local-first, zero infrastructure. Everything is files on disk.

```bash
pip install -e ".[dev]"                            # from a checkout — see below
laurelin serve --workspace ./my-workspace          # single workspace
laurelin serve --root ./workspaces                 # many workspaces (SQLite control plane)
```

> **Laurelin is not on PyPI yet.** There is no git tag in the repository and
> nothing has been uploaded; `.github/workflows/release.yml` fires on a `v*`
> tag and is inert until trusted publishing is configured. So `pip install
> laurelin` does not work today — install from a checkout. This note comes out
> when the first release actually ships.

Back it up with `cp -r` (or `pg_dump` for a Postgres control plane). This is the
right shape for an analyst, a small team, or evaluation.

## Docker Compose (small team)

Laurelin + PostgreSQL, one command:

```bash
docker compose up --build
# open http://localhost:8787  ->  create the server administrator
```

See [`docker-compose.yml`](../docker-compose.yml). The API image ships the
prebuilt UI, so no Node toolchain is needed.

## Kubernetes / Helm (production)

A Helm chart is under [`deploy/helm/laurelin`](../deploy/helm/laurelin):

```bash
helm install laurelin deploy/helm/laurelin \
  --set controlDatabaseUrl='postgresql://laurelin:pw@postgres:5432/laurelin' \
  --set replicaCount=3 \
  --set persistence.accessMode=ReadWriteMany \
  --set ingress.enabled=true \
  --set-string 'ingress.hosts[0].host=laurelin.example.com' \
  --set secretEnv.LAURELIN_OIDC_CLIENT_SECRET=... \
  --set env.LAURELIN_OIDC_ISSUER=https://idp.example.com
```

The chart renders a Deployment (liveness `/health`, readiness `/health/ready`),
Service, optional Ingress + HPA, a PVC for workspace data, and a Secret for
sensitive env (SSO secrets, SCIM token). `helm lint` clean.

## Engines by role

Compute is four roles behind one governance layer (see
[SCALE.md](SCALE.md#four-roles-one-governance-layer)). Only the first is
required; the rest are optional extras you install when you need that role.

| Role | Extra | What must be running |
|---|---|---|
| Embedded analytical default | none — built in | Nothing |
| Iceberg storage | `laurelin[iceberg]` | Nothing. pyiceberg's `SqlCatalog` points at the database you already have — no REST catalog to operate |
| Federation (scan in place) | none for parquet/delta/iceberg/postgres | The foreign system (S3 bucket, Postgres, …) |
| Federation (delegated compute) | `laurelin[engines]` | A Flight SQL engine: Trino, Dremio, Databricks |
| Serving tier — **StarRocks** | `laurelin[starrocks]` | A StarRocks FE reachable on 9030, with an account holding `SELECT` and nothing else |
| Serving tier — ClickHouse | `laurelin[clickhouse]` | Nothing. chdb is ClickHouse embedded in the Laurelin process |
| Operational object store | none | The metadata database |

Two things an operator should know before planning around this section:

**The StarRocks object store is not selectable in this release.** The ontology
object store is pluggable in the sense that there is a seam with a second
implementation behind it — `StarRocksObjectStore`, writing via Stream Load with
primary-key upserts. There is no env var, route or config key that switches to
it; `OntologyService` constructs `MetadataObjectStore`. That implementation has
also **only ever run against an in-memory double, never a real StarRocks
server.** The metadata store is what runs, everywhere, today. (This is separate
from `kind="starrocks"` *datasets*, whose read path was measured against a real
StarRocks container — do not read one status as the other.)

**Licensing, for the `starrocks` extra.** `mysql-connector-python` is **GPLv2
with the FOSS exception**, and Laurelin is Apache-2.0. It is an *optional*
extra, imported only by the StarRocks backend, so nothing in a default install
links it — but anyone redistributing a build with this extra installed should
make that call deliberately. It was chosen because it is the only
MySQL-protocol client of the three candidates that genuinely *binds*
parameters; client-side interpolation would put this codebase's only injection
surface inside a third-party library, and on StarRocks that is a remote write
primitive rather than a wrong read.

## High availability — what holds, and what to know

**Stateless API.** The API process holds no per-request state; sessions and API
tokens live in the database. A replica that can't reach its store fails its
readiness probe and is pulled from rotation.

**Shared control plane.** Global identity + the workspace registry go in
PostgreSQL (`--control-db` / `LAURELIN_CONTROL_DATABASE_URL`) — a single shared,
HA-friendly store. Point Laurelin at a managed/HA Postgres and the identity tier
is HA.

**Per-workspace metadata in PostgreSQL.** On a Postgres control plane, each
workspace's metadata lives in its own **schema** (`ws_<slug>`) in the same
database. Every replica reads and writes the same store, and the database
handles the concurrency. This is what makes multi-replica safe — it replaced a
per-workspace SQLite file, which could not be shared across hosts (SQLite's WAL
needs shared memory, so a ReadWriteMany volume made concurrent writers
*dangerous* rather than safe).

> Earlier versions of this page and the Helm chart said multi-replica only
> needed a ReadWriteMany volume. That was wrong. **A Postgres control plane is
> now required for more than one replica** — with a SQLite control plane
> (embedded mode), workspaces still use their own files and you must run
> exactly one replica.

**Object storage for data.** Set `LAURELIN_DATA_URI` to `s3://bucket/prefix`
(or `gs://`, `abfs://`) and dataset Parquet lives there instead of on a volume.
Versions are manifests of immutable parts written to unique keys, and
registering the manifest row is the commit — no atomic directory rename is
required, so the protocol is native to object stores. With this set there is no
shared filesystem at all: **the data plane is stateless and replicas are
interchangeable.** Credentials come from the usual environment (instance
profile, workload identity, `AWS_*`).

**Builds are leased.** Any replica may accept "run a build"; exactly one
executes it. A worker claims the build with a single conditional `UPDATE` and
renews the lease as it goes; if the replica dies, the lease expires and the
build is reaped rather than sitting in `running` forever.

### Scaling out

```bash
helm install laurelin deploy/helm/laurelin \
  --set replicaCount=3 \
  --set controlDatabaseUrl='postgresql://laurelin:pw@postgres:5432/laurelin' \
  --set env.LAURELIN_DATA_URI='s3://my-bucket/laurelin' \
  --set persistence.enabled=false
```

With both a Postgres control plane and an object-store data URI, nothing is
node-local: scale the Deployment freely and let the HPA drive it.

**Behind TLS.** Keep `secureCookies: true` (the default) so session cookies get
the `Secure` flag; terminate TLS at the ingress. Set `X-Forwarded-Proto: https`
(most ingresses do) so cookie security and redirect URLs are correct.

**Observability.** `pip install 'laurelin[metrics]'` and scrape `/metrics`.
Beyond HTTP rate and latency, the metrics worth alerting on are the ones
covering work that happens without a user watching:

| Metric | Why it matters |
|---|---|
| `laurelin_query_rejections_total{reason}` | `timeout` / `memory` / `admission` — three different remedies |
| `laurelin_schedule_fires_total{status}` | A pipeline that silently stopped running |
| `laurelin_builds_total{status}` | Build failure rate |
| `laurelin_build_claims_total{outcome}` | Steady `lost` claims mean replicas are contending |
| `laurelin_builds_reaped_total` | A replica died mid-build |
| `laurelin_source_syncs_total{status}` | Ingestion broke upstream |
| `laurelin_queries_in_flight` | Approaching the admission limit |

Scraping requires credentials by default; set `LAURELIN_METRICS_PUBLIC=1` only
on a port users can't reach. Labels are deliberately low-cardinality — route
templates and statuses, never dataset names, workspace slugs or usernames.

Set `LAURELIN_LOG_FORMAT=json` for structured logs. Every response carries an
`X-Request-ID` (echoing the caller's if supplied), and it appears on every log
line for that request, so a trace can be followed across replicas.

**Backups.** Back up the Postgres control DB (`pg_dump`) and the workspace data
volume together. A point-in-time restore needs both from the same moment.

**Migrations.** The schema is created/upgraded additively on startup (idempotent
`CREATE TABLE IF NOT EXISTS` + additive `ALTER`s), so a rolling deploy is safe;
there is no separate migration step to run.

## Configuration reference (environment)

Engine-related variables are grouped by role rather than by vendor:
`LAURELIN_ICEBERG_*` configures **storage**; `LAURELIN_ENGINE_*` configures
**federation's** delegated-compute path; `LAURELIN_FEDERATION_WORKBENCH` gates
what federation *and* the **serving tier** expose to ad-hoc SQL; and the
`LAURELIN_QUERY_*` budgets apply to whichever engine answers, translated into
that engine's own settings. The **operational object store** has no variables —
it is the metadata database.

| Variable | Purpose |
|---|---|
| `LAURELIN_CONTROL_DATABASE_URL` | Postgres URL for the multi-workspace control plane |
| `LAURELIN_NO_AUTH=1` | Disable auth (local dev only) |
| `LAURELIN_LOCK_PIPELINES=1` | Disable in-browser transform authoring (untrusted tenants) |
| *(no env var)* — use the `--secure-cookies` flag | `Secure` flag on session cookies. There is deliberately no `LAURELIN_SECURE_COOKIES`; the flag is the only switch, and the Helm chart passes it. The flag is also implied per-request when `X-Forwarded-Proto: https` reaches the app |
| `LAURELIN_OIDC_ISSUER` / `_CLIENT_ID` / `_CLIENT_SECRET` / `_ROLE_MAP` | OIDC SSO |
| `LAURELIN_SAML_IDP_METADATA` / `_SP_ENTITY_ID` / `_ROLE_MAP` | SAML SSO (needs `xmlsec1`) |
| `LAURELIN_SCIM_TOKEN` | Enable SCIM provisioning (IdP bearer token) |
| `LAURELIN_MAX_UPLOAD_MB` | Upload size cap, also caps HTTP-connector downloads (default 1024) |
| `LAURELIN_BUILD_WORKERS` | Async build worker threads per replica (default 2) |
| `LAURELIN_DATA_URI` | Object store for dataset Parquet (`s3://`, `gs://`, `abfs://`). Unset = the workspace directory |
| `LAURELIN_DATABASE_URL` | Postgres for a *single*-workspace server's metadata (multi-workspace derives it from the control plane) |
| `LAURELIN_WORKER_ID` | Identifies this replica when claiming build leases (default `<hostname>:<pid>`) |
| `LAURELIN_ICEBERG_WAREHOUSE` | Where Iceberg table data lives (`s3://…`); default: `<workspace>/iceberg` |
| `LAURELIN_ICEBERG_CATALOG` | SQLAlchemy URL for the Iceberg catalog; default: the metadata database |
| `LAURELIN_FEDERATION_WORKBENCH=1` | Expose federated, ClickHouse **and StarRocks** datasets to ad-hoc SQL (off by default) |
| `LAURELIN_QUERY_MEMORY_LIMIT` | Memory budget per interactive query (default `2GB`; also becomes ClickHouse's `max_memory_usage` and StarRocks' `query_mem_limit`) |
| `LAURELIN_QUERY_TIMEOUT` | Seconds before an interactive query is interrupted (default `60`; `0` disables). DuckDB uses a watchdog thread; ClickHouse enforces it natively as `max_execution_time`; StarRocks as `query_timeout`, rounded **up** to a whole second because it rejects a fractional one |
| `LAURELIN_QUERY_THREADS` | Cap cores per interactive query (default: DuckDB's own) |
| `LAURELIN_MAX_CONCURRENT_QUERIES` | Interactive queries admitted at once per replica (default `8`; `0` disables) |
| `LAURELIN_BUILD_MEMORY_LIMIT` / `_TIMEOUT` / `_THREADS` | The same budget for builds (looser: default `4GB`, no timeout) |
| `LAURELIN_AUDIT_MAX_EVENTS` | Trim the audit log to N most recent events after each build (default `0` = unlimited) |
| `LAURELIN_AUTO_COMPACT_PARTS` | Compact a dataset once a version reaches N parts (default `0` = manual only) |
| `LAURELIN_EDIT_LOG_MAX_FOLDED` | After a writeback, prune the ontology edit log to N folded edits (default unset = never prune automatically). Pruning only ever deletes edits it can prove are redundant, and a failed prune never fails the fold |
| `LAURELIN_SCHEDULER=0` | Stop this replica running the scheduler (default on; leases make firing exactly-once, so every replica can) |
| `LAURELIN_METRICS=0` | Disable `/metrics` (default on when `laurelin[metrics]` is installed) |
| `LAURELIN_METRICS_PUBLIC=1` | Allow unauthenticated scraping — only when the port isn't reachable by users |
| `LAURELIN_STRICT_FILE_MODE=1` | Also strip *group* access when opening an inherited `metadata.db` / `control.db`. World access is always stripped; group access is kept by default because a group can be a set of principals you provisioned (a backup agent, an operator with read but not write) |
| `LAURELIN_LOG_FORMAT=json` | One JSON object per log line, with request id / workspace / actor |
| `LAURELIN_LOG_LEVEL` | Log level when JSON logging is on (default `INFO`) |
| `LAURELIN_SCHEDULER_POLL` | Seconds between scheduler polls (default `15`) |
| `LAURELIN_ENGINE_TIMEOUT` / `_MAX_ROWS` | Delegated-engine query timeout (default `300`s) and result-size cap (default `5000000`) |
