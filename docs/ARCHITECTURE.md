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
```

`build()` behavior: `store.create_build`, mark running with `started_at`; for each
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
POST /api/v1/datasets/{name}/upload           multipart file (.csv/.parquet) -> DatasetVersionInfo
GET  /api/v1/lineage                          -> {"nodes":[{id,type:"dataset"|"transform"}],"edges":[{from,to}]}
                                                 (dataset->transform->dataset graph derived from lineage_edges)
GET  /api/v1/transforms                       -> [{name, output, inputs:[dataset], kind}]
POST /api/v1/builds                           {targets?: [str]} -> BuildInfo (synchronous)
GET  /api/v1/builds                           -> [BuildInfo]
GET  /api/v1/builds/{id}                      -> BuildInfo
GET  /api/v1/ontology/object-types            -> [ObjectTypeDef]
GET  /api/v1/ontology/object-types/{name}     -> ObjectTypeDef + links + actions for it
GET  /api/v1/ontology/objects/{type}?search=&limit=&offset=&filter.<prop>=<val>
                                              -> {"objects":[...],"total":N}
GET  /api/v1/ontology/objects/{type}/{pk}     -> object dict (404 if absent)
GET  /api/v1/ontology/objects/{type}/{pk}/links/{link} -> {"objects":[...]}
GET  /api/v1/ontology/actions                 -> [ActionDef]
POST /api/v1/ontology/actions/{name}/apply    {pk?: str, parameters: {...}} -> ObjectEdit
GET  /api/v1/audit?limit=                     -> [AuditEvent]
```

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
