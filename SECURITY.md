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
DuckDB external access disabled, over only the datasets the caller may view.
`read_csv('/etc/passwd')`, `COPY … TO`, path traversal, and attempts to widen
the sandbox with `SET` are all blocked, and there are regression tests for
each. A dataset you can't see is an *unknown table*, not a permission error —
no existence oracle.

**Secrets aren't echoed.** Connector configs, federated dataset sources and
engine URIs go through one redactor (`laurelin/core/redaction.py`) in every API
response; API tokens are shown exactly once, at creation. The rule is *shape
first*: a `scheme://user:password@host` DSN has its credential masked, and any
value whose shape cannot be read — an ODBC keyword string, a URL carrying a
query, a driver's option namespace, a nested config object — is withheld whole
rather than guessed at. Three redactors with three regexes preceded this and
leaked ten different credential forms between them; guessing where a secret
sits is what failed, so the replacement does not guess.

What a masked DSN still shows is the endpoint: `user@host:port/db`. Whether
that belongs in a viewer-readable response is an open question we have not
answered, and it is stated in the redactor's docstring alongside the rest of the
residual (a secret in a URL *path*, or pasted into a free-form value, is not
detectable and is not removed). The workspace export answers it the strict way
and withholds the endpoint too — see docs/PORTABILITY.md.

**A third party's error text is treated as credential-bearing.** A driver
quotes back the connection string it was handed, in prose with no shape a
redactor can find — a password containing a space came back from psycopg as
``unexpected spaces found in "SUPER SEKRET"``, and that string was stored on the
source row and served. So a failed sync, a failed federated-source probe and a
failed engine test all have the credentials the config says we issued
*substituted out by value* first, then the shape rules applied, then the whole
message withheld if a known secret survived both.

One consequence to plan for: whenever a message is redacted or withheld from the
browser, the unredacted exception is written to the **server log** so the
operator can still diagnose it. Treat server logs as credential-bearing and give
them the same protection as `metadata.db`.

**Free-form fields that are executed are gated on write, not on read.** A
dashboard panel's SQL and a schedule's targets are stored and returned verbatim,
and a panel's SQL round-trips through the editor's textarea — redacting on read
would let a mask be saved over the real query. A value embedding a
`user:password@host` connection string is therefore refused at `PUT` with a
message pointing at registered sources. Values stored before this are not
retroactively redacted; `laurelin export` scans for them and refuses.

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
   `-wal`/`-shm` siblings), `control.db` and `laurelin.yml` are created `0600`,
   and a workspace root Laurelin creates is `0700`, as are the `data/`,
   `pipelines/` and `ontology/` directories inside it. See the deployment
   checklist below for what happens to a workspace created before that.

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
      mode it has. Opening an inherited database strips world access and
      leaves group access alone — so **an upgraded workspace ends up `0640`,
      not `0600`**, and that is the default outcome of every upgrade, not an
      edge case. **Admin → Workspace files on disk** shows the mode each file
      actually has, re-stat'd on every request and including `control.db`, and
      `LAURELIN_STRICT_FILE_MODE=1` forces `0600` on every open. Data under
      `data/` is not re-moded at all.
- [ ] Set `LAURELIN_MAX_UPLOAD_MB` to something sane for your box (it also
      caps HTTP-connector downloads).
- [ ] Rotate `LAURELIN_SCIM_TOKEN` and SSO client secrets like any other
      credential; keep them in a Secret, not in `values.yaml`.
- [ ] Review the audit log periodically — it only helps if someone reads it.

## Scope

**In scope:** authentication and session handling, the authorization layers
above, the SQL sandbox, secret exposure, injection, path traversal, CSRF,
privilege escalation between roles/workspaces, and marking/lineage bypass.

**Out of scope:** anything requiring `--no-auth`; attacks by a user who
already has pipeline-write access (see boundary 1); denial of service via
deliberately expensive queries (the query engine is not resource-limited yet —
a known gap, tracked on the roadmap); social engineering; and vulnerabilities
in dependencies without a demonstrated exploit path through Laurelin.
