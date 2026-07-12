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

**Stateless API.** The API process holds no per-request state; sessions and API
tokens live in the database. Run N replicas behind the Service and scale
horizontally (the chart includes an optional HPA). A replica that can't reach
its store fails its readiness probe and is pulled from rotation.

**Shared control plane.** Global identity + the workspace registry go in
PostgreSQL (`--control-db` / `LAURELIN_CONTROL_DATABASE_URL`) — a single shared,
HA-friendly store. Point Laurelin at a managed/HA Postgres and the identity tier
is HA.

**The one caveat — per-workspace data.** Each workspace's Parquet + per-workspace
`metadata.db` lives on the mounted data volume, not (yet) in Postgres. So for
**more than one replica**, all replicas must share the same filesystem: use a
**ReadWriteMany** volume (NFS / EFS / CephFS). Object-storage-backed workspaces —
which would remove this requirement and give a fully stateless data plane — are
on the roadmap (WS1). Until then, treat the data volume as the one piece of
shared state that needs HA storage underneath it.

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
| `LAURELIN_MAX_UPLOAD_MB` | Upload size cap (default 1024) |
