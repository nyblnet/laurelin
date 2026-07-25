# Deploying Laurelin

Laurelin has two deployment shapes from one codebase.

## Embedded (single process)

Local-first, zero infrastructure. Everything is files on disk.

```bash
pip install laurelin
laurelin serve --workspace ./my-workspace          # single workspace
laurelin serve --root ./workspaces                 # many workspaces (SQLite control plane)
```

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

## High availability — what holds, and what to know

> **Correction (supersedes earlier guidance).** A previous version of this
> page said that more than one replica just needs a ReadWriteMany volume.
> **That is wrong and unsafe** — see "Run one replica" below. If you are
> running multiple replicas against a shared volume today, scale to one.

**Stateless API.** The API process holds no per-request state; sessions and API
tokens live in the database. A replica that can't reach its store fails its
readiness probe and is pulled from rotation. The *process* is stateless — but
see below for why that isn't yet enough to run several of them.

**Shared control plane.** Global identity + the workspace registry go in
PostgreSQL (`--control-db` / `LAURELIN_CONTROL_DATABASE_URL`) — a single shared,
HA-friendly store. Point Laurelin at a managed/HA Postgres and the identity tier
is HA.

**Run one replica.** Each workspace's metadata lives in a per-workspace SQLite
file on the data volume ([`context.py`](../laurelin/api/context.py)), opened in
WAL mode. **SQLite's WAL requires shared memory between the processes using the
database, which does not work across hosts on a network filesystem** — so on
NFS/EFS, WAL either fails to engage or, worse, concurrent pods can corrupt the
database. A ReadWriteMany volume is *necessary but not sufficient*: it makes the
bytes visible everywhere, and that is exactly what makes concurrent SQLite
writers dangerous.

So today: **one replica** (`replicaCount: 1`). Scale vertically; use Postgres
for the control plane so identity survives a restart; keep the data volume on
storage you'd trust with a database.

Note what is *not* the problem. Parquet data is immutable and write-once,
published by atomic rename — it is already safe on shared storage. Only the
mutable metadata database is not. Two changes lift the limit, both tracked on
the roadmap:

1. **Per-workspace metadata in PostgreSQL.** `MetadataStore` already speaks
   Postgres (it's the same dialect layer the control plane uses); workspaces
   use SQLite only because the path is currently hardcoded. This is the change
   that makes N replicas safe.
2. **Object-storage-backed workspaces**, which remove the shared-volume
   requirement entirely and give a fully stateless data plane.

**Builds are not coordinated across replicas.** Each process runs its own
worker pool with no shared queue or lease, so two replicas would happily build
the same target at once — duplicated work and confusing duplicate versions
(not corruption; version allocation is guarded by an atomic rename). A shared
build lease is part of the same work as (1).

**Behind TLS.** Keep `secureCookies: true` (the default) so session cookies get
the `Secure` flag; terminate TLS at the ingress. Set `X-Forwarded-Proto: https`
(most ingresses do) so cookie security and redirect URLs are correct.

**Backups.** Back up the Postgres control DB (`pg_dump`) and the workspace data
volume together. A point-in-time restore needs both from the same moment.

**Migrations.** The schema is created/upgraded additively on startup (idempotent
`CREATE TABLE IF NOT EXISTS` + additive `ALTER`s), so a rolling deploy is safe;
there is no separate migration step to run.

## Configuration reference (environment)

| Variable | Purpose |
|---|---|
| `LAURELIN_CONTROL_DATABASE_URL` | Postgres URL for the multi-workspace control plane |
| `LAURELIN_NO_AUTH=1` | Disable auth (local dev only) |
| `LAURELIN_LOCK_PIPELINES=1` | Disable in-browser transform authoring (untrusted tenants) |
| `LAURELIN_SECURE_COOKIES` via `--secure-cookies` | `Secure` flag on session cookies |
| `LAURELIN_OIDC_ISSUER` / `_CLIENT_ID` / `_CLIENT_SECRET` / `_ROLE_MAP` | OIDC SSO |
| `LAURELIN_SAML_IDP_METADATA` / `_SP_ENTITY_ID` / `_ROLE_MAP` | SAML SSO (needs `xmlsec1`) |
| `LAURELIN_SCIM_TOKEN` | Enable SCIM provisioning (IdP bearer token) |
| `LAURELIN_MAX_UPLOAD_MB` | Upload size cap, also caps HTTP-connector downloads (default 1024) |
| `LAURELIN_BUILD_WORKERS` | Async build worker threads per replica (default 2) |
