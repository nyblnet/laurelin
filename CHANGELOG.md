# Changelog

Notable changes to Laurelin. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[semantic versioning](https://semver.org/), with the caveat that pre-1.0
minor releases may break things.

## Unreleased

## Security

Laurelin is a governance product, so the entries below are the ones that matter
most: each was a live disclosure or a live bypass on a running server, each was
reproduced before it was fixed, and each fix was reverted and watched to fail
before being restored.

### Fixed: eighteen defects from a third adversarial pass

The two rounds below were attacked again by agents who had written none of the
code. Every entry here was reproduced on a running server first, and every fix
was reverted and watched to fail its own regression test before being restored.

**Credential disclosure (`laurelin/core/redaction.py` and its callers).**

- **A scheduled sync put the password on the VIEWER-gated audit route.**
  `connectors.sync_source` redacts, records the redacted copy, and then
  re-raises the *original* exception; `Scheduler._run` caught that and wrote
  `str(exc)` into `schedules.last_error` and into an audit row. So the leak the
  connector documents as fixed was fixed on the `POST /sources/{n}/sync` path
  only: through a schedule, a plain viewer — 403 on `/sources`, 403 on
  `/schedules` — read `unexpected spaces found in "SUPER SEKRET"` out of
  `GET /audit`. The `/audit` backstop cannot catch it: that sentence has no
  `://` and no credential *word* in it. The scheduler now redacts at the point
  of record, loading the source's own config to do it.
- **The credential gate was shape-only, and a credential is not always a URL.**
  `credential_in_free_text` was `value != redact_value(value)`, and
  `redact_value` only acts on strings containing `://`. A libpq conninfo in a
  dashboard panel's `ATTACH` — which DuckDB's postgres and mysql extensions
  accept verbatim — an ODBC keyword string, and a DuckDB `CREATE SECRET` all
  passed the gate, and a plain VIEWER read the password from `GET /dashboards`.
  Same hole on schedule targets. There is now a `keyword_credential` test:
  a password keyword *beside* a connection keyword, which is narrow enough not
  to reject `WHERE password = :p`.
- **The same gate refused legitimate URLs with a factually false 400.**
  `redact_dsn` withholds a URL carrying a query, and one whose `@` falls after
  a `/`, because those are *ambiguous* — not because a credential was found. A
  gate built on "did anything change" could not tell the two apart, so a public
  CSV with `?format=csv` and an S3 prefix keyed by an email address were both
  refused as embedding a credential, with no override and no way to author the
  panel at all. The gate now asks its own question, and
  `postgresql://alice:pa/ss@db/prod` is still refused: its first segment is not
  a hostname.
- **`MASK` shipped most of a password containing `@`.**
  `_free_form_is_truncated` skipped any match that already contained an `@` —
  true only if that `@` is the last one. `postgres://alice:p@ss word@db/prod`
  matched only as far as `p@ss`, and the result masked to the `@` *inside the
  fragment*, shipping the rest of the password under a `*****` that asserts the
  credential was removed. Reproduced on every terminator in the regex class,
  through `POST /engines/{n}/test` (whose body also said `"withheld": false`)
  and through a source's `query`. A message naming two DSNs still has both
  masked rather than being withheld whole.
- **A schedule's `source` and `upstream_dataset` had no gate at all.** The loop
  ran over `targets` and stopped there; the exact DSN it refuses was accepted
  one field over and returned verbatim by `GET /schedules`.
- **A keyword credential under an ordinary key reached a VIEWER.** `path`,
  `query` and `connection` are not URL-shaped, so `redact_mapping` disclosed
  `Driver={x};Server=db;Uid=a;Pwd=…` to every editor of `GET /sources` — and,
  through `federation.redacted_source`, to every **viewer** of `GET /datasets`.
  The module docstring conceded the first case and not the second; both are
  now withheld.

**File permissions (`laurelin/core/fileperms.py` and the paths around it).**

- **`harden_existing` preserved group WRITE, and every surface called the
  result "readable by its group".** Every sentence of the argument for keeping
  group access is about *read* — a backup agent, an on-call operator without
  write. `metadata.db` is the file that says who is an admin. At `umask 002`
  (the Debian/Ubuntu login default, and what a systemd unit with `UMask=0002`
  sets) a pre-`fileperms` release left it 0664, the repair took it to 0660, and
  a member of that group — not a Laurelin user at all — opened it with
  `sqlite3`, ran `update users set role='admin'`, and reached the ADMIN-only
  routes. Group write is now stripped like world access; group *read* still
  survives, and `describe()`, the note and the UI badge now say which of the
  two they are looking at.
- **The Iceberg warehouse was never mode-protected.** `data/`, `pipelines/` and
  `ontology/` are created 0700 because the workspace root is only 0700 when
  Laurelin made it — and the documented Docker shape leaves it 0755.
  `iceberg/` arrived later and never joined that list, so
  `POST /datasets/{n}/iceberg` produced governed Parquet at 0644 inside 0755
  directories, readable by every local user with no row policy and no masks.
- **A local or mistyped `LAURELIN_DATA_URI` put the data plane on local disk at
  0755.** `is_remote_uri` was a case-sensitive exact-prefix match, so
  `S3://bucket/p`, `s3:/bucket/p` and `s3a://bucket/p` were all judged *local*
  and became directories of that literal name under the process CWD, with the
  governed Parquet 0644 inside, while the operator believed the data was in a
  bucket. Nothing logged, warned or failed. Unaddressable schemes are now a
  startup error, and a local data URI is created 0700.
- **`metadata.db` was created 0644, with no note, when its path was a dangling
  symlink.** Relocating the database to another volume before first start is
  ordinary; `O_EXCL` refuses to follow the link, `harden_existing` then stat'd
  through it, got `-1`, and did nothing, so sqlite3 created the real file at
  `0666 & ~umask` — holding the scrypt hashes, session tokens and DSNs written
  during the bootstrap run. It self-repaired on the *next* start.
- **`LAURELIN_STRICT_FILE_MODE=1` did not touch the two other rows the admin
  screen shows.** `laurelin.yml` stayed 0644 and the workspace directory 0755,
  on every restart, under a card offering exactly the two remedies that
  provably did nothing. Strict mode now takes both, from `find` as well as
  `init`, because a restart is what the card asks for.
- **`iceberg-catalog.db` was absent from `GET /workspace/file-security`**,
  though `core/iceberg.py` pre-creates it 0600 on the argument that a warehouse
  URI in it can carry a credential. On a PostgreSQL store the report listed one
  file while the SQLite catalog sat beside it on disk.
- **Concurrent `Workspace.init` raised an unhandled `FileExistsError`** from a
  check-then-`O_EXCL` race — 17 of 40 trials, and reachable from the front door
  via `api/context._bundle` on a threadpool.

**Ontology overlay and object index.**

- **Column masks were bypassed on read.** A live overlay edit is merged onto
  the base rows *after* the policied scan, so a masked column written by a
  mask-exempt editor was read back in plaintext by a masked reader — through
  the object, through `aggregate` grouped by that column, and by searching a
  fragment of the value. The mask returned the moment the edit was folded,
  which is what proved only the overlay path disclosed. The same for creates:
  `_policy_admits` honoured the row filter and returned the **raw** payload.
  The *write* half of this guard was present and correct the whole time; the
  read half did not exist.
- **Row policy was bypassed the same way.** A user restricted to
  `realm='valinor'` kept reading an object an admin had moved to
  `'beleriand'`, and could filter for it by that value — for exactly as long as
  the hand edit was live. A service built with a policy but no way to resolve
  it against the overlay now refuses rather than serving it unpoliced.
- **An update could erase a primary key.** `new_key is None` short-circuited as
  "restating the key". The object rendered with a `__pk` of the string `'None'`
  and was addressable under neither name, with no API anywhere to revoke a live
  object edit — and a second one wedged `writeback` for the whole object type,
  permanently, from two ordinary action calls.
- **`POST`/`DELETE /ontology/object-types/{n}/index` had no per-type gate.** A
  global editor with no grant on the type — 403 on the type and on its objects
  — got a 200 with the global object count and could drop the owner's index;
  and a policied caller got the operator counters `get_object_type`
  deliberately withholds from them.

**Import.**

- **`--merge --rename-prefix` overwrote the destination's live dataset parts.**
  `_write_part` truncates, and the part-write loop used the archive member name
  verbatim as the destination storage key; the rename touches only metadata.
  So on the documented *safe* resolution for a name collision — a `note`, not a
  refusal — an archive's `data/salaries/parts/<uuid>.parquet` landed on top of
  the destination's live part of that key, which its own `salaries` still
  referenced. Every archive exported from a workspace keeps that workspace's
  part keys, so a round trip collides by construction, and the archive is
  unsigned. Arriving parts whose key is already in use are now written to fresh
  keys, with the version rows rewritten in the same pass and the relocation
  reported. This also closes the sibling: the pre-commit rollback deletes
  `written_parts`, which were the destination's own keys.

### Fixed: three DSN redactors that disclosed live credentials on admin routes

`federation.redacted_source`, `engines._redact_uri` and
`connectors.redacted_config` each had their own regex, each regex was a
different guess about where a credential lives, and ten inputs got past them —
into responses an admin reads in a browser, and (via `_dump`, which redacts
every dataset's `source`) into responses a *viewer* reads:

- a password containing `/`, `?`, `#` or `//`;
- a DSN with no username, `postgresql://:hunter2@db/prod`;
- a secret in a query parameter, `?api_key=…`, and a bearer token in a bare
  userinfo, `https://ghp_…@github.com/…`;
- an ODBC/JDBC keyword string, `Server=db;Uid=alice;Pwd=hunter2;`;
- ADBC driver options carrying `Bearer …` under
  `adbc.flight.sql.rpc.call_header.authorization`;
- and, in connectors, every nested value: `{"auth": {"password": …}}` and
  `{"headers": {"X-Api-Key": …}}` were not redacted at all.

The three regexes are replaced by one module, `laurelin/core/redaction.py`,
which decides *whether* a value may be shown before it decides how. A
`scheme://user:password@host` DSN has its credential masked and everything
else disclosed — **unchanged from before, deliberately**: whether a viewer
should see `user@host:port/db` at all is an open product question, and this fix
does not answer it. Anything whose shape cannot be read — a keyword string, a
URL carrying a query, an option namespace that belongs to a driver, a nested
config object — is **withheld whole** rather than guessed at, and the UI renders
a withheld value as "withheld" with an explanation instead of a blank field.

Two behaviours narrowed as a result and are called out in their tests: every
HTTP header value is withheld (the name denylist missed `X-Api-Key`), and every
engine option value is withheld (it missed the ADBC call header). The residual
disclosure — usernames and endpoints, a secret in a URL *path*, a credential
pasted into a free-form value — is stated in the module docstring rather than
left implicit.

### Fixed: seven more credential disclosures, found by attacking the redactor above

The module above was then attacked, and these got through it. All were
reproduced on a running server before being fixed.

- **A driver's exception text was stored and served verbatim.** A failed
  connector sync put the raw message in `sources.last_sync_error` — the same
  JSON object whose `config.url` it masks — and in an audit row. A password
  containing a space came back as ``unexpected spaces found in "SUPER SEKRET"``
  to any **editor** on `GET /sources`, and to any **viewer** on `GET /audit`,
  who is 403 on the source itself. No redactor can find a credential in a third
  party's prose; this one does not try. It substitutes the credentials the
  source's own config says we handed the driver, then applies the shape rules,
  then withholds the message whole if a known secret survived both. The
  unredacted exception goes to the server log.
- **`PUT /datasets/{name}/federated` and `/clickhouse` returned the whole DSN in
  the 502 body** — DuckDB's postgres extension prefixes its IO error with the
  connection string, one line above a success path that redacts the same dict.
  Admin-only, so not a privilege crossing, but a live credential in a response
  body, a proxy log and the UI's error box.
- **A password containing `,` `;` `'` `"` `(` `)` `[` `]` `<` `>` or a space
  survived in full inside any free-form value**, because the embedded-URL regex
  ends a match at exactly those characters and the truncated fragment had no
  `@` left in it to mask. Reached through a source's `query`, which is
  editor-readable and printed in the Sources "From" column. A match is now
  trusted only when its authority is complete and credential-free; otherwise the
  value is withheld.
- **A URL with an `@` in its path was given a fabricated hostname.** Taking the
  last `@` read the whole authority and path as userinfo, so
  `reports.prod.example.com` and `reports.stage.example.com` both rendered as
  `https://*****@2024.csv` — not over-masked, *replaced*, with nothing on screen
  saying so. Ambiguous parses are now withheld. This narrows two cases that
  previously rendered as masked DSNs (a password containing `/`, and an `@` in a
  path); the credential was never disclosed in either, and is not now.
- **A password equal to its username** was disclosed by the username slot, which
  the endpoint policy shows. Both slots are now masked in that one case.
- **Dashboard panel SQL and schedule targets carried DSNs to lower-privileged
  readers** — `GET /dashboards` is viewer-gated — and are now **refused at
  write time**. They cannot be redacted on read: a panel's SQL is executed and
  round-trips through the editor's textarea, so a mask returned would be saved
  over the real query. **This does not retroactively redact values already
  stored**; it stops new ones.

The residual disclosure is unchanged and still stated in the module docstring:
usernames and endpoints on a parseable DSN, a secret in a URL *path*, and a
credential pasted into a free-form value that is not URL-shaped.

### Fixed: ontology updates wrote through column masks and out of the row policy

Creates were checked against the backing dataset's policy. Updates were not,
and the gap was documented in the code rather than closed, because re-checking
the merged row needs the unmasked base row the policy has already withheld from
the caller. Both halves were reachable with EDITOR on the object type alone:

- **An update over a masked column** was read back in plaintext by its author —
  the overlay is applied *after* the policied scan, so the mask on that cell was
  defeated by writing through it. It is also a blind overwrite of a value the
  author was never shown, and a writeback makes it the dataset's value for
  everyone.
- **An update that moved a row out of the allowed set** still showed that row to
  its author, because the row filter runs on the *base* value. The mirror image
  of the create channel closed earlier, except the row is pushed into another
  tenant's partition rather than pulled out of it.

Both are now **refused**, not silently dropped — reporting success for a write
that did not happen is the one outcome worse than either bug — and the message
names the offending properties and what the caller can do instead. The merged
row is evaluated under the policy through a system view (the same device
`reindex` uses), so the check never hands the caller the base values it needed;
a regression test asserts no refusal quotes them.

### Fixed: three ways around that update guard, found by attacking it

The guard above holds. What it did not cover was every other way to reach the
same outcome, and the first of these made it ineffective on its own.

- **Delete, then create.** `_refuse_shadowing_create` asked the *overlaid* view
  whether the key existed, and the overlay includes the caller's own pending
  delete — so `raze city-0` followed by `found city-0` was accepted. An editor
  whose update was correctly refused reached exactly the refused state instead:
  the row moved into another tenant's realm, both masked columns overwritten
  with values of their choosing, durably, in the shared materialization, for
  every reader. A delete is a pending edit and does not free the key; the check
  now asks the base dataset plus live creates.
- **Creates were never policy-checked on the write side at all.**
  `_policy_admits` hides a create that lands outside the caller's rows *from the
  caller*, which is not the same as not writing it: the edit is recorded and
  materialized under a system identity into the shared store, where the tenant
  it landed on reads it. A create is now refused if the policy would not return
  it to its author. Masks are deliberately **not** checked on a create — writing
  a masked column of a new object overwrites nothing, and refusing it would stop
  a policied editor creating anything on a dataset carrying any mask.
- **An update could rewrite the primary key.** The probe sees nothing wrong (the
  key is unmasked, the row filter is on another column), so an editor could move
  an object onto a key held by a row they cannot see. After a writeback the
  dataset held two rows with that key; the object view dedups last-wins, so a
  later delete removed both — six objects to four, with nothing in the audit
  trail naming the loss. **Primary-key-changing updates are now refused for
  every caller**, policied or not. This removes a capability that previously
  appeared to work; delete-and-create expresses the same intent through two
  operations that each have a guard.

Related, and defence in depth rather than a reachable bug now: `writeback`'s
duplicate-key guard counted duplicates in the fold's *input*, so a fold could
create the duplicate the guard exists to prevent and then permanently block
every later writeback. It now also checks the fold's output, before publishing.

### Fixed: object-store lag that no write could ever clear

A store that was a dataset-version behind *and* held unapplied edits reported
"behind by N edits" and nothing else, while `catch_up` bailed on the version
mismatch before replaying anything — so N climbed 1, 2, 3 with every edit and
only a rebuild cleared it. The index payload carried no built-at dataset
version, so neither the API nor the UI could tell the self-healing state from
the one that needs a rebuild, and the hint text was deliberately worded to be
true in either case.

`GET /ontology/object-types/{name}` now carries `dataset_version`,
`current_dataset_version`, `stale_version` and `stale_definition`, and the
Object store panel names the actual remedy for each of the three states instead
of hedging. The version fields are not withheld from a policied caller — a
version number counts versions, not rows — unlike the object and lag counters
beside them.

A fourth kind of not-current was found afterwards and is the only one that
served *wrong* rows rather than stale ones: the definition fingerprint did not
cover `backing_dataset`, so repointing an object type at a different dataset
left every field reporting fresh while queries answered out of a
materialization of the old one. The binding is now part of the fingerprint, so
a rebind invalidates like any other definition change. Existing
materializations will report `stale_definition` once and need one rebuild.

### Fixed: five gaps in the workspace file-permission repair

Found by attacking the fix that made `metadata.db` 0600.

- **`control.db` was invisible to the admin file-security report**, which was
  built from the per-workspace store alone. It holds every user, session token
  and API token for *every* workspace on the server, so on an upgraded
  deployment the file with the widest blast radius got a warning in a log and
  nothing else. It is now in the report.
- **`harden_existing` never re-stat'd after `chmod`**, and reported the mode it
  had *asked for*. On a filesystem that accepts chmod and ignores it (vfat,
  exfat, ntfs-3g, some CIFS and FUSE mounts) the advisory note — the field the
  docs tell operators to act on — asserted a tightening that had not happened.
  It now reports what the file has, and says so explicitly when a chmod did not
  take.
- **`Workspace.init` created `data/`, `pipelines/` and `ontology/` with no mode
  at all.** Inside a 0700 root Laurelin made, that is invisible; under a root
  the operator provisioned (an upgraded workspace, or the Dockerfile's
  `RUN mkdir -p /data`) they inherited `0777 & ~umask` — every dataset Parquet
  readable by any local user at umask 022, and a world-writable `pipelines/` at
  umask 000, whose contents are `exec`'d on every build. They are created 0700
  now. Existing directories are still left alone, deliberately.
- **The import-state file was written then chmod'd**, and was caught at 0644
  with its content on disk. Both call sites now use the same atomic
  create-private-and-rename primitive `cli.py` already had.
- **`SQLiteBackend("")` chmod'd the process's working directory**, because the
  "no file" check ran on `str(Path(""))`, which is `"."`. No product path
  reaches it; the guard now tests the string it was given.

**Still true and still deliberate:** a workspace upgraded from an older release
keeps its metadata database group-readable at 0640. World access is removed
without asking; group access is preserved because a group can be a set of
principals somebody provisioned, and silently breaking a backup agent is a worse
failure than the one being fixed. On the upgrade population that premise is
often wrong — the group bit and the world bit are the same artefact of umask 022
— which is why the residual is on the admin screen in gold rather than only in a
log. `LAURELIN_STRICT_FILE_MODE=1` strips it.

### Workspace export/import — the anti-lock-in claim became a command

Until now "you can leave" was a claim about file formats. There was no export
verb, no export route, no backup anywhere in the tree. A Foundry refugee could
read the Parquet and would still have to hand-reassemble datasets, versions,
ontology bindings, policies and markings out of a directory tree — which is the
work they were trying to escape.

- **`laurelin export` / `laurelin import` / `laurelin verify-governance`**, the
  same three verbs over HTTP under `/api/v1/workspace/`, and an
  **Admin → Portability** page that previews, downloads, imports, lists what has
  to be re-supplied, and diffs the governance fingerprint cell by cell.
- **A POSIX tar stream**, manifest first and trailer last, readable with
  `tar tvf` on a machine that has never heard of Laurelin, and pipeable:
  `laurelin export - | ssh host laurelin import -`. Rows travel as JSONL through
  the store rather than as a copy of `metadata.db`, which is what lets an archive
  taken from SQLite restore onto PostgreSQL.
- **The proof is the product.** `verify-governance` recomputes a per-
  `(principal, dataset)` decision matrix — sha256 over Arrow IPC bytes, through
  all three enforcement paths — so "it governs identically after the move" is
  something an operator checks, not something the release notes assert.
- **Import binds no principal.** Every rule imports verbatim (dropping an
  unresolvable grant would *widen* — measured: an empty grant list flips a
  dataset from allowlist to readable-by-every-viewer), while users, group
  memberships and clearances travel and are never written. Schedules land
  disabled, connectors land needing credentials, and imported pipelines do not
  execute until an admin says they read them.

Limitations are in [docs/PORTABILITY.md](docs/PORTABILITY.md) and are real:
federated/ClickHouse/StarRocks datasets carry no data, Iceberg tables are not
moved, object-store data planes are unverified, and there is no incremental or
resumable export.

### Export/import — the credential and archive-integrity fixes on the above

Everything here was reproduced against the code as first written, most of it
through the real HTTP front door, before it was fixed. Each has a regression
test that was watched failing without its fix.

- **Fixed: six kinds of credential travelled in the clear.** The secret posture
  was a *denylist* over key names anchored to exactly `url`/`host`/`user`, so a
  source configured with `base_url`, `endpoint_url`, `hosts`,
  `bootstrap_servers`, `connection` or `path` shipped all six verbatim with
  their passwords — while `manifest.withheld` positively certified that the
  row's endpoint had been withheld. `SourceUpsertRequest.config` is
  `dict[str, Any]`, so the key vocabulary belongs to the caller;
  `datasets.source_json.path` is *required* by three of federation's four source
  types and is exactly where a presigned S3 signature lives.
  **`datasets.source_json` and `sources.config_json` are now an allowlist over
  shape keys** — anything else is nulled, whatever it is called, and named in the
  manifest.
- **Fixed: `audit_log.details_json` used a narrower matcher with no endpoint
  keys**, and production logs free-form failure text under `reason` — so an OIDC
  error carrying `?client_secret=` in a token-endpoint URL travelled verbatim.
  Endpoint and free-text keys are nulled now (subjects like `username` still
  travel, because an audit trail without them is not one), and a row whose
  surviving values still look like a credential is withheld whole.
- **Fixed: the credential scanner walked past `Authorization: Bearer`,
  `passwd=`, `jdbc:`, `mongodb+srv://` and `rediss://`**, reporting zero warnings
  while shipping them. It also never looked at `ontology/*.yml`,
  `dashboards.panels_json`, `object_apps.config_json` or
  `schedules.targets_json` — all of which travel near-verbatim, and a dashboard
  panel's SQL is documented as arbitrary. All of it is scanned now, the flag is
  `--allow-content-warnings`, and the warning preview stops at the match
  (measured: it used to reproduce the credential inside `manifest.json`).
- **Fixed: `dataset_versions.files_json` was imported unvalidated**, so an
  archive naming `../tenant_b.parquet` — together with governance the same
  archive authored — served a sibling workspace directory's Parquet through
  `GET /datasets/{name}/rows`.
- **Fixed: `TRAILER.json`'s per-member digests were parsed and never compared.**
  A repacked archive with a byte-identical trailer stripped a marking and
  widened a grant to `everyone can_view can_edit`, imported clean, and logged a
  successful `workspace_imported`. (The trailer still detects corruption and
  partial rewrites, not tampering — there is no signature.)
- **Fixed: import committed metadata before landing files.** Any failure in that
  window left attacker pipelines on disk with the acknowledgement gate *off*
  (it reads "acknowledged" when the state file is absent), reachable by a single
  viewer-gated `GET /transforms` — and the failure handler then deleted the
  Parquet of versions that were already committed. The commit is now the last
  thing that happens and the handler checks.
- **Fixed: a merge could widen, overwrite or crash.** Imported `ontology_grants`
  were unioned onto the destination's same-named object type (a viewer went from
  403 to reading a dataset the archive never contained); workspace files were
  overwritten by `shutil.move` after the commit, destroying an ontology file or
  bricking every ontology route with a duplicate `api_name`; and a destination
  sharing one dashboard, app, schedule, source, engine, group or marking name
  aborted with a raw `IntegrityError` — an HTTP 500, on the only two resolutions
  the collision report printed. All of these are now named collisions.
- **Fixed: a 514 KiB archive could take 4.6 GiB of RSS** by declaring a 512 MiB
  JSONL member, and still report success. Member sizes are checked from the tar
  header.
- **Also fixed:** duplicate members silently last-wins; `ontology/.` chmod'ing
  the ontology directory to 0600; a member nested inside another member; a
  `format_version` of 0 or -1 read as 1; `data_state` trusted over what actually
  arrived (a dataset with no parts read as a bare `FileNotFoundError` instead of
  the promised 409); dataset names, kinds, marking names and version numbers no
  other code path could have written; malformed rows escaping as driver errors
  instead of refusals; imported sources and engines carrying no
  needs-credentials marker; and a namespaced marking leaving the archive's own
  clearance checklist naming a marking that no longer exists.

### Security — the workspace stopped being world-readable

Measured before the fix: `<root>/metadata.db` and `<root>/laurelin.yml` were
both mode 0644, and nothing in the tree had ever passed a mode to `open` or
`mkdir` — every file came out at `0666 & ~umask`. That database is not a cache:
it holds unexpired session tokens, in-flight PKCE verifiers, scrypt password
hashes and every connector DSN in the clear. Any local user on a shared host
could read a bearer token and replay it. The export hardening in the previous
release made this sharper — the archive you carried off the box was better
protected than the workspace it came from.

- **Created private, not created and then repaired.**
  `os.open(..., O_CREAT | O_EXCL | O_WRONLY, 0o600)` for `metadata.db`,
  `control.db`, `laurelin.yml` and the SQLite `iceberg-catalog.db`;
  `mkdir(mode=0o700)` for a workspace or
  server root Laurelin creates. `chmod` after `open` leaves a window in which
  SQLite writes the header, the schema and the first session row — and an
  attacker who opened an fd inside it keeps reading afterwards, because
  permission is checked at open and never again. A regression test sabotages
  `os.chmod` outright and still requires 0600.
- **`-wal` and `-shm` need no separate handling, and must not get any.**
  Measured: SQLite creates both by copying the main database file's mode, so a
  0600 `metadata.db` yields 0600 siblings. Chasing the siblings would have
  fixed the copies, left the original, and raced the checkpoint that deletes
  them. On PostgreSQL there is no local file at all, and the admin API says
  `store_is_remote` rather than reporting a comforting mode for a file that
  does not exist.
- **An inherited workspace is repaired in one direction only.** Opening a
  database from an older release strips world access silently — 0644 grants
  read to others and write to nobody but the owner, while every process that
  opens this database opens it read-write, so no working component was reaching
  it through the `other` bits and there is no configuration to break. Group
  access *survives*, because a group is a set of principals somebody had to
  provision (a backup agent, an operator with read but not write), and turning
  a security fix into a silently broken backup is a worse failure than the one
  being fixed. `LAURELIN_STRICT_FILE_MODE=1` strips it too.
- **Directories are never retro-tightened, and a chmod we are refused is not
  fatal.** A directory the operator made is a directory whose mode the operator
  chose, and narrowing a tree reaches backup agents and log shippers that one
  file's mode never touches. Refusing to start is a real option for a
  governance product but not one earned by a defect the product shipped, so an
  EPERM (root owns the file, the service runs as someone else) is reported and
  the server serves.
- **Admin → Workspace files on disk** shows the mode each file actually has,
  read back from the filesystem, because a partial repair is only defensible if
  the operator can see the residual — and a WARNING in a log nobody tails is not
  how anyone makes that decision. Names only, never paths or DSNs.

### Ontology — the object store stopped being thrown away on every write

The complaint this answers: "the ontology is slow as an application database."
It was, and for a specific reason. The object index reported itself stale
whenever the edit *count* changed, so a single hand edit invalidated the whole
index for that object type and every subsequent read fell back to a full scan
that replayed the entire edit log. Read cost grew with total write history.

- **An edit now upserts the materialization instead of invalidating it.** For
  the default (metadata-store) backend the log append and the row upsert are
  **one transaction**, so a reader can never see "caught up" with the row not
  yet there. Measured on 100 K objects: reading a page costs **1.01× / 0.97× /
  0.96×** of the zero-edit baseline at 100 / 1 000 / 10 000 edits, and edit
  #1 000 costs **1.00×** what edit #1 did. Under the old behaviour the first
  edit sent every subsequent read back to a full scan.
- **`object_index_state.edit_count` became `applied_seq`**, a catch-up
  watermark rather than an invalidation flag, and `object_edits` gained a
  gapless per-type `edit_seq`. A store that has not applied every committed
  edit **refuses to answer** and the read falls through to the scan path, which
  is always correct. Unreachable, a missing state row and a dropped table all
  count as "behind". *This field is visible over HTTP* on
  `GET /ontology/object-types/{name}`, which also now reports `lag` and `store`.
- **A pluggable object store** (`laurelin/ontology/store.py`): the metadata
  store (default, no new dependency) or StarRocks primary-key tables via Stream
  Load. The StarRocks implementation is **unverified against a real server** —
  it has only run against an in-memory double.
- **A content digest per materialization**, XOR-combined and maintained
  incrementally, because a watermark cannot detect divergence: a store that has
  drifted can be perfectly caught up by position.
- **Writeback** (`POST /ontology/object-types/{name}/writeback`, manual only):
  folds the overlay into a new dataset version and marks the folded edits
  rather than deleting them, so the version stays reproducible. Requires edit
  rights on the **backing dataset**, not just the object type. Refuses a
  transform-produced backing by name unless overridden — otherwise the next
  build silently reverts the folded edits.

### Ontology — concurrency, governance and writeback fixes on the above

Everything in this section was demonstrated with runnable reproductions against
the code as first written, most of them with two ordinary threads calling
`apply_action`. The common shape: the store was not *behind*, it was **wrong
while level**, so the watermark reported `fresh: true, lag: 0`, reads never fell
through, and `catch_up()` had nothing to replay.

- **Fixed: a concurrent edit could be silently discarded.** An edit's rows are a
  read-modify-write of the current rows, and the read happened on a different
  connection from the write. Two people editing one object each merged onto the
  same pre-image and one committed edit became invisible to every reader; a
  delete racing an update let the update re-insert the deleted row; four
  concurrent creates all took the same ordinal; and the divergence digest was
  computed from a superseded base, so ordinary concurrent writes raised a false
  corruption alarm. `ObjectStore.commit_edit`/`apply_edit` now take a
  `build(pre_image, seq)` callback invoked *inside* the write transaction, under
  a lock on the object type's state row.
- **Fixed: on PostgreSQL, creating an object silently killed the
  materialization.** `object_index.ord` was `INTEGER` — 64-bit on SQLite, 32-bit
  on PostgreSQL — while created objects sort at `2**62 + edit_seq`. The INSERT
  overflowed, the write path swallowed it, the user saw success, and reads
  reverted permanently to the full scan this feature exists to remove; the
  rebuild endpoint then returned 500 forever. Now `BIGINT`, with a migration.
- **Fixed: `POST /ontology/object-types/{name}/index` returned 500 when an
  object was created during a rebuild.** `reindex` read its three inputs on
  three connections; they now come from one pinned snapshot.
- **Fixed: a policied user could overwrite an object their row-level security
  hides.** A create for an existing key is a replacement — it inherits the
  hidden row's position and a writeback folds it over that row in the dataset,
  destroying another tenant's data for everyone. Refused for callers a dataset
  policy narrows.
- **Fixed: withdrawing a property from the ontology did not stop it being
  served.** Every other read path projects to the declared properties; the
  materialization did not, and each subsequent write copied the withdrawn
  property forward. `object_index_state.type_fingerprint` now invalidates on a
  definition change, exactly as a dataset version does.
- **Fixed: `index.objects` disclosed the unpoliced object count** to a user
  whose row-level security shows them a subset. `objects`, `lag` and
  `applied_seq` are `null` for policied callers.
- **Fixed (StarRocks store): a failed load after the log commit was reported to
  the caller as a failed write** — for an edit that was durable and visible to
  every reader, with no audit record. `commit_edit` is now all-or-nothing by
  contract. The store also refuses to apply an edit it is not exactly one
  position behind, which is what stands in for the lock it cannot take.

**Writeback**, same treatment:

- **Fixed: two concurrent folds could destroy an edit permanently.** The version
  check was a check-then-act; publishing is now compare-and-set
  (`catalog.write(..., expect_version=...)`, raising `StaleBaseVersion`). The
  same gap let a fold discard a whole dataset version published by a concurrent
  build — the exact scenario the check was written for.
- **Fixed: a fold could not express clearing a property to NULL.** The overlay
  merged with `COALESCE`, which cannot tell an assigned NULL from an absent one,
  so folding a null-clearing edit silently restored the old value — and broke
  live null updates that had been working before the fold.
- **Fixed: a fold deleted every duplicate-primary-key row from the dataset.** It
  materialized the object view's last-wins dedup into the data. Now refused,
  with a pointer at doing the dedup in a transform where it is visible.
- **Fixed: a create over an existing key nulled that row's undeclared columns.**
- **Fixed: the transform-backed guard disappeared after its first override**,
  because writeback overwrites the version source it was reading.

### Engines — one policy decision, now rendered for three SQL dialects

Governance was "one decision, two renderers", but the SQL renderer *was*
DuckDB's SQL with a seam drawn around it. That is an abstraction now, because
two other engines implement it — and each one bent it somewhere different.

- **A dialect seam** (`laurelin/core/dialects.py`). `SqlDialect` has no safe
  defaults: every method is abstract, so a dialect that forgets one fails
  loudly instead of silently emitting DuckDB syntax. `DatasetInfo.sql_dialect`
  now names the dialect a dataset's policy must be rendered in, because
  `scans_at_source` had been quietly carrying two facts — "read via the source
  expression" *and* "DuckDB renders the SQL" — that stop being the same fact
  the moment a second engine exists. DuckDB's rendered output is pinned
  byte-for-byte by a golden test, so adding an engine cannot change the first
  one.
- **ClickHouse-backed datasets** (`kind="clickhouse"`, `pip install
  'laurelin[clickhouse]'`, `PUT /datasets/{name}/clickhouse`): read-only,
  scanned in place by **chdb** — ClickHouse embedded in the process, so there
  is no server to run. Four divergences from DuckDB were measured, and every
  one of them is a leak rather than a wrong number: DuckDB's identifier quoter
  resolves a ClickHouse column to a *different* column and returns its data;
  the named-parameter channel is not byte-preserving (`a\nb` arrives three
  bytes, not four); `NULLIF(c, c)` leaves NaN unmasked, since NaN ≠ NaN; and
  because ClickHouse resolves `WHERE` against `SELECT` aliases, a flat
  statement evaluates the row policy against the **mask** — a total row-policy
  bypass that fails open.
- **StarRocks-backed datasets** (`kind="starrocks"`, `pip install
  'laurelin[starrocks]'`, `PUT /datasets/{name}/starrocks`): read-only, read
  over the MySQL wire protocol with the row policy and column masks compiled to
  StarRocks SQL and pushed down. This is the first engine that is a *server*
  rather than a library, and that changes the threat model more than it changes
  the syntax. Stacked statements execute — `SELECT 1; INSERT INTO t VALUES
  (99)` on one `execute()` runs the INSERT — so a policy value reaching SQL as
  text would be a remote **write**, not a wrong read. `StarRocksDialect.
  literal()` therefore **raises**, and no escaper ships even unused; every
  query goes through a prepared cursor, which StarRocks refuses to let express
  an INSERT at all (error 1295). Point it at an account holding `SELECT` and
  nothing else.
- **Type portability is checked, not assumed.** A row policy is a comparison of
  a column's *text* and a hash mask is a digest of it, and the four
  stringifiers involved — Arrow's row key, Arrow's digest input, and each SQL
  engine's — do not agree. Measured: a `decimal(12,2)` tenant key with policy
  value `'1.1'` returned nothing from `/datasets/{name}/rows` and *another
  tenant's rows* from `/query`. So each dialect declares the Arrow types it
  renders identically to the Arrow reference and the renderer **refuses**
  everything else, because the alternative is a policy that admits a different
  set of rows depending on which engine ran it. `null` and `redact` masks need
  no text rendering and stay available on every column of every type. Bool is
  a portable row key on DuckDB and ClickHouse but **not** on StarRocks, where
  `CAST(b AS STRING)` is `'1'` and Arrow says `'true'`.
- **Fixed: DuckDB and StarRocks over-claimed decimals.** Both said the whole
  decimal family was portable. Past scale 6 it is *Arrow* whose rendering
  changes — a `Decimal` whose adjusted exponent falls below -6 prints in
  scientific notation, so pyarrow gives `'0E-7'` where both engines give
  `'0.0000000'` — and the corpus sampled scales 0, 2 and 6, one step short of
  the boundary. Measured on a live StarRocks server and on DuckDB. Decimals are
  now claimed only at scale 0–6; hash-masking a wider decimal was already
  emitting a token that would not join.

**Positioning, stated once:** StarRocks is the serving tier Laurelin is built
toward — querying Iceberg is a first-class path there, so "open at rest"
survives the serving tier rather than being traded for it; it joins natively,
and an ontology link *is* a join; and it has primary-key tables with real
upserts, which is what an operational store needs. ClickHouse is a fully
supported peer, not a lesser one. Both shipped in this slice. DuckDB remains
the embedded default for medium data and Trino/Dremio/Databricks remain the
federation and delegation path; none of that changed.

**Not verified, and load-bearing enough to say so.** The StarRocks read path
was measured against a StarRocks container locally, but the opt-in CI job that
runs those suites has **never executed on GitHub Actions** — it is written from
that container's behaviour. Reading a StarRocks **Iceberg external catalog** is
untested: the three-part `catalog.db.table` scan expression works, but the
type-agreement tables were measured on native StarRocks columns and the
Iceberg→StarRocks mapping could move DECIMAL scale or DATETIME precision. And
the **StarRocks object store has only ever run against an in-memory double** —
no part of it has touched a real server. It is also not selectable by
configuration: `OntologyService` constructs `MetadataObjectStore`, so every
deployment today runs the default store and the StarRocks one is a seam with
an implementation behind it, not a switch an operator can throw.

### Security

Three fixes in code that **predates both new backends** — they were found while
building the dialect seam, not caused by it.

**Nobody is exposed.** `0.2.0` was never tagged or published and `0.1.0`
existed only in the source tree, so there is no release anyone could have
installed that carries any of these. This is a changelog note, not a security
disclosure, and there is nothing to upgrade from. It is here because a
governance layer that quietly fixes its own fail-open bugs is not one.

- **`GET /datasets` and `GET /datasets/{name}` returned source config
  unredacted.** A federated PostgreSQL dataset handed
  `postgresql://user:password@host/db` to anyone who could *see* the dataset —
  the `viewer` role is enough, and both endpoints are viewer-readable by
  design. The credentials were in the `source` dict, which was never meant to
  leave the server. Redaction now happens at the single point where a
  `DatasetInfo` is serialized rather than in each route, so a new dataset kind
  cannot reopen the same hole by forgetting to opt in.
- **`SqlPolicy.render` could emit `select_list='*'` with masks still pending.**
  When column discovery returned nothing — an unreachable source, an empty
  `DESCRIBE` — the renderer joined an empty list and fell back to `*`. That is
  an *unmasked* read of a dataset that has masks to apply: fail open, in the
  one place in the codebase that must fail closed. It now refuses the read and
  explains why. A related fail-open went with it: a mask whose column name
  differed from a real column only in **case** silently masked nothing, and is
  now a refusal — while a mask on a genuinely dropped column still passes, so
  schema evolution does not start denying datasets. Both rules apply to every
  dialect, not just the one that surfaced them.
- **`is_federated` was used where `scans_at_source` was meant, at five call
  sites.** "Who owns the table" and "where does the scan happen" were the same
  question until Iceberg made them different — Iceberg is owned and versioned
  like a managed dataset but read at the source like a foreign one — and three
  of the five were live Iceberg defects, each shipped and each unhit only
  because nothing exercised that combination: a SQL transform reading an
  Iceberg input went down the local-Parquet path, which has no parts to scan;
  `GET /datasets/{name}/rows` paired an Iceberg table's *current* rows with an
  *old* version's row count; and an ontology object type could bind to an
  Iceberg-backed dataset, which the federated-only check existed to prevent.
  `DatasetInfo.scans_at_source` is now the predicate every read path branches
  on, and the kind→dialect and kind→reader maps are **total with no default** —
  an unknown kind raises rather than falling back to DuckDB, because the engine
  that would get read with the wrong dialect is always the newest and
  least-checked one, and `source_table`'s dialect-mismatch guard cannot catch
  that case: both sides would say "duckdb".

### Fixed

- **Created objects bypassed row-level security on every read path.** The edit
  overlay was applied with no policy at all, so an object created with
  `realm='beleriand'` was returned to a user restricted to `realm='valinor'`.
  Created objects now pass the backing dataset's row policy before they are
  visible, failing closed when the payload omits the policy column. Updates
  remain a narrower guarantee — see `_policy_admits` for exactly what is and is
  not covered.
- **`reindex` materialized under the calling user's policy**, and the rebuild
  endpoint is only EDITOR-gated, so a policied editor baked their narrowed view
  into the index everyone reads. It now builds under a system identity.
- **Every created object shared one ordinal** (`2**63-1`), leaving them tied
  under `ORDER BY` so paging between them was arbitrary; and a create for a key
  that already existed emitted *both* rows in the SQL path, so the pushdown
  counted one more object than the in-memory path did.
- **The search mirror was rewritten for the whole object type on every sync**,
  which would have made each single-row edit O(objects) — invisible on
  Postgres, which needs no mirror at all.

## 0.2.0 — 2026-07-27

**The first published release.** `0.1.0` existed only in the source tree and
was never tagged or uploaded, so there is nothing to upgrade from — this
describes what Laurelin *is*, not what changed since something you could have
installed.

### Storage

- **Versions are manifests of Parquet parts.** `append` writes only the new
  rows and references the previous version's parts, so adding today's data
  costs today's data: **72× faster than a rewrite** on a 5 M-row dataset with a
  1% delta, and the advantage widens as the dataset grows. `compact()` merges
  parts back when you want to pay that cost deliberately;
  `LAURELIN_AUTO_COMPACT_PARTS` does it on a threshold.
- **Object storage** for dataset Parquet (`s3://`, `gs://`, `abfs://`) via
  `LAURELIN_DATA_URI`. A version commits by inserting its manifest row rather
  than renaming a directory, so the protocol is native to object stores.
- **Apache Iceberg datasets** (`pip install 'laurelin[iceberg]'`): written and
  versioned by Laurelin, readable by Spark/Trino/Snowflake/DuckDB without it.
  Each write is a snapshot *and* a Laurelin version pinned to it, so time
  travel, lineage and builds share one notion of "when". Branches are named
  pointers into the snapshot history (cutting one copies no data); merges
  fast-forward. Schema changes are additive by default, and dropping or
  renaming a column names the transitive downstream datasets before it lets
  you. No REST catalog to run — pyiceberg's `SqlCatalog` points at the
  database Laurelin already has.
- **Federated datasets**: govern Iceberg/Delta/Parquet/PostgreSQL tables
  without holding the bytes, scanned in place with pushdown.

### Ontology

- **Query pushdown**: object queries run in DuckDB over Parquet instead of
  materializing every row in Python — **36 s → 1.4 s at 5 M objects**.
- **An opt-in object index**: paging 293 ms → 27 ms at 1 M objects, and key
  lookups **constant-time** at 1.4 ms. A stale index is never read — freshness
  is checked against both the dataset version and the edit count on every
  query, and a request carrying row-level security never touches it.
- **Trigram-accelerated search**: a selective search over 800 K objects goes
  **288 ms → 5.9 ms** and stays flat. Deliberately trigram, not full-text:
  search means *substring*, and token matching would silently redefine it.
  Hits are ranked by where the term appears in the title.
- **Aggregations**: `POST /ontology/objects/{type}/aggregate` — group-by with
  count / sum / avg / min / max / median / count_distinct, pushed into DuckDB
  (437 ms over 1 M objects). It aggregates *objects*, so the edit overlay is
  included; SQL over the backing dataset is not.
- **Object apps**: curated single-type views — the columns that matter, the
  filters that scope them, and only the actions an operator should reach for.

### Pipelines

- **Async builds** with a worker pool, leased so exactly one replica executes
  each and a dead replica's work is reclaimed rather than stranded.
- **Streaming transforms** (`streaming=True`): peak memory tracks a batch, not
  the dataset — 121 MB → 21 MB on 3 M rows.
- **Incremental transforms** (`incremental=True`): process only the rows an
  input has gained. Because a version is a manifest, "did this input only
  grow?" is a prefix comparison rather than a guess.
- **Data expectations**: `@expect(not_null(...), unique(...), row_count(min=1))`.
  Checked against the written Parquet **before the manifest row is inserted**,
  so a failing output is never published rather than published and retracted.
- **Scheduling**: cron and on-upstream triggers, leased so firing is
  exactly-once across replicas with no leader election.
- **Connectors**: PostgreSQL, HTTP and file sources, with incremental syncs
  that pull only rows above a cursor high-water mark.
- **Delegated compute**: `@remote_transform` submits SQL to Trino, Dremio,
  Databricks — anything speaking Flight SQL — and stores the reduced result
  with lineage intact. Laurelin runs no cluster and does not intend to.

### Governance

- Local users, **OIDC** (PKCE), **SAML 2.0**, and **SCIM** provisioning.
- RBAC, per-dataset ACLs, and ontology grants that **compose**: object access
  requires both the ontology grant and access to the backing dataset.
- **Classification markings** that propagate through lineage, so a pipeline
  cannot launder classified data into an unmarked output.
- **Row-level security and column masking**, pushed into the scan. The policy
  tax at 5 M rows went from 3.6× to **1.0×** — one decision, rendered to
  either Arrow or SQL, so federated and Iceberg tables are covered by the same
  interpretation of a rule.
- Append-only audit log.

### Operations

- **Horizontal scaling**: PostgreSQL schema-per-workspace, object storage, and
  leased builds. With both, nothing is node-local and replicas are
  interchangeable. Embedded mode (SQLite) remains single-replica by
  construction, and says so.
- **Query resource limits**: memory, wall-clock and concurrency, so one
  expensive query fails its own request rather than the replica.
- **Observability**: Prometheus `/metrics`, JSON logs, and an `X-Request-ID` on
  every response and log line.
- Docker Compose, a Helm chart, and a readiness probe.

### Interfaces

- React + TypeScript UI served as one self-contained HTML file, zero CDN:
  datasets, SQL workbench, pipeline canvas, dashboards, ontology explorer,
  object apps, admin.
- **Drag-and-drop import** with a schema preview before anything is created.
- **Dashboards** with SVG charts and no chart library. Panels draw from SQL or
  from an object aggregation — the latter reflects the edit overlay, which SQL
  over the backing dataset does not.
- **MCP server** so an agent is a scoped, audited user rather than a side door.
- Python SDK and a `laurelin` CLI.

### Quality

- **662 tests**, run against SQLite *and* PostgreSQL.
- **CI** across Python 3.11–3.14, with a guard that fails the run if the
  PostgreSQL suite silently skipped.
- **The tutorials execute in CI.** Running them found three defects a reader
  would have hit, including a path that stopped existing when versions became
  manifests.
- **A benchmark gate** asserting the scaling claims published in
  [docs/SCALE.md](docs/SCALE.md) as ratios, so a lost pushdown fails the build.

### Known limitations

Published deliberately rather than discovered later — see
[docs/SCALE.md](docs/SCALE.md#known-limitations-plainly) for the current list
with numbers. The short version: compute is DuckDB in one process and there
are no plans to change that; builds don't spread across replicas; object edits
are an overlay that doesn't flow back into Parquet; horizontal scaling needs
PostgreSQL; and Iceberg merges are fast-forward only.

**Not battle-tested.** It is early. It has a coherent design and a lot of
tests; it does not have production hours behind it.
