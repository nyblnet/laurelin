# Security policy

Laurelin is designed to hold data people care about — that's the whole premise
of the governance layer — so this document states plainly what we protect,
what we don't, and how to report a problem.

## Reporting a vulnerability

**Please do not open a public issue for a security bug.**

Use GitHub's private vulnerability reporting on this repository
(**Security → Report a vulnerability**), which opens a private advisory
visible only to maintainers.

Please include: what you found, how to reproduce it, the version or commit,
and what an attacker gains. A proof of concept helps enormously.

**What to expect**

| | |
|---|---|
| Acknowledgement | within 3 business days |
| Initial assessment (severity + plan) | within 10 business days |
| Fix for high/critical | prioritized over feature work |
| Credit | in the advisory and release notes, unless you'd rather not |

Laurelin is a young, volunteer-maintained project: we will be honest with you
about timelines rather than promise a 24-hour SLA we can't keep. If a report
is serious and we're slow, escalate by opening a public issue that says only
"privately reported security issue awaiting response" with no details.

We consider good-faith security research a contribution, not an attack. We
won't pursue action against researchers who follow this policy, act in good
faith, and avoid privacy violations, data destruction, or service disruption.

## Supported versions

Laurelin is pre-1.0 (`0.x`). **Only the latest release receives security
fixes.** There are no backports to older `0.x` releases. When 1.0 ships, this
section will state a real support window.

## The security model

Understanding the trust boundaries matters more than any individual control.

### What is enforced

**Authentication.** Local accounts (scrypt password hashing, per-user salt,
constant-time-ish verification that runs scrypt even for unknown usernames so
username enumeration isn't a timing oracle), OIDC, SAML 2.0, SCIM 2.0
provisioning, and API tokens. Sessions are opaque random tokens in `httpOnly`,
`SameSite=Lax` cookies — with the `Secure` flag when `--secure-cookies` is set.
Session and API tokens are stored only as SHA-256 hashes; a database dump does
not yield usable credentials. The `disabled` flag is checked on *every*
credential resolution, so disabling a user (including via SCIM deprovisioning)
takes effect on their next request — no waiting for a session to expire.

**Authorization**, composed in this order, evaluated on every read path:

```
role (viewer/editor/admin)
  └─ dataset / ontology ACL grants
       └─ mandatory classification markings (clearance for every effective marking)
            └─ row-level security  +  column masking
```

The composition is the point: object-type access requires the ontology grant
*and* backing-dataset access, so there is no path through the object API to
data you cannot read directly. Row and column policy is applied at a single
choke point shared by the rows API, SQL workbench, dashboard panels, ontology
materialization, and the MCP tools.

**Markings propagate through lineage.** A derived dataset inherits its inputs'
classifications on every build, so a pipeline cannot launder classified data
into an unmarked output.

**The SQL surface is sandboxed.** Workbench and dashboard queries run with
DuckDB external access disabled (`catalog.py`, `SET enable_external_access=false`),
over only the datasets the caller may view. `read_csv('/etc/passwd')`,
`COPY … TO`, path traversal, and attempts to widen the sandbox with `SET` are
all blocked, and there are regression tests for each
(`tests/test_query.py`, `tests/test_federation.py`). A dataset you can't see is
an *unknown table*, not a permission error — no existence oracle.

That is the sandbox around *caller-written* SQL, and it is not the same as a
filesystem sandbox around the whole process. **Registering a federated or
ClickHouse source whose path is local is a "read any file the server process
can read" capability**, because `federation.connect` disables
`LocalFileSystem` only for sources that are *not* local, and chdb has no
filesystem restriction to disable at all. It is therefore admin-only, and
exposing such datasets to ad-hoc SQL is additionally off by default behind
`LAURELIN_FEDERATION_WORKBENCH=1`. This is a property of the registration
route, not a hole in the workbench: callers receive an Arrow table, never a
connection, so only server-generated SQL reaches those engines.

**The `object_store` ingestion source is secret-bearing, and its endpoint is
admin-authored.** Registering one (`PUT /sources`, admin-only) stores an
`access_key_id`/`secret_access_key` pair for an S3/GCS bucket, redacted in
every API response and export by the same allowlist as postgres/http sources
(the key pair masks to `*****`; `uri`/`endpoint_url` disclose to admins only,
through `redact_dsn`; export withholds them whole so a re-imported source
refuses its first sync with a re-supply message rather than failing inside
DuckDB). The sync itself runs on a fresh, per-sync DuckDB connection that is the
*only* network-enabled connection in the process: it loads a temporary,
bucket-SCOPEd secret, then disables **both** `LocalFileSystem` and
`HTTPFileSystem` and locks the configuration. `s3://` reads use DuckDB's
S3FileSystem and are unaffected; disabling `HTTPFileSystem` stops that
connection reaching any other http(s) host (incl. cloud metadata at
169.254.169.254) as defense-in-depth behind `validate_source`, which already
forces the `uri` scheme to `s3://`/`gs://`. Build and query connections keep
`enable_external_access=false`, which blocks `s3://` even with a valid secret
loaded, and DuckDB secrets are per-instance and temporary, so no other path in
the process can use the sync's credential. The ingest is size-capped at
`LAURELIN_MAX_UPLOAD_MB` (the http puller's ceiling) so a triggered sync of a
huge object cannot mint an unbounded dataset.

There is an **SSRF surface, and it is bounded by who may author the endpoint,
not by who may trigger the sync.** The sync route's only gate is
`_require_dataset_edit`, so a plain editor, a viewer holding a `can_edit`
dataset grant, and the unattended scheduler can all *cause* a fetch to the
admin-configured `endpoint_url` — but none of them can *supply* it. This is the
same trust already extended to postgres/http sources: an admin can point the
server's sync path at an arbitrary URL. The residual, measured: a bucket-SCOPEd
secret is credential *selection*, not egress control, so an out-of-scope
`s3://` URL still resolves against public AWS — bounded because the `uri` is the
same admin's config and nothing an editor supplies ever reaches the connection.
Object-store ingestion is verified against MinIO; real AWS S3, GCS HMAC
interop, and Azure are not yet tested (Azure registration is rejected pending a
test).

**Secrets aren't echoed — and that no longer rests on recognising one.** Three
rounds of attackers found credential disclosures in the module that tried to
*detect* credentials in free text, and each round something walked around the
newest regex. Finding a credential inside a string is not decidable: libpq
conninfo, ODBC keyword strings, JDBC URLs, `CREATE SECRET` bodies, a driver's
prose, and formats nobody has enumerated are all valid places for a password.
Two rules replace the detector, and **nothing's confidentiality depends on it
any more**.

*R1 — third-party driver text is never persisted or returned.* Every place a
driver or library exception is caught converts it, at the catch site, into a
`Failure` (`laurelin/core/failure.py`): a code and a phase from closed enums, a
subject in Laurelin's own namespace, a `host:port` rebuilt from Laurelin's own
parse of its own config, integer counters, and a `detail_ref`. The only
driver-derived fields are the exception's class name and its vendor code, both
gated on the shape of a *Python identifier* — which no conninfo, ODBC string,
JDBC URL, `CREATE SECRET` body or PEM block can satisfy. That question is
decidable; "does this contain a credential" is not.

There is a net under the catch sites, because there has to be:
`@app.exception_handler(ValueError)` and `(KeyError)` catch anything uncaught,
and `pyarrow.lib.ArrowInvalid` **is** a `ValueError` while `ArrowKeyError` **is**
a `KeyError`. An editor read an operator's S3 warehouse credential out of a 400
that way. `failure.is_first_party` decides on the **deepest traceback frame** —
where the `raise` is written — rather than on the exception's type, and anything
else becomes a `Failure`. The route-level `except ValueError` blocks above that
net apply the same rule through `failure.safe_detail`: most of them are catching
Laurelin's own validation and echoing the caller's own input back, which is
fine, but several wrap a call that reaches a third-party library, and the check
costs nothing when the exception is ours.

*R2 — author-written free text is readable only at the privilege level that
could author it.* If you cannot write it, you cannot read it. Fields declare an
audience (`laurelin/core/audience.py`); anything not explicitly `PRESENTATION`
is withheld from readers below the record's authoring role, enforced at one
serialization point (`laurelin/core/serialize.py`). A field added tomorrow with
no annotation fails closed; a model added tomorrow is admin-only. A field whose
*writer* sits above its record says so with `AuthoredBy` — `DatasetInfo.source`
is written only by the three admin registration routes, so it reaches admin and
nobody else even though a dataset is editor-authored. Descending into a nested
record can only ever disclose less.

The three practical consequences:

- A connector's `config`, a federated dataset's `source` and an engine's URI are
  **not disclosed below admin at all**. Not a better denylist over somebody
  else's config vocabulary — the absence of one. What a lower-privileged reader
  gets is a Laurelin-built descriptor (which table, in which format) assembled
  from an allowlist of shape keys with an identifier-shaped gate on every value.
- A dashboard panel's SQL is not disclosed to a viewer. They get the **rows**:
  `POST /dashboards/{name}/panels/{id}/run` executes the stored panel
  server-side *as the caller*, with that caller's ACL, row-level security and
  column masking. A stored dashboard still grants nobody new read access.
- **Error paths are read paths.** A 400 that says a stored instruction is broken
  must not quote the instruction to a principal who may not read it. The
  message naming the offending field goes to whoever could have authored it;
  everyone else gets a code and a `detail_ref`. `HTTPException(detail=…)` does
  not pass through the serializer, so the routes that carry a failure call
  `serialize.detail_for` explicitly.

`laurelin/core/authoring_hints.py` still holds the old free-text matcher. It is
an **authoring hint and not a boundary**: saving a panel that looks like it
contains a connection string succeeds with a warning in the response body rather
than a 400. Being wrong costs an editor a banner instead of a viewer a password.
`tests/test_redaction.py::test_the_authoring_hint_is_not_load_bearing`
monkeypatches the matcher to always return "clean" and re-runs the entire leak
battery; it passes.

**What is still disclosed, deliberately.** An author's *captions* reach the
audience they were written for: a dashboard panel's `title`, and the `alias` on
a metric, which becomes the column header a viewer reads. An editor who puts a
credential in a column header has disclosed it to their own readers on purpose,
exactly as they would by typing it into the panel title. The invariant Laurelin
asserts and tests is about *instructions* — a panel's `sql`, `group_by`,
`filters`, `search`, and its metrics' `op` and `property` — not about labels.

**The server log is where driver text now lives, and that is a deployment
decision.** R1 *relocates* a driver's exact words from a browser-readable
database column to the process log, tagged with the same `detail_ref` the API
returns. Who can read that is **not bounded by any Laurelin role** — it is
whoever can read your log sink. If those logs are shipped to a SIEM whose
readership is wider than your admin group, you have widened the audience of
every credential your operators have pasted into a connector config. Treat the
server log as credential-bearing and give it the same protection as
`metadata.db`. A best-effort substitution of known secrets runs on the log path
as a courtesy for a sink Laurelin does not own; it is explicitly not a boundary.

**One data-destroying migration, on purpose.** `builds.error`,
`build_tasks.error`, `sources.last_sync_error` and `schedules.last_error` are
set to NULL on upgrade. Those columns hold prose of unknown provenance that can
never be re-classified, they are *known* to contain live credentials, and they
sit in a file whose permissions were themselves a shipped bug. Every
pre-existing `audit_log` row is stamped `min_read_role='admin'` for the same
reason: its writer had no idea who would read it.

**Mutations are audited** with actor, action, and details.

**Governance changes can require a second admin.** The consequential governance
writes — dataset/ontology grants, row policy, masks, markings and marking
deletion, clearances, group membership, role changes, workspace membership —
pass through a single store-level chokepoint that demands a `ChangeTicket`, so
REST, MCP (which calls the same routes), SCIM and flow governance all classify
under the same gate; a route decorator would miss the service callers. A
comparator decides *loosening* vs *tightening* by evaluating each non-admin
user's capability before and after the change — exact, because the policy
language is closed and declarative — and it classifies against an account's
**policy** capability even when the account is disabled, because the disabled
flag is reversible by an ungated identity write (a real bypass that is now
tested). Tightenings apply immediately; loosenings file a proposal. In
**second-approver mode** (opt-in, and enabling it requires ≥ 2 active admins) a
loosening queues and the approver must differ from the proposer. Enforcement is
uniform across paths: a **workspace import** carrying governance rules is
*refused* while second-approver mode is armed rather than write them below the
gate through its raw-SQL importer.

**Health and alert delivery leak nothing a viewer could not already read.** The
`GET /health/datasets` rollup and `GET /health/events` feed are filtered per
dataset (a viewer sees only datasets they can read), there is no unfiltered
totals endpoint, and the event feed's ordinal is renumbered per response so a
global counter cannot leak the existence, count or timing of transitions on
datasets the caller cannot see. An outbound webhook payload is mechanically the
*viewer-role* projection of the health record, so it cannot carry a masked
value, a row count, a `cursor_value` or editor prose; the link is a relative
path, never an absolute URL.

### What is *not* a boundary

Be clear-eyed about these. They are design consequences, not oversights:

1. **Pipelines are trusted code.** A transform is Python that the server
   imports and executes. **Anyone who can write a pipeline file has remote
   code execution as the server process.** In-browser authoring is
   editor-gated for this reason. On any deployment where editors are not
   fully trusted, serve with `--lock-pipelines` and manage pipeline files
   through git and code review. This is the single most important line in
   this document.

   Editors are trusted with code execution only where Python authoring is
   enabled. With `--lock-pipelines`, editors can still author **flows and
   Analyses charts** — no-code artifacts that compile to bound,
   schema-checked SQL, are governance-checked against their recorded author
   at every build, and cannot reach `exec` — while pipeline files are
   managed on disk. Ejecting a flow to Python writes a `.py`, so it is
   locked with Python. Operators who want the old total lockdown (no
   authoring of any kind) add `--lock-flows` / `LAURELIN_LOCK_FLOWS=1`.
   Dashboard raw-SQL panels are a separate, always-available surface: they
   are persisted authoring but execute as the calling viewer under their own
   row policies and masks, and were never covered by either lock.

   Where Python authoring *is* enabled, an API-authored transform can no
   longer launder data: the saving user is recorded server-side, and every
   build refuses a python/sql transform whose recorded author cannot read
   every input in full — view rights, and no row policy or column mask *that
   applies to that author* — checked against the recorded author, never
   whoever triggered the build. "Applies to" is resolved by the same engine a
   read uses, so an admin (who bypasses policy) and an author exempt from a
   mask build, while an author the policy would filter is refused; that is
   what keeps a legitimate re-save from becoming a false refusal. **The
   surviving trust assumption, plainly: pipeline files written on disk — git,
   import, the CLI — have no recorded author and build unchecked**, as does a
   `--no-auth` server (every request is the implicit admin, not a real user,
   so nothing accountable is recorded). Whoever can write to `pipelines/` on
   disk already has code execution as the server, so the entitlement check
   binds exactly the population the API can identify and no one else. It does
   not touch the code-execution surface itself — a pipeline function body runs
   as the server and can read any dataset on disk regardless of its declared
   inputs; that is what `--lock-pipelines` (item 1 above) closes.

2. **Admins bypass markings and row policy.** Deliberate: a mandatory-access
   system that can lock every human out of their own workspace is an
   availability incident waiting to happen. If you need admins constrained,
   separate duties across workspaces — don't assume the marking stops them.

3. **`--no-auth` disables everything.** Every request becomes an
   administrator. It exists for local development and the demo. Never expose
   a `--no-auth` server to a network you don't control.

4. **Dataset files are not protected from the OS.** Anyone with read access to
   the workspace directory reads the Parquet under `data/` directly, with no
   row policy and no column masks — access control is enforced at the API, not
   the filesystem. Protect the volume accordingly. The *credential*-bearing
   files are a separate matter and are handled: `metadata.db` (with its
   `-wal`/`-shm` siblings), `control.db`, `iceberg-catalog.db` and
   `laurelin.yml` are created `0600`, and a workspace root Laurelin creates is
   `0700`, as are the `data/`, `pipelines/`, `ontology/` and `iceberg/`
   directories inside it. See the deployment checklist below for what happens
   to a workspace created before that.

   `LAURELIN_DATA_URI` pointing at a local path creates that directory `0700`
   too; pointing it at a scheme this build cannot address (`s3a://`, a
   mistyped `s3:/`) is a **startup error**, because the alternative was a
   local directory of that literal name holding every governed Parquet while
   the operator believed the data was in a bucket.

   `pipelines/` matters more than the other two: its contents are `exec`'d on
   every build, so a directory anyone can write to is code execution. Laurelin
   creates it `0700`; a directory that already exists keeps the mode it has, and
   **Admin → Workspace files on disk** shows the workspace root's actual mode.

5. **No encryption at rest.** Use encrypted volumes or an encrypted Postgres.

6. **Multi-tenancy is soft.** Workspaces isolate data and membership, but they
   share a process. Treat the tenant boundary as an organizational one, not a
   hostile-tenant sandbox.

7. **Change approval governs the network surface, not the filesystem.** The
   approval gate binds REST, MCP, SCIM, the CLI and flow governance because
   they all pass through the ticketed store methods — but a local process that
   opens `metadata.db` directly (the CLI does; the file is `0600`) writes with
   a `local` ticket and no queue. Approvals are the honest-operator record and
   the network control, not a defense against a hostile operator with
   filesystem access. Two composition points are exempt by explicit decision
   and file an after-the-fact record rather than queueing: an IdP **SCIM** group
   push (configuring `LAURELIN_SCIM_TOKEN` is the standing authorization —
   queueing would break push semantics), and `--no-auth` mode (every request is
   the implicit admin). Both are stated in `laurelin/core/approvals.py`.

8. **An alert webhook is an outbound surface an admin controls.** The URL is
   admin-configured and off by default; there is **no egress allowlist**, so an
   admin can point a webhook at an internal address (a metadata endpoint, an
   internal service) and the server will POST there. This stays within the
   admin trust boundary — there is no non-admin path to configure a webhook or
   induce a delivery to an attacker-chosen URL — but if your admins are not
   trusted with outbound requests from the server's network position, do not
   enable webhooks. The URL is stored as a write-only credential (read back as
   `WITHHELD`, omitted from export archives), so it is not disclosed to a later
   admin or an exported workspace; its *destination* is still the configuring
   admin's choice.

## Deploying safely

A short checklist; details in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

- [ ] Never run `--no-auth` outside localhost.
- [ ] Terminate TLS at the ingress; run with `--secure-cookies`; ensure
      `X-Forwarded-Proto` reaches the app.
- [ ] Set `--lock-pipelines` unless every editor is trusted with code
      execution. Editors keep Flows and Analyses charts — no-code authoring that
      cannot reach `exec` — which is the intended production posture; add
      `--lock-flows` only if you want no authoring surface at all.
- [ ] Use PostgreSQL for the control plane in any multi-replica deployment,
      and back it up together with the data volume.
- [ ] Give the server process its own OS user. Laurelin creates `metadata.db`
      (with its `-wal`/`-shm` siblings), `control.db` and `laurelin.yml` at
      `0600`, and a workspace or server root it creates at `0700` — but a
      workspace from before that, or a directory you made yourself, keeps the
      mode it has. Opening an inherited database strips world access **and
      group write**, and leaves group *read* alone — so **an upgraded
      workspace ends up `0640`, not `0600`**, and that is the default outcome
      of every upgrade, not an edge case. Group write is not treated as a
      share: `metadata.db` is the file that says who is an admin, and at
      `umask 002` a legacy `0664` database let any member of the group make
      themselves one with `sqlite3`. **Admin → Workspace files on disk** shows
      the mode each file actually has, re-stat'd on every request, including
      `control.db` and `iceberg-catalog.db`, and distinguishes group *write*
      from group read. `LAURELIN_STRICT_FILE_MODE=1` forces `0600` on every
      open, and also takes `laurelin.yml` and the workspace directory itself.
      Data under `data/` is not re-moded at all.
- [ ] Set `LAURELIN_MAX_UPLOAD_MB` to something sane for your box (it also
      caps HTTP-connector downloads).
- [ ] Rotate `LAURELIN_SCIM_TOKEN` and SSO client secrets like any other
      credential; keep them in a Secret, not in `values.yaml`.
- [ ] Treat a workspace archive as untrusted input. `laurelin import` is not
      signature-checked (see [docs/PORTABILITY.md](docs/PORTABILITY.md)): the
      trailer proves the archive is internally consistent, not that it came
      from anyone in particular. An archive whose data parts collide with the
      destination's is now relocated to fresh keys rather than written over
      them, and the relocation is reported in the import warnings — but the
      *rows* it brings are still whatever the archive said.
- [ ] Review the audit log periodically — it only helps if someone reads it.
- [ ] **Treat the server log as an operator-privilege artifact, and check who
      your log sink is shared with.** Laurelin no longer persists third-party
      driver text anywhere a browser can reach it: an exception from psycopg,
      mysql-connector, DuckDB or a Flight SQL driver is converted at the catch
      site into a structured `Failure` (an error code, a phase, a subject in
      Laurelin's own namespace, our own `host:port`, integer counters) and the
      driver's own sentence goes to the process log, tagged with the same
      `detail_ref` the stored failure carries.

      That is a *relocation*, not a sanitization, and it is the deliberate
      trade: an operator has to be able to read what actually broke. A driver
      quotes back the connection string it was handed, so those log lines can
      contain any credential an operator has pasted into a connector config.
      **If your logs are shipped to a SIEM whose readership is wider than your
      admin group, you have widened the audience of every one of those
      credentials.** Laurelin makes a best-effort pass to substitute out the
      secrets it can name from the config in hand; that is a courtesy for a
      sink Laurelin does not own, not a control — see
      `laurelin/core/authoring_hints.py`.

## Scope

**In scope:** authentication and session handling, the authorization layers
above, the SQL sandbox, secret exposure, injection, path traversal, CSRF,
privilege escalation between roles/workspaces, and marking/lineage bypass.

**Out of scope:** anything requiring `--no-auth`; attacks by a user who
already has pipeline-write access (see boundary 1); social engineering; and
vulnerabilities in dependencies without a demonstrated exploit path through
Laurelin.

**Resource exhaustion is partly in scope, and here is exactly how far it
goes.** Interactive queries *are* budgeted — `laurelin/core/limits.py` applies
a memory limit, a wall-clock timeout enforced by a watchdog that calls
DuckDB's `interrupt()`, and a per-replica concurrency semaphore that refuses
past its bound with `Retry-After` rather than queueing. All three are tunable
(`LAURELIN_QUERY_MEMORY_LIMIT`, `LAURELIN_QUERY_TIMEOUT`,
`LAURELIN_MAX_CONCURRENT_QUERIES`) and are translated into ClickHouse's and
StarRocks' own settings for those engines. So one expensive query fails its
own request rather than the replica. What is **not** bounded: builds get a
looser budget and **no timeout by default** (`LAURELIN_BUILD_TIMEOUT=0`), and
there is **no general request rate limiter** — the only throttle in the tree is
on failed logins (5 consecutive failures per username → 30s lockout, 429), so a
caller who issues many *cheap* authenticated requests is unbounded. Report a
way to take a replica down *through* the query budget; a report that says
"unbounded builds exist" is describing a documented default. Ingestion pulls
(`http` and `object_store` sources) cap the pulled size at
`LAURELIN_MAX_UPLOAD_MB` (default 1024 MB) so a single sync cannot mint an
arbitrarily large managed dataset, but the *number* of syncs is not rate-limited
beyond the edit gate above.
