# Laurelin architecture

Laurelin is a local-first, ontology-driven data platform. One **workspace
directory** holds all state: Parquet data, a SQLite metadata store, Python
pipelines, YAML ontology. Modules communicate through the models in
`laurelin/core/models.py` — that file is the source of truth for all types.

```
laurelin/
├── core/        models.py, config.py (Workspace), db.py (MetadataStore)   [DONE]
├── catalog/     versioned Parquet dataset storage + DuckDB access
├── transforms/  @transform / @sql_transform, DAG builder, lineage
├── ontology/    YAML loader, object queries, links, actions, edit overlay
├── api/         FastAPI app: REST + static UI mount
├── ui/static/   vanilla-JS single-page UI (no CDN, self-contained)
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
    def read(self, name: str, version: int | None = None) -> pa.Table
    def parquet_glob(self, name: str, version: int | None = None) -> str
        # absolute glob to the version's parquet files, for duckdb read_parquet()
    def rows(self, name: str, limit: int = 100, offset: int = 0,
             version: int | None = None) -> list[dict]      # via duckdb, JSON-safe values
    def upload_file(self, name: str, path: Path, description: str = "") -> DatasetVersionInfo
        # CSV (duckdb read_csv_auto) or Parquet by extension
```

Rules:
- Dataset names: `^[a-z][a-z0-9_]*$`, validate on create/write. Raise `ValueError` otherwise.
- `write()` is atomic-ish: write parquet to a temp dir inside `data/`, then
  `os.rename` to `data/<name>/v{version:04d}/`; only then `store.add_version(...)`.
  Auto-creates the dataset row if missing.
- Versions are immutable; `read` with no version = latest. Missing dataset/version
  raises `KeyError` (api layer maps to 404).
- `rows()` must convert non-JSON-safe values (timestamps, bytes, Decimal, NaN) to strings/None.
- Schema captured as `ColumnSchema(name, type=str(arrow_type))`.

### `laurelin/transforms`

`laurelin/transforms/__init__.py` re-exports: `transform, sql_transform, Input, Output, TransformRegistry, Builder, collect_transforms`.

```python
@dataclass
class Input:  dataset: str
@dataclass
class Output: dataset: str; description: str = ""

@dataclass
class TransformSpec:
    name: str                    # function name
    output: Output
    inputs: dict[str, Input]     # param name -> Input
    kind: str                    # "python" | "sql"
    fn: Callable | None          # python transforms: fn(**{param: pa.Table}) -> pa.Table
    query: str | None            # sql transforms: SELECT over input aliases as table names

def transform(output: Output, **inputs: Input)   # decorator for python transforms
def sql_transform(output: Output, inputs: dict[str, Input], query: str)  # decorator (fn body ignored)

class TransformRegistry:
    def register(self, spec) / get(self, name) / all(self) -> list[TransformSpec]
    def by_output(self, dataset: str) -> TransformSpec | None

def collect_transforms(pipelines_dir: Path) -> TransformRegistry
    # exec each *.py in pipelines_dir (sorted); decorators register into the
    # *active* registry via a module-level context (use a contextvar or module
    # global set/reset around exec). Files import `from laurelin.transforms import ...`.

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
        # {"objects": [ {props..., "__pk": str, "__title": str} ], "total": int}
    def get(self, type_name: str, pk: str) -> dict | None
    def linked(self, type_name: str, pk: str, link_name: str) -> list[dict]
        # follows link in either direction (link where from==type or to==type)
    def apply_action(self, action_name: str, pk: str | None,
                     parameters: dict, actor: str = "anonymous") -> ObjectEdit
    def edits(self, type_name: str) -> list[ObjectEdit]
```

Object materialization = **base + overlay**: base rows from the backing
dataset's latest version (duckdb over parquet; if the dataset has no versions
yet, base = empty); then apply `store.list_object_edits(type)` in order:
`create` adds a row (payload must include primary key), `update` shallow-merges
payload into the row with that pk, `delete` removes it. Everything is computed
in memory per request (fine at this scale). `search` = case-insensitive
substring over string-typed properties; `filters` = equality on property values
(compare as strings). pk values compared as `str(value)`.

`apply_action` validates: action exists; for update/delete pk must reference an
existing object; required parameters present; parameters must be declared;
update/create payload keys must be declared properties of the object type
(reject unknown, except pk on create). Coerce parameter values per declared type
(integer/float/boolean). Writes `ObjectEdit` + audit log. Raise `ValueError`
with a clear message on any validation failure (api maps to 400/404).

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
`dataset_permission` (row API, SQL workbench, ontology objects). Admin API:
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

**RBAC.** viewer: all GETs. editor: viewer + POST datasets / upload / builds /
action apply. admin: editor + user & token management. Enforced via a
`require_role(...)` dependency; violations → 403 `{"detail": ...}`.

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
GET  /api/v1/workspace                        -> {name, description, root}
GET  /api/v1/datasets                         -> [DatasetInfo]
POST /api/v1/datasets                         {name, description?} -> DatasetInfo
GET  /api/v1/datasets/{name}                  -> DatasetInfo + versions: [DatasetVersionInfo]
GET  /api/v1/datasets/{name}/schema?version=  -> [ColumnSchema]
GET  /api/v1/datasets/{name}/rows?limit=&offset=&version= -> {"rows":[...],"row_count":N}
POST /api/v1/query        {sql, max_rows?} -> {columns,rows,row_count,truncated}
                                 (read-only DuckDB; each dataset is a view; viewer+)
POST /api/v1/datasets/{name}/upload           multipart file (.csv/.parquet) -> DatasetVersionInfo
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
PUT  /api/v1/dashboards/{name}                {title?, description?, panels} -> DashboardInfo (editor)
                                 (panels = saved SQL + chart config; the client
                                  runs each panel through POST /query, so every
                                  viewer sees their own filtered data)
DELETE /api/v1/dashboards/{name}              -> {deleted}  (editor)
GET  /api/v1/sources                          -> [SourceInfo]  (editor; secrets redacted)
GET  /api/v1/sources/{name}                   -> SourceInfo    (editor; secrets redacted)
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
GET  /api/v1/ontology/objects/{type}?search=&limit=&offset=&filter.<prop>=<val>
                                              -> {"objects":[...],"total":N}  (403 if not viewable)
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
GET  /api/v1/pipelines                        -> [{name, transforms:[str], error, bytes}]  (viewer)
GET  /api/v1/pipelines/{name}                 -> {name, content}  (viewer)
PUT  /api/v1/pipelines/{name}                 {content} -> {name,transforms,collect_error}  (editor)
DELETE /api/v1/pipelines/{name}               -> {ok:true}  (editor)
POST /api/v1/pipelines/from-query             {sql, output, name?} -> {name,...}  (editor)
GET  /api/v1/audit?limit=                     -> [AuditEvent]
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
the caller can view, so a blocked dataset is simply an unknown table. Datasets
that need no policy for the caller are registered as **lazy Arrow datasets**
(DuckDB streams them with projection/filter pushdown, so workbench memory
scales with the result, not the dataset); policy'd datasets are read and
filtered through the same choke point as the row API. External access stays
disabled either way — the SQL can never touch the filesystem. Object-type
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
then page, so counts reflect visible rows), the SQL workbench (each registered
dataset is filtered/masked, so aggregates respect RLS), and ontology object
materialization (objects are rows, so there is no read-around). Policy is
managed by admins via `/dataset-policies` + `/datasets/{name}/policy`.

#### Pipeline (transform) authoring (`laurelin/transforms/authoring.py`)

`PipelineFiles` reads/writes `pipelines/*.py`. **SECURITY:** writing a pipeline
file is code-execution-equivalent (it is `exec`'d on every build/collection).
Reads are viewer; writes/deletes are editor; the whole surface is disabled by
`serve --lock-pipelines` / `LAURELIN_LOCK_PIPELINES=1`. Writes validate syntax
(`compile`) before an atomic write and return the file's transforms plus any
cross-file `collect_error` (e.g. a duplicate output). Module names must match
`^[a-z][a-z0-9_]*$` (no paths/dots — no traversal). `from-query` wraps a
workbench SQL query as a `@sql_transform`, auto-detecting input datasets by
name. Executed transform code is **not** sandboxed (a roadmap item).

### `laurelin/ui/static` — single-page app

`index.html` + `app.css` + `app.js`, **zero external resources**. Dark, clean,
information-dense. Sidebar navigation: **Datasets / Pipeline / Ontology /
Audit**. Fetch from `/api/v1/...` (same origin).
- Datasets: list w/ latest version + row counts; detail = schema table, version
  history, paged row preview.
- Pipeline: lineage graph (layered left-to-right SVG: dataset nodes as rounded
  rects, transform nodes as pills; simple longest-path layering), transforms
  list, "Run build" button (POST /builds) + build history w/ per-task status.
- Ontology: object types; per type a searchable object table; object detail
  panel with properties, linked objects, action forms (inputs per parameter,
  submit → POST apply, then refresh).
- Audit: recent events table.

### `laurelin/cli.py` — typer

`main()` = entry point that invokes the typer app.

```
laurelin init PATH [--name] [--description]
laurelin serve [--workspace PATH] [--host 127.0.0.1] [--port 8787]
laurelin build [TARGETS...] [--workspace PATH]
laurelin datasets list|show NAME [--workspace PATH]
laurelin upload NAME FILE [--workspace PATH]
laurelin demo [PATH=demo-workspace] [--build/--no-build]   # default: build
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
