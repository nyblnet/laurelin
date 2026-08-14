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

### What is *not* a boundary

Be clear-eyed about these. They are design consequences, not oversights:

1. **Pipelines are trusted code.** A transform is Python that the server
   imports and executes. **Anyone who can write a pipeline file has remote
   code execution as the server process.** In-browser authoring is
   editor-gated for this reason. On any deployment where editors are not
   fully trusted, serve with `--lock-pipelines` and manage pipelines through
   git and code review. This is the single most important line in this
   document.

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

## Deploying safely

A short checklist; details in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

- [ ] Never run `--no-auth` outside localhost.
- [ ] Terminate TLS at the ingress; run with `--secure-cookies`; ensure
      `X-Forwarded-Proto` reaches the app.
- [ ] Set `--lock-pipelines` unless every editor is trusted with code
      execution.
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
"unbounded builds exist" is describing a documented default.
