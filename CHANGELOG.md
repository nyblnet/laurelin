# Changelog

Notable changes to Laurelin. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[semantic versioning](https://semver.org/), with the caveat that pre-1.0
minor releases may break things.

## Unreleased

Nothing here has shipped: there is no git tag in this repository and nothing has
been uploaded to PyPI. Everything below is in `main`.

### Point-of-decision guidance: the paths a new user actually walks

An adversarial novice walkthrough of the consolidated UI found that every
journey's first success stranded the user. Fixed, in severity order, each with
a regression test (`tests/test_ui_guidance.py` + a react-dom/server harness
rendering the real components per role):

- **The dataset detail page is no longer a dead end.** Right where an import
  lands you, an "Open in:" row now offers the next steps, filtered by the same
  role rules as the nav: **Explore** (`/explore?dataset=…`, editor), **SQL**
  (`/workbench?dataset=…`, seeded with a starter query, every role), **New
  pipeline from this dataset** (`/pipelines?from=…`, opens the naming dialog
  and pre-picks the source, editor), **Schedule builds** (editor), and a
  **Dataset access** link for admins only — editors deliberately get no
  permissions door. Hidden while a dataset's reads refuse (needs-credentials).
- **The Dashboards empty state stops pointing at a door that cannot reach a
  dashboard.** It recommended "build one by clicking in Analyses" — and
  Analyses has no dashboard affordance at all. It now points at Explore and
  SQL, the two surfaces that can actually put a panel on a board.
- **Explore and Analyses each say which job the other is for** (one chart for
  a dashboard vs a multi-step document), at the moment of choosing instead of
  after building the same chart twice. The Milestone B merge remains the real
  fix; this removes the coin flip until then.
- **The "Add panel" modal offers the no-code path** beside the raw-SQL wall
  for new panels, linking to Explore with the dashboard prefilled. Edits keep
  SQL-only (Explore cannot reopen a raw-SQL panel).
- **Schedule build targets are picked, not typed from memory**: known pipeline
  outputs render as checkboxes; the free-text box stays for a target authored
  before its pipeline (the save-then-warn banner covers it). The warning
  itself now speaks one word per concept — "no pipeline produces 'x'" instead
  of mixing "transform", "flow" and "pipeline" in one sentence.
- **Explore's save no longer fails with "The query is not finished yet."**
  while a finished-looking (stale) chart is on screen: the Save button
  disables with the actual shaping issue as its tooltip, and the mutation's
  backstop error names the same issue.
- **A dashboard states its zero-step sharing** ("Visible to everyone in this
  workspace, computed with each viewer's own data access") — an indicator
  line only, no sharing controls added.
- **Focus-on-navigate now reaches headings that render after data loads**:
  the effect parks focus on the main region and hands it to the `<h1>` when
  it appears (MutationObserver, never stealing focus the user moved
  themselves). Verified with a CDP probe on a dashboard detail page.
- Retired-vocabulary stragglers: the visual builder's step copy said "Every
  flow begins…" (`views/flow/vocab.ts`, missed by the rename sweep); the
  datasets empty state now also names the in-repo tutorial path so the guided
  path survives an offline install (serving docs in-product is still open).

Known and deliberately unchanged: task #75 (viewer enumerates hidden dataset
names via /builds//lineage//transforms) is still open — re-confirmed present,
not widened, and still a sequencing constraint on any new lineage surface.

### Data health + governance change-approval (two Foundry gaps)

Two governance surfaces Foundry has and Laurelin lacked, built on the records
that already exist rather than a parallel mechanism.

**Data health.** A new `GET /health/datasets` rollup and `GET /health/events`
feed compute each dataset's status — `healthy | stale | failing | overdue |
unknown` — deterministically from builds, build tasks, dataset versions,
expectation results and schedules (`laurelin/core/health.py`). A dead scheduler
is detected at read time by the same `next_run_at` predicate the live one
advances, so a pipeline that silently stopped is visible the moment anyone
looks. Freshness is declared per dataset (`PUT /datasets/{name}/freshness`,
editor-gated). The rollup is **filtered per dataset** through
`viewable_datasets`: a viewer sees only datasets they can already read, statuses
and codes but not editor prose (`message`/`measured` ride in an editor-only
`detail`), and there is deliberately **no unfiltered totals endpoint** — a
global count would leak existence deltas when a hidden dataset flips state.

**Outbound alerting (opt-in, off by default).** One generic JSON webhook, admin
-configured, fired from the scheduler tick on status *transitions* (edge- not
level-triggered). The payload is mechanically `serialize.dump(DatasetHealth)`
at the viewer role — so it can never carry a masked value, a row count, a
`cursor_value`, driver text or editor prose — and the link is a relative path,
never an absolute URL. The webhook URL is a credential: write-only, read back as
`WITHHELD`, and omitted from export archives by the existing `^url$` allowlist.
What it does **not** do: no retries/backoff, no Slack/PagerDuty/email
integrations, no templating, no per-user preferences, no SSRF egress allowlist
(the destination is admin-trusted, by design).

**Governance change-approval.** Every governance write — dataset/ontology
grants, row policy, masks, markings, marking deletion, clearances, group
membership, role changes, workspace membership — now flows through one
chokepoint: a required `ChangeTicket` kwarg on the `MetadataStore` methods
themselves (`laurelin/core/approvals.py`), so REST, MCP, SCIM, CLI and flow
governance all pass the same gate; a route decorator would have missed the
service callers. A comparator (`laurelin/core/policy_diff.py`) classifies each
change as loosening or tightening by evaluating every non-admin user's
capability tuple before and after — exact, because the policy language is
closed. Tightenings apply immediately; loosenings file a proposal record.
Default posture is **record-and-self-approve** (single-admin workspaces never
deadlock, existing routes still return 200); **second-approver mode** is opt-in
and requires the approver to differ from the proposer. What it does **not** do:
no access-requests for non-admins, no approvals for non-governance changes
(schedules, dashboards, pipeline code keep their own ceremonies), and it is
**not** a defense against a local operator with filesystem access to
`metadata.db` — approvals govern the network surface and the honest-operator
record (stated in the module docstring).

The pre-existing `/builds`/`/transforms`/`/lineage` viewer-wide dataset-name
disclosure is **not** fixed here (it is flagged for its own task); the new
health endpoint is filtered precisely so it does not widen it.

### Security: three approval/health findings found by attack and fixed

Found by adversarial review of the change above, each reproduced live before the
fix and pinned by a regression test named for the invariant:

- **Second-approver mode was bypassable via disable → grant → enable (HIGH).**
  The loosening comparator skipped disabled users, so a grant, clearance or
  group-membership naming only a *disabled* account produced zero capability
  gains and classified as a tightening — applying immediately with no queue.
  Re-enabling the account (an ungated identity write) then surfaced the access,
  walking straight past the second approver. Fix: the comparator now classifies
  against a non-admin account's *policy* capability regardless of the disabled
  flag (the flag is reversible state, not a capability), so the loosening queues
  when it is decided (`policy_diff.py` `_candidates`, `_diff_clearances`).
  `tests/test_approvals.py::test_disable_grant_enable_cannot_bypass_second_approver_mode`.
- **Workspace import applied governance loosenings without a second approver
  (MEDIUM).** The import path writes governance rows with raw SQL below the
  ticketed store methods (by design) and its digest-confirm ceremony is a
  one-party act, so under second-approver mode a lone admin could import an
  `everyone can_view can_edit` grant with zero pending proposals. Fix: while
  second-approver mode is armed, an import carrying governance-rule tables is
  refused rather than bypass the queue (`laurelin/export/reader.py`); data and
  inert config still import. `tests/test_approvals.py::test_import_refuses_governance_rules_under_second_approver_mode`.
- **The health-event feed leaked hidden transitions through a global `seq`
  (MEDIUM).** `health_events.seq` is a global monotonic PK; filtering rows by
  dataset *after* it was assigned left gaps a viewer could read — seq 1 and 3
  but not 2 means one transition happened on a dataset she cannot see, timeable
  from the bracketing `at` values, and the absolute max leaked the workspace-
  wide event count. Fix: the route renumbers `seq` to a gap-free per-response
  ordinal after filtering, so the global id never leaves the server
  (`laurelin/api/health_routes.py`). `tests/test_health.py::test_health_event_seq_does_not_leak_hidden_transitions`.

### CI: the workflow now exercises what the badge claims

The workflow in `.github/workflows/ci.yml` had never executed, and its first
draft was a false-confidence machine: no job set `LAURELIN_TEST_S3`, so every
object-store test proven against live MinIO would have skipped green; the
StarRocks job was gated on "workflow_dispatch or a label", which means never;
and no job anywhere executed a single line of UI JavaScript. Every job's steps
have now been executed locally, in order, as written — but **none of this has
run on GitHub Actions yet**. Runner-side facts (the `CI=true` default, Chrome
preinstalled on ubuntu-latest, pull bandwidth for the 1.92 GB StarRocks image)
are from GitHub's documentation, not observation; the first push is the real
measurement.

What each job covers:

- **lint** — ruff 0.9.6 + vermin 1.8.0 over `laurelin/` (1.6.0 crashed on the
  very interpreter the job pins; found by running the step).
- **test** (4-python matrix, 3.11–3.14) — SQLite + PostgreSQL (service
  container) + MinIO (a `docker run` step: a `services:` MinIO starts without
  `server /data`, dies silently, and every S3 test skips green) + embedded
  chdb/ClickHouse + all ten optional extras + the 31 webapp-harness tests,
  which bundle the real `.tsx` sources and execute them under node
  (`npm ci`, 80 MB, ~2 s warm). `tests/test_ci_guards.py` turns each of those
  suites' silent skip into a failure.
- **package** — build the wheel, prove the UI is inside it, install into a
  clean venv, serve, and then *execute* the served page in headless Chrome,
  asserting the React shell mounted. The previous `grep '<title'` check was
  measured to stay green with a bundle corrupted into a guaranteed
  SyntaxError — a completely blank app passed every step of every job.
- **starrocks** (one job, one Python; the image is a 1.92 GB pull, so it
  stays out of the matrix) — runs on push to main, on PRs whose diff touches
  StarRocks code (a `changes` job computes the diff with plain git;
  a broken prepared cursor was measured to merge green under a main-only
  gate), on the `starrocks` label, and on dispatch.
- **docs** — executes the tutorials against a real server, and now also
  validates every `laurelin` command in a `no-run` block against the real
  CLI (a documented flag that does not exist — including on the first
  command a reader ever runs — was measured to stay green forever).
- **bench** — `bench/regression.py` gates the published scaling claims;
  measured 5m25s locally, the second-longest job after the matrix.

Guard changes in `tests/test_ci_guards.py`: new guards for the object-store
suite, the webapp harness, and a pinned manifest of all 82 test files
(deleting `tests/test_markings.py` was measured to leave guards, lint and
collection green — nothing in the pipeline reads test names or counts); the
StarRocks structural guard now also pins the PR path gate. And the suite got
4 minutes faster: `test_throttle_table_is_bounded` was 252 s — 35% of the
whole suite, paid four times per matrix run — because 8,692 authenticate()
calls each paid the real scrypt KDF to test what is actually a dict-eviction
invariant; the KDF is now stubbed in that one test (3.9 s), and the eviction
failure it guards still goes red (re-measured with eviction broken).

### Object-storage ingestion, and the formats real data arrives in

A new `object_store` source type copies bucket objects into a governed managed
dataset — the biggest missing ingestion path for a migration whose customer
data sits in S3. Config is one locator (`uri: s3://bucket/prefix/*.parquet` —
a key, prefix or glob), an optional `endpoint_url` for S3-compatible stores
(MinIO, R2; path-style and SSL are derived), `provider: gcs` for Google Cloud
Storage over HMAC interop, and an `access_key_id`/`secret_access_key` pair
(absent pair = public bucket). Azure is deliberately rejected until it can be
tested; federation already reads Azure-hosted tables in place.

The pull runs on a fresh per-sync DuckDB connection hardened in the
federation order — extensions, a temporary bucket-SCOPEd secret, **both** the
local *and* the raw http(s) filesystems disabled, configuration locked — and
the connection dies with the sync. Disabling `HTTPFileSystem` (not just
`LocalFileSystem`) is defense-in-depth: `s3://` reads go through DuckDB's
S3FileSystem and are unaffected (verified against MinIO), so the
network-enabled sync connection cannot be steered at an arbitrary http(s) host
(incl. cloud metadata at 169.254.169.254) even behind `validate_source`'s
s3://-only guard. The ingest is size-capped at `LAURELIN_MAX_UPLOAD_MB` (the
same ceiling the http puller enforces) so an editor-triggered sync of a huge
object cannot mint an unbounded managed dataset. Build and query connections
keep `enable_external_access=false`, which blocks `s3://` even with a valid
secret loaded (measured), so no other path gains endpoint access. Credentials
inherit the existing allowlist redaction:
key pair masked in every API response, config withheld from editors entirely,
and the export withholds `uri`/`endpoint_url` whole so a re-imported source
refuses its first sync with a re-supply message instead of failing inside
DuckDB.

`mode: append` gets an implicit object cursor: only objects with
`last_modified` above the stored high-water mark are pulled (a metadata-only
listing decides), and a sync that finds nothing new mints no version. The
strict-`>` comparison shares the postgres cursor's same-instant caveat, and an
overwritten object re-enters an append sync — replace-in-place buckets should
use `mode: replace`, where matching zero objects is a 400, never a silent
empty version.

All three file-shaped connectors — `object_store`, `http`, `file` — now also
read **JSON, JSONL/NDJSON and Avro** alongside CSV and Parquet (suffix
inference included), with zero new dependencies. The connector round-trips,
the governance inheritance (versions, markings, ACLs on the ingested
dataset), and the credential-redaction posture are tested against live MinIO
(`LAURELIN_TEST_S3`, skip-with-reason when absent), and the Sources UI grows
the fourth registration form.

### Concurrency: the first real evidence, and the six races it found

Until now every correctness claim in this project was single-user, single-run —
"exactly once across replicas" had never actually seen two replicas. A new
concurrency suite (`tests/test_concurrency.py`, `test_concurrency_exec.py`,
`test_concurrency_coord.py`, sharing `tests/concurrency_harness.py`) forces the
contended interleavings deterministically — barriers that release N threads at
the same instant, pause hooks that park a thread between store calls — and
asserts invariants only: counts, set-equalities, monotonicity. Never a timing.
Every test runs on both SQLite and Postgres, because they serialize differently
and a race impossible on one is routine on the other. Loop-heavy tests carry a
`soak` marker (deliberately **not** deselected by default; deepen with
`LAURELIN_SOAK_ROUNDS`).

The suite found six real defects. All are fixed, and each fix was verified by
reverting it and watching the invariant break for the race itself:

- **Security — a lost update that widened access (Postgres).** All five
  security-list setters (dataset grants, ontology grants, group members,
  clearances, explicit markings) were an unlocked DELETE+INSERT. Two
  concurrent replaces of the same scope ended as the **union** of both lists —
  wider access than either administrator wrote, both told success. Measured
  20/20 rounds before the fix. Every setter now takes a write lock on a
  `scope_locks` anchor row first (the `_lock_index_state` idiom), so replaces
  of one scope serialize.
- **Security — a stale recompute emptied the effective markings (both
  backends).** `recompute_all_markings` read its inputs across many
  transactions and wrote blind-last. A recompute that read before a marking
  change and wrote after that change's own recompute replaced correct
  effective sets with stale, emptier ones — and enforcement treats an empty
  effective set as "no clearance needed", so an uncleared viewer could read a
  classified dataset until the next build. This is not exotic: the builder
  recomputes on **every build**, so any build overlapping an admin's
  `PUT /markings` is this race. Marking-input writers (explicit markings,
  marking deletion, lineage) now bump a generation atomically with their
  change; recompute re-checks that generation under the write lock and starts
  over if it moved.
- **Security — a committed marking denied nobody until its recompute ran
  (both backends).** Enforcement read only the *effective* (inherited) rows,
  and the marking route writes explicit markings and recomputes in two
  transactions — so in the gap, a committed, acknowledged classification was
  enforced against no one. Enforcement now checks explicit ∪ effective in one
  query (`get_enforced_markings`); the union can only deny more, never less.
- **Two replicas recomputing markings crashed one of them (Postgres, 19/20
  rounds).** Both DELETE+INSERTed the same `(dataset, marking, inherited)`
  rows; the loser died on a duplicate key — i.e. one of two concurrent builds
  failed outright. Recompute writes now serialize on the marking anchor row.
- **SCIM PATCH lost member changes while returning 200 (both backends).** The
  route read members, merged in Python, and wrote in a second transaction; two
  overlapping PATCHes each merged onto a stale read. A lost *add*
  under-provisions; a lost *remove* silently **keeps a deprovisioned member**
  in a group that may carry grants — reported to the IdP as success. The
  merge now runs inside one locked store transaction
  (`update_group_members`).
- **A schedule fired twice per window (both backends, deterministic).** A
  replica holding a stale due list could claim a schedule another replica had
  already served — `claim_schedule` re-checked nothing about due-ness and
  `record_schedule_run` released the claim by name alone, so a stale runner
  could also release its *successor's* live claim (same shape in
  `release_build`: a stalled worker's tail-end release re-opened a build
  another replica was executing). The claim now re-checks cron due-ness in
  SQL, the scheduler re-reads and re-verifies due-ness *under the claim*
  before firing, and both releases are fenced to the current owner.

What the same forcing machinery **failed to break**, on both backends, with
100-round soaks: build and schedule claims are exactly-once under 8
simultaneous claimants; concurrent object edits keep gapless seqs and lose no
update; the object-index watermark was never observed ahead of the rows it
certifies and never moved backwards; `catch_up` racing live commits skips
nothing; a policy swap is never seen torn; a grant list is never observed
half-replaced; racing catalog writes all survive with distinct contiguous
versions; first-user creation is single-winner.

Still unproven under concurrency, stated rather than implied: a build whose
transform outlives its lease (the reap/renew/release interplay) has no
harness yet; an index rebuild racing a live commit; the Iceberg write path's
version-row registration; everything about the StarRocks store beyond its
in-memory double. See docs/SCALE.md for the full honest ledger.

### Analyses: the multi-cell governed notebook (Code Workbook parity, no code)

A new **Analyses** surface (`/analyses` in the UI, `/api/v1/analyses` REST):
a saveable, shareable, multi-step analysis document. An analyst adds cells —
each cell is EITHER a governed SQL query (the workbench, inline) OR a
point-and-click shaping step (Explore's card stack) — sees the result table
and an optional chart per cell (the same hand-rolled SVG charts), and a later
shaping cell can take an earlier shaping cell's **output** as its source;
that source picker is the whole chaining UX.

Chaining never materializes an intermediate dataset. Per run or preview the
server synthesizes ONE FlowDef from the target cell's ancestor closure
(cells' step ids namespaced `{cell}_{step}`, cross-cell edges rewritten to
the upstream cell's terminal) and compiles it through the one Flow compiler
— so the entire chain executes as a single parameter-bound statement through
`_execute_sql` **as the caller**, under that caller's ACL / row policy /
masks in one policy pass. Two viewers get different rows from the same chain;
a chained cell cannot show a viewer rows only the author's policy would have
allowed; no result is ever persisted (a cached result would be a silent RLS
bypass). SQL cells are non-chainable in both directions — the IR is closed
to raw SQL by design, and both bridging mechanisms measurably fail (DuckDB
refuses parameters in views; textual composition misaligns ordinals).

Sharing is the dashboard model, R2 included: a viewer's cell arrives as
exactly `{id, title, chart, x, y, series, stacked, width}` plus rows from
`POST /analyses/{name}/cells/{id}/run`; `sql`, `flow`, `inputs` and `top`
are withheld, and run errors are laundered through
`stored_instruction_error`. The whole-record and per-cell PUTs merge every
absent field — both halves, instruction and presentation — from the stored
record: absence is "unchanged", never "blank it", so a round-tripping client
cannot blank an instruction it was never shown, and a reorder PUT of bare
`{"id": …}` cells keeps titles and chart bindings too (measured before the
rule covered presentation: that PUT silently reset both). Editor-facing
compiler refusals are rewritten **server-side** into the vocabulary the
product speaks — "Cell 2 ('Revenue by region')'s Summarise card refers to a
column named 'amount'…", never "Step 'c2_a1'" — so scripts and MCP agents
hear the same sentences the bundled UI shows. There is deliberately **no
code cell** — that is the RCE surface `--lock-pipelines` exists to close.
`tests/test_analyses.py` states the invariants; the audience sweep covers
the new routes automatically.

### MCP authoring surface: agents can now build a workspace, not just read one

The MCP server grows from 18 read-mostly tools to a full authoring surface —
ontology definitions (`put_object_type` / `put_link_type` / `put_action_type`
and their deletes, backed by a new admin-gated REST surface that writes
managed YAML files into the ontology directory), sources, datasets, no-code
flows (`preview_flow` / `write_flow` / `delete_flow`), dashboards, schedules,
and governance (markings, clearances, dataset/object-type grants, row
policies, column masks, users, groups). Every tool body is a single
authenticated call to the same REST route the UI uses, so role gates, lock
flags (`--lock-flows` binds MCP flow authoring; there is deliberately **no**
Python-pipeline authoring tool), server-side author stamping, build-time
author entitlements, audience-gated serialization and audit all run once, in
the route — pinned by an AST test that allows `laurelin/mcp/server.py` no
laurelin import beyond the client. Bulk row data stays off MCP by design:
rows arrive through sources, uploads or builds.

Governance is also *readable* over MCP, not write-only: `list_dataset_markings`
(explicit + effective per dataset — how you see what a marking propagated),
`list_dataset_grants`, `list_dataset_policies`, `list_object_type_grants` and
`get_user_clearances` mirror the admin REST reads, so an agent can verify the
governance it set through the same surface it set it with.

New: `docs/MIGRATING-FROM-FOUNDRY.md`, a migration runbook written for an
agent — concept map, ordered playbook with per-phase verification, the full
Flow IR parameter reference, decision trees for the lossy translations, and a
failure-modes table whose claims are pinned by `tests/test_migration_guide.py`.

Hardening from the red-team pass on this surface:

- `PUT /flows/{name}` refused to check the *kind* of a same-named producer: a
  flow saved under the name of an existing **Python transform** passed the
  duplicate-producer guard, saved, and broke `collect_transforms` for the
  whole workspace (every graph-touching route 409ing) until the flow file was
  deleted. Both the same-output and the same-name-different-output collisions
  are now a 409 at save, naming the code transform and its pipeline file.
- `PUT /datasets/{name}/policy` now warns, at save time, when a row policy
  lands on a dataset any transform reads — flow governance refuses
  row-policied sources fail-closed, so those flows stop building and editing
  the moment the policy lands, and the breakage otherwise surfaced only on
  the next scheduled build. The response's `warnings` names the affected
  transforms.
- `PUT /schedules/{name}` now warns about referents that do not exist (a
  `sync` source that is not registered, a `build` target no transform
  produces, an `upstream` dataset that does not exist) instead of saving
  silently and failing when the schedule fires. Warnings, not refusals: a
  schedule may be authored before its flow, but never silently.

### Security: API-authored Python transforms no longer launder ACLs, row policies or column masks

An editor who could not view a dataset could save `@sql_transform(query=
"SELECT * FROM s")` through the API, build it, and read every row of the copy
— unfiltered, unmasked, world-readable. Closed at the build, not the route:
the three pipeline-writing routes (`PUT /pipelines/{name}`,
`POST /pipelines/from-query`, `POST /flows/{name}/eject`) now record the
saving user server-side (`pipeline_authors`, never taken from the body —
the same contract as a flow's author), and the Builder refuses any python/sql
task whose recorded author cannot read every input **in full**: view rights,
no row policy, and no column mask, resolved with the same author semantics as
flows (deleted author refuses; a zero-user workspace is exempt). The check
binds to the recorded author and never the triggering principal, so an admin
pressing "build", the scheduler, and the CLI all enforce it identically.

Per its own docstring's instruction, the pinning test
`test_the_python_transform_path_still_launders_acls_row_policies_and_column_masks`
has been **deleted** — the gap it documented is closed — and replaced by
`tests/test_build_governance.py`, which asserts the laundering is refused and
that legitimate pipelines still build.

**Behaviour change, deliberate and not opt-in:** an API-authored pipeline
refuses to build when an input carries a row policy or column mask **that
applies to its recorded author** — one that filters the author's rows, or
masks a column from them. A Python transform's touched columns are unknowable,
so *any* mask that applies to the author refuses where a flow could drop the
masked column; the remedy is in the refusal (author it as a flow, or re-save
it as someone entitled). The check is about *applicability*, not the mere
presence of a policy: an admin (who bypasses every policy) and an author
explicitly exempt from a mask read the input in full and build — so an admin's
re-save is the recovery path, and a routine re-save of a pipeline over a
dataset whose policy does not touch its author is not turned into a refused
build. It is resolved through `PermissionService.decide()`, the same resolver
the read path uses, so it cannot disagree with what an actual read would show.

Pipelines authored on disk (git, import, CLI) have no recorded author and
build unchecked — operator-trusted, exactly as before, and now pinned by a
test. Existing API-saved files from before this change are unstamped and
therefore also unchanged until re-saved. A `--no-auth` server records no
author (every request is the implicit admin, not a real user), so its files
stay operator-trusted and build — a workspace that carries real users (an
imported one, or an authed workspace restarted with `--no-auth`) is no longer
made unbuildable by a stamped `anonymous`. The `pipeline_authors` table travels
in workspace export/import; an imported author who does not exist at the
destination refuses the build with the reassignment remedy, as flows already
did.

**Operational note:** because a build is checked against its recorded author,
deleting an author account (offboarding) makes that author's API-saved
pipelines refuse until an admin re-saves them or reassigns them. Disk-managed
files are unaffected. This is the fail-closed direction — a build must not run
as nobody — but it is a change from the pre-item-58 world where builds carried
no identity.

**Out of scope, unchanged:** a pipeline function body is arbitrary Python
`exec`'d as the server process, so it can read any dataset on disk regardless
of its declared inputs — this is the remote-code-execution surface that
`--lock-pipelines` exists to close (see SECURITY.md), not something the
input-entitlement check claims to. Item 58 closes laundering through the
governed build seam; item 59's lock closes the code execution beneath it.

### Changed: `--lock-pipelines` locks code, not clicks — Flows stay authorable; new `--lock-flows`

There is now a safe production posture. `--lock-pipelines` /
`LAURELIN_LOCK_PIPELINES=1` no longer refuses `PUT`/`DELETE /flows/{name}`:
the flag's contract — stated in its own help text since it shipped — is that
writing a pipeline file is code-execution-equivalent, and a flow is not code.
It compiles to parameterised SQL (every value bound, every identifier checked
against the live schema), is refused if it would launder a mask, and is
re-checked against its recorded author at every build. Locking both behind one
flag meant a hardened server had no authoring path at all, so the flag went
unset and every editor kept remote code execution. The intended deployment is
now expressible: **Python locked, Flows and Explore open, eject locked**
(`POST /flows/{name}/eject` writes a `.py` and still refuses under
`--lock-pipelines`; it also refuses under `--lock-flows`, since it consumes a
flow file). Workspace import and the imported-pipelines acknowledgement gate
stay behind `--lock-pipelines` — an archive carries `pipelines/*.py`.

**Upgrade note for hardened operators:** if you run `--lock-pipelines` and
relied on it also freezing flow authoring, that widened on this upgrade — set
the new `--lock-flows` / `LAURELIN_LOCK_FLOWS=1` alongside it to keep exactly
the old total lockdown. Dashboard raw-SQL panels were never covered by any
lock (they execute as the calling viewer under row policies and masks) and
remain out of scope of both flags.

`GET /auth/status` now reports
`"authoring": {"pipelines_locked", "flows_locked"}`, and the UI says the
posture up front instead of letting an author discover it as a 403 on save:
Transforms/Pipeline show a "Python authoring is locked — Flows and Explore
remain available" notice, the Workbench's "save as transform" says whether it
was the role or the lock, Flows disables eject with the reason, and a
`--lock-flows` server banners the Flows screen pre-emptively. A structural
test walks the app's dependency trees and pins the exact guarded route set for
both flags, so silently dropping a guard fails loudly.

### Added: Explore — point-and-click data-to-chart

The Contour/Quiver half of the product. An analyst who cannot write SQL picks a
dataset or an object type, shapes it by clicking (filter with value
suggestions, group by — including date buckets, "read as dates" for text
timestamp columns, and numeric bins — summarise, order, top-N), watches the
chart update live, and saves it to a dashboard. New chart kinds: pie and
scatter; bar/line/area gained split-by-series and stacked bars.

Explore has no query representation of its own: the screen synthesizes a
`FlowDef` and everything below that seam is the Flow stack — one compiler,
every value bound, every identifier checked against the live schema, preview
under the caller's own ACL/row policy/masks. A saved panel stores the flow
(withheld from viewers exactly like `sql`) and re-runs per viewer.
`POST /explore/preview` skips exactly one Flow check — the materialization
guard — because Explore materializes nothing; that divergence is pinned by a
test so it cannot be rediscovered as a bug. Deliberately absent: heatmaps,
dual axes, KPI deltas, maps, percentiles beyond median, viewer-facing Explore.

### Fixed: charts that stopped telling the truth — a review pass

An attacker-style pass rendered the real `charts.tsx` against adversarial
data and drove the Explore screen end to end. Every item below was reproduced
before it was changed and has a regression test that fails when the fix is
reverted (`tests/test_charts_render.py` renders the shipped component under
node; no chart claim is left untested because "it's frontend").

- **NULL was drawn as a measured zero** — a revenue line plunged to the
  baseline for a month with no data, the tooltip asserting "0"; bars drew
  0.5px zero-bars for NULLs the table showed as blank. NULL now renders as a
  gap, counted in a visible "n missing values shown as gaps, not zero" note.
- **The series pivot could falsify a trend**: rows sorted by (series, x)
  rendered a strictly rising series as a peak-and-decline because x labels
  were ordered by first encounter. The pivot now merges each series' own
  order (topological), falling back to encounter order only on conflict.
- **Histogram bins were index-spaced**, so eight empty bins' worth of gap drew
  identically to one bin's width; bins also rendered in arbitrary order by
  default. Numeric x axes now sort ascending and materialize empty grid
  positions as visible empty width (never as fabricated zero marks), and
  choosing a bin or date bucket defaults the order to ascending, visibly.
- **Small numbers rounded to lies**: a stat panel showed "0" for 0.004; four
  distinct nonzero gridlines all labeled "0.00". Formatting now derives
  decimals from the tick step and switches small KPIs to significant digits;
  billions get a "B" tier instead of "3900.0M".
- **Scatter hid structure**: forced zero-anchoring collapsed a tight cluster
  into less than one pixel; NULL rows vanished uncounted; tooltips omitted
  which category a point was. Axes now fit the data, skipped rows are
  counted, and the first categorical column names each point.
- **Assorted honesty fixes**: grouped bars can no longer overflow into the
  neighbouring group's band; pie color cycling can't give the closing slice
  the first slice's color; 100+-slice pies fold their tail into "other";
  midnight timestamps label as dates; truncated labels stay distinguishable;
  binding inference scans past a NULL in row 0; wildly mismatched measure
  scales get a visible warning instead of an invisible series.

And on the Explore screen itself: "Edit in Explore" on a raw-SQL panel no
longer white-screens the app (and the button no longer appears on SQL
panels); text-typed timestamp columns can be bucketed by month via a
synthesized cast — "read as dates" — instead of silently offering nothing;
compiler refusals are rewritten into the screen's own card vocabulary and the
two easiest ways to trigger them (duplicate group columns, punctuation in a
summary name) are refused in plain English before any preview fires; shaping
state survives a reload (sessionStorage); the sort direction defaults by what
the column is, so a fresh time series never runs backwards; filter values get
suggestions drawn through the same governed preview path, and an empty result
behind a filter says "no rows matched" instead of looking like empty data;
the object path's pickers grey out properties your masks cover (the aggregate
API now reports `masked_properties`) instead of offering a chart whose only
bar is `"***"`.

### Fixed: Flows — a review pass, and what it found

Every item below was reproduced end to end before it was changed, and each has a
regression test that fails when the fix is reverted.

**A masked column could be read through a name the database could not tell
apart.** The compiler compared column names with Python `in` (case-sensitive)
while DuckDB resolves identifiers case-insensitively and binds the first match.
So `derive PAY = 'x'` then `select keep [PAY]`, over a dataset with a redact
mask on `pay`, passed every governance check — `referenced_columns` saw `PAY`,
the mask named `pay` — and published real salaries into a world-readable
dataset. Two dropdowns, no hostile string, no admin involved. Identifier
collisions are now folded with `permissions.confusable_identifier`, the same
function `_reject_case_mismatch` uses, so the compiler and the mask checker
cannot hold two opinions of "the same name"; and a schema holding two confusable
names is refused rather than guessed at. The same defect let a data supplier
choose which row a `dedupe` kept, by shipping a column called `_Laurelin_Rn`.

**The mask check itself disagreed with the policy resolver.** It compared
`mask.column in touched`, where `permissions.py` compares under NFKC + strip +
casefold precisely so a mask spelled `SSN` against a column `ssn` fails closed.
Measured: with such a near-miss spelling nobody but an admin could read the
dataset at all, and a one-step flow copied it out verbatim.

**`POST /flows/{name}/eject` was a one-click way out of every flow protection.**
It ran `check_flow_sources` alone. So a flow the platform refuses to save *and*
refuses to build — masked column, row-policied source — ejected happily, and
the resulting Python transform read the data in plaintext; and a flow that
promised `output_will_be_restricted: true` produced a dataset with no grants at
all. Eject now runs the full `check_flow_governance` and applies the author
restriction itself, since after ejecting there is no flow left for the Builder
to apply it for.

**A flow's output dataset was never authorized.** A flow's name *is* its output
dataset's name, so claiming somebody else's dataset was one text field: an
editor overwrote a dataset she could not read. The same gap laundered
classification markings — an uncleared editor could not name a marked dataset as
a source, but could edit a flow that read one to read a public dataset instead,
which replaced the lineage edge and declassified every downstream dataset while
the classified rows stayed in them. The author must now be able to view and edit
the output dataset when it already exists.

**"Derived from restricted ⇒ restricted to the author" never fired for a flow
pointed at an already-shared dataset.** `restrict_output_to_author` leaves
existing grants alone, deliberately, so an administrator's widening survives a
rebuild — but it read *any* pre-existing grant as that decision, including the
ordinary ACL of whatever dataset the author chose to overwrite. Choosing an
output shared more widely than a restricted source is now refused at authoring
time; widening a flow's own output afterwards is still an administrator's call.

**`GET /flows` and `GET /flows/{name}` disclosed the IR to anyone with the
editor role.** `GET /flows/{name}/sql` withholds the bound values on purpose —
"a filter constant can be a customer name" — and its sibling read routes handed
over the source names, the column names and the values. Both are now gated on
view rights for every source.

**A self-referential flow saved happily and wedged every build in the
workspace**: the cycle was only found in `Builder.plan`, which runs for *every*
build, so `POST /builds` with no targets — the scheduler's path — 400'd until
somebody found and deleted the flow. `PUT` now walks `registry.by_output` and
refuses.

**A join whose two inputs were the same step** compiled to a duplicate-alias
self-join and reached the author as DuckDB's `Ambiguous reference to table`.

### Fixed: Flows — the parts that made it unusable

A compiler that is perfectly safe and unusable by analysts has missed the point
of the feature. These were measured by driving the real screen.

**Filtering on a date lost the flow.** `_coerce_literal` returns a real
`datetime.date`; `as_json()` passed it to `json.dumps`; the `TypeError` was not
a `FlowRefused`, so `PUT /flows/{name}` returned **500** and the screen said
"Error 500: Internal Server Error". `POST /flows/preview` returned 200 with
correct rows for the same literal, so the step visibly worked and then Save
destroyed it, naming no control. Dates and timestamps now round-trip through the
file as ISO 8601.

**Type mistakes — the commonest thing an analyst does — had no route to
discovery.** Asking for the total of a column of text saved cleanly (`GET
/flows/{name}` reported `error: null`), previewed as *"A column referenced does
not exist on the remote system"* — false in three ways at once, about a column
visibly present in the picker below — and then failed its build as *"Laurelin's
own code raised … the traceback is in the server log"*, to a person with neither
code nor a server. Three changes: the IR now carries a coarse type per column
(number / text / true-false / date-or-time) and refuses the mistake at save time
naming the column and the remedy; the "Total of" and "Average of" pickers offer
only columns that hold numbers; and `_task_failure` classifies DuckDB errors as
DuckDB's — it compared `type(exc).__module__` against `"duckdb"`, but DuckDB
defines its exceptions in `_duckdb`, so every one of them was recorded as
Laurelin's own code raising. The failure templates for a *query* no longer claim
a remote system either: the engine is embedded DuckDB as often as it is a
federated cluster.

**The preview was not honest about being a sample.** `truncated` was
structurally always false — the SQL `LIMIT` and the fetch used the same number —
so the panel read "50 rows · 7 columns" for a 56-row dataset and "200 rows ·
2 columns" for a 5,000-group aggregate, with the number looking like the size of
the answer. The banner also quoted 200 while the panel requested 50.

**"is not" silently dropped empty rows.** Over a column holding
`[null, null, null, 5]`, *"keep rows where v is not 5"* returned nothing:
correct SQL, and the opposite of what the words on the screen mean to somebody
who does not write SQL. `is not` and `is not one of` now keep empty values —
`IS DISTINCT FROM` and `NOT coalesce(… IN …, false)` — and say so under the
picker. `is` is unchanged.

**Ejecting was refused for any flow containing a filter value**, which is to say
every flow that filters anything, with the whole explanation in a tooltip on a
disabled button. `sql_transform` now takes a `params` list bound to the query's
placeholders, so an ejected pipeline binds exactly what the flow bound and
nothing is written into the SQL text. This makes the *Python* path able to bind
values too.

**A flow over a federated / ClickHouse / StarRocks / Iceberg dataset could never
be previewed**, and the error said the table did not exist — immediately after
`GET /flows/schema` had listed its columns. `catalog.query` skips registering a
source-scanned dataset unless the ad-hoc workbench is enabled; a compiled flow
is server-authored SQL over a closed IR, which is the case that gate's own
docstring carves out, so it is now registered for a preview without opening the
workbench to ad-hoc queries.

**A join was refused over columns the pipeline never uses.** This repo's own
demo pipeline, `clean_flights` joined to `clean_aircraft` on `tail_number`, was
refused over `status` — and told to *rename* it, which produces a worse result
than dropping it, one collision per round trip. The refusal now names every
clashing column and offers "Choose columns" first.

### Added: the Flows screen — pipelines without code

The builder itself, at **Flows** in the sidebar, above Transforms: for this
feature the screen *is* the product, and a no-code backend behind a JSON editor
would have served nobody.

A flow is a vertical list of step cards — *Start from a dataset*, *Filter rows*,
*Combine with another dataset*, *Group and summarise*, *Add a column* — never
`WHERE`, `JOIN` or `GROUP BY`. Every control is a `<select>` over a closed
vocabulary or a column picked from the live schema; the condition and formula
editors are nested dropdowns, so the "no free-text SQL anywhere" property the
compiler guarantees is one the UI cannot violate either. A `join`'s second input
is drawn as its own chain inside the card that consumes it, with the same
add/remove affordances, because the server's advice for a column collision names
a step to add to one side and the screen has to offer that gesture.

A **preview runs on every committed change**, debounced, cancellable, capped at
50 rows, and pinned under a banner saying what it is: *"Preview runs as you.
Your row and column policy is applied. The build runs as the system and will see
at least as many rows."* Where a source has a masked column, the panel
distinguishes a mask you are looking at from one the flow does not include.

Refusals are attached to the step that caused them. `FlowRefused` names a step
by its internal id and a remedy by its node kind; the screen rewrites both into
what the author sees — *"Step 3 (Combine with another dataset) … add a “Choose
columns” step"* — and lights up that card.

The relationship to code is stated in both directions. **Open in Python…** is a
type-to-confirm modal that says ejecting is one-way before the click. **Show
SQL** is a read-only `<pre>`. The Transforms screen now points back: *"Not a Python
programmer? Flows builds the same kind of transform step by step."* There is no
**Rename** — lineage is keyed on the name — so **Duplicate…** is offered in its
place and says why.

### Added: no-code pipelines ("Flows") — backend

Laurelin's Transforms screen is a Python editor. Anyone who cannot write Python
could not author a transform at all, which excludes most of the people the tool
exists to serve. A *flow* is a declarative pipeline — pick a dataset, filter,
join, group, sort — stored as `pipelines/<name>.flow.json` and compiled to SQL
in memory.

It compiles onto the **existing** build path rather than beside it.
`collect_transforms` puts flows into the same `TransformRegistry` as
`pipelines/*.py`, so duplicate-output detection, planning, cycle detection,
lineage, marking propagation, expectations-before-publish, build leases,
scheduler targets, the imported-pipelines acknowledgement gate and
`--lock-pipelines` all cover flows without knowing they exist. A flow appears in
`GET /transforms` and `GET /lineage` as a transform with `kind: "flow"`.

**No value an author supplies is ever rendered into SQL.** Every filter
constant, formula literal, `IN` element and `LIKE` pattern is *bound* as a query
parameter; every column and dataset name is checked for membership in the live
schema before it is quoted; everything else — cast targets, aggregate functions,
join types, sort directions — comes from a closed enum the compiler already
contains. There is no free-text SQL or expression box anywhere in a flow, at any
nesting depth. `laurelin/transforms/flow_compile.py` contains no function that
converts a value to SQL text at all, which is a stronger property than "our
escaper is correct". The invariant is asserted directly: two flows differing
only in their literal values compile to *byte-identical* SQL.

Flows are also **stricter than the Python transform path, deliberately.** A
flow's sources must be viewable by its recorded author (re-checked at build
time, since a scheduled build has no request user); a source carrying a row
policy is refused outright; a source with a column mask on a column the flow
reads or emits is refused naming the column; and a flow over a restricted input
produces an output granted to its author alone, never the union of the inputs'
grants. The Python path still launders all three — that is unchanged here, and
is now pinned by a test that states it rather than left to be discovered.

### Fixed: `generate_sql_transform` corrupted the SQL it was given

Two defects, one loud and one silent, both measured:

* SQL containing `"""` produced a file that would not parse, and the author saw
  `Syntax error: unterminated triple-quoted string literal … (outa.py, line 9)`
  — a Python line number for a file they never saw.
* SQL containing a backslash was **silently corrupted**. An authored
  `WHERE path = 'C:\temp\new'` came back out of `collect_transforms` holding a
  real TAB and a real NEWLINE. The file was written, it compiled, the build ran,
  and every layer reported success while executing SQL nobody wrote.

`write()`'s compile-before-save guard catches the first and cannot catch the
second. The query is now rendered with `repr` per line, which round-trips every
input byte for byte and keeps a multi-line query readable in the editor.

`POST /pipelines/from-query` also now filters the candidate dataset list through
the caller's view rights before the input-inference regex runs, so a generated
pipeline can no longer declare an input its author cannot view.

### Changed (behaviour): builds can no longer read the server's filesystem

**This may break an existing `@sql_transform` that reads a file or a URL through
DuckDB.** The build's DuckDB connection, and the expectation validator's, now
issue `SET enable_external_access=false` — which `catalog.query`, `catalog.read`
and both ontology query paths already did. The two build connections were the
only ones that did not.

Measured before the change: a `kind="sql"` transform running
`SELECT (SELECT a FROM read_csv_auto('<path>') LIMIT 1) AS stolen` built
successfully and published the file's contents as a governed dataset, with
lineage claiming it came from nowhere.

A Python transform that wants a file uses pyarrow, which this does not touch,
and every managed / object-store / federated / Iceberg input arrives at the
build connection as an already-registered Arrow object, so DuckDB performs no
I/O of its own there.

### Fixed: `accepted_values` escaped its values instead of binding them

`expectations.accepted_values` built its `IN` list by doubling quotes — a second
SQL-generation surface with its own escaper, evaluated on the build connection.
`Expectation` now carries `params` and the values are bound.

### Fixed: an incremental non-Python transform crashed the build

`incremental=True` with `kind != "python"` reached `spec.fn(**{param: delta})`
with `fn=None` and died as `TypeError: 'NoneType' object is not callable`. The
decorators cannot produce that combination, so it was unreachable until specs
could come from somewhere other than a decorator. `TransformRegistry.register`
now refuses it, for every producer.

### Added: `DatasetCatalog.column_names`

There was no accessor covering both dataset kinds. `GET /datasets/{name}/schema`
resolves through the *version* row, and a source-scanned dataset (federated,
ClickHouse, StarRocks, Iceberg) has none — so that route answered "has no
versions" for exactly the datasets a remotely-backed flow needs to read.

### Added: the ontology edit log can be pruned, and says what it costs first

`object_edits` was never trimmed, so a workspace using the ontology as an
application database accumulated edits forever: disk grew without limit and
every rebuild replayed more. `prune_object_edits` had been specified and never
built, and nothing in the product said how large the log was.

The log is truth, so pruning is a proof obligation discharged per edit rather
than a retention policy applied to a table. An edit may go only when it is
folded; the version it was folded into is still in the backing dataset's
history *and* is a `writeback` version (which is what ties it to this dataset
rather than to a dataset the type used to be bound to); no version written
since could have superseded the fold (only `writeback` and `compact` carry one
forward — a transform build, upload, sync or merge may have overwritten it, and
then those log rows are the only surviving record of the hand edits); it is not
the row holding `MAX(edit_seq)`, which the sequence allocator and every
materialization watermark depend on; and it is outside the operator's retention
window. The last two are re-checked in SQL inside the deleting transaction, so
a stale plan deletes fewer rows rather than the wrong ones.

`GET /ontology/object-types/{name}/edit-log` reports the size and what pruning
would reclaim — plus, for every retained edit, the reason it stayed, because
"less than you expected" is a question a number alone cannot answer. The
ontology page shows all of it. Pruning itself is ADMIN, a rank above the fold
that made the edits redundant: it deletes the record of who changed what.
Automatic pruning after a fold is off unless `LAURELIN_EDIT_LOG_MAX_FOLDED` is
set, and a failed prune never fails the fold.

Reported size is the stored JSON payloads only — a floor on what the log
occupies, not a measurement of the database file, and it says so where it is
shown.

### Fixed: `compact()` corrupted Iceberg datasets instead of compacting them

Pre-existing, and silent. `write()` is exempted from the scanned-at-source
refusal for Iceberg — Laurelin really does write that table, via
`write_iceberg` — so compaction took the managed path: it wrote a local Parquet
part nothing ever reads and registered a version row with `snapshot_id = NULL`.
The Iceberg table was untouched (the audit reported `parts_before: 0` on a
two-file table), and a version with no snapshot pinned reads as *the table as it
is now*, forever. Measured: compact, then append, and time travel to the
compacted version returned five rows where its own version row said four.

Compaction now rewrites the table into one new snapshot through
`write_iceberg`, so the version pins the snapshot it made and the data files
actually merge (2 → 1, audited both ways). It reclaims scan cost, not disk:
earlier snapshots keep their files, which is what keeps history readable. The
Compact button, previously hidden for Iceberg datasets, now sits in the
snapshot-history panel where it belongs.

### Performance: the audience projection cost 3–11× per response, and the published numbers were re-measured

The security work below put a per-field disclosure decision on **every**
serialized response, and nothing measured it: `bench/benchmark.py` exercises the
service layer and never goes through serialization, so a large regression sat on
the busiest path in the product with no number attached to it. Measured against
the pre-security tree, benchmarked back to back: a 1 000-dataset response cost
**5.2×** what it had, and a 100-dashboard response **11.3×**.

Profiling rather than guessing put **68%** of the time in re-deciding per field
what depends only on the *shape* — `field_author_role` rescanning
`field.metadata`, pydantic's `model_fields` property re-entered once per field
per record — against **7%** in actual pydantic serialization. That decision is
now memoized on `(class, reader role, author role, narrowed)`, which is
everything it depends on and no field value, and `_holds_model` gained an
exact-type fast path. Neither changes a disclosure decision, and
`tests/test_audience.py` asserts the memoized answer against the uncached rules
rather than trusting that sentence.

**What is left is published, not hidden.** A viewer's response now costs ~1.2×
what it did and an admin's 2.9–4.9×, and an admin pays *more* than a viewer
because a viewer's projection drops most fields before they are walked. Roughly
half the residual `DatasetInfo` cost is `redacted_source` (0.90 → 3.55 ms per
1 000), which its own docstring says is a courtesy and explicitly not a
boundary — the obvious next cut, and not cut. Two other costs bought
correctness and are stated where they are relevant rather than only here: a
policied overlay read with pending edits is **1.18×** (the fix for the
mask-exempt-editor plaintext read), and the rest of the service layer — query,
ingest, build, incremental, ontology — is unchanged at 0.85–1.13×.

`bench/serialize_cost.py` is new, and exists so this table cannot rot the way
the index table did; `tests/test_bench.py` smoke-tests it for the same reason it
smoke-tests `benchmark.py`. All six ratio claims in `bench/regression.py` still
pass.

Re-measuring also caught **numbers that had already rotted, before this
session**, now corrected in [docs/SCALE.md](docs/SCALE.md): the UI row page was
published as flat at 15 ms and is 50/73 ms at 1 M/5 M (it was never flat — the
pre-security tree measures 45/86 ms); ontology get-by-key was published at
75/279 ms and is 107/334 ms (the pre-security tree is *slower* at 117/345 ms).
The object-index table was reported from a script **that was never committed**,
so it could not be re-run at all; it now carries what a committed harness
measures, and the "selective search is constant-time" claim is **withdrawn**
rather than restated, because the probe available matches a fixed *fraction* of
the type and so cannot test it. The README's headline 1.4 ms key lookup is
true: 1.3 ms at both 200 K and 800 K.

### Security — each entry below was reproduced on a running server before it was fixed

Laurelin is a governance product, so the entries below are the ones that matter
most: each was a live disclosure or a live bypass on a running server, each was
reproduced before it was fixed, and each fix was reverted and watched to fail
before being restored.

### Changed: credentials are no longer detected in free text — the detector stopped being a boundary

Three adversarial passes found 22, then 17, then 18 confirmed defects, and the
credential-redaction module produced criticals in **all three**. Round 1:
three redactors, three different guesses about where a credential lives. Round
2: driver exceptions stored verbatim reached a VIEWER via `GET /audit`. Round 3:
the same leak on the scheduler path nobody had checked, plus
`credential_in_free_text` only understanding `://` — so libpq conninfo, ODBC
keyword strings and DuckDB `CREATE SECRET` bodies saved with 200 and a viewer
read them off `GET /dashboards`.

Each round widened a matcher and each round something walked around it, because
**finding a credential inside free text is not decidable**. This release stops
trying. Two rules replace it.

**R1 — third-party driver text is never persisted.** Every place a driver or
library exception is caught now converts it *at the catch site* into a
`Failure` (`laurelin/core/failure.py`): a code from a closed enum, a phase from
a closed enum, a subject in Laurelin's own namespace, a `host:port` rebuilt
from Laurelin's own parse of its own config, integer counters, and a
`detail_ref`. The only driver-derived fields are the exception's class name and
its vendor code, both gated on the shape of a *Python identifier* — which no
conninfo, ODBC string, JDBC URL, `CREATE SECRET` body or PEM block can satisfy.
That is decidable; "does this contain a credential" is not. The driver's own
words go to the server log and nowhere else.

Because psycopg reports `sqlstate=None` on **every** connect failure (measured:
wrong password, space in password, unknown database, bad host, refused port),
Laurelin runs its own DNS lookup and TCP connect on the failure path to
distinguish unresolvable / unreachable / timed out / rejected. Those are
first-party facts from the stdlib, not a reading of somebody else's prose.

**R2 — author-written free text is readable only at the privilege level that
could author it.** Fields now declare an audience
(`laurelin/core/audience.py`), enforced at the single serialization point
(`laurelin/core/serialize.py`). Anything not explicitly `PRESENTATION` is
withheld from readers below the record's authoring role, so a field added
tomorrow fails closed, and a model added tomorrow is admin-only. A field whose
*writer* sits above its record — `DatasetInfo.source`, written only by the three
admin registration routes, inside an editor-authored dataset — says so with
`AuthoredBy`, and descending into a nested record can only ever disclose less.

**The viewer's dashboard still works, and this is the part that made R2
affordable.** A viewer no longer receives panel SQL — they receive the rows.
`POST /dashboards/{name}/panels/{panel_id}/run` executes the stored panel
server-side **as the caller**, applying that caller's ACL, row-level security
and column masking. The old invariant holds for a new reason: a stored
dashboard still grants nobody new read access, because the server rather than
the browser is now the thing running the query.

**Breaking.**

- `GET /audit` is now EDITOR-gated. New `GET /audit/mine` (VIEWER) returns the
  caller's own rows — their headers always, their `details` only at the level
  the row's writer declared. `audit_log` gains `min_read_role`, default `admin`,
  which is both the row filter on `/audit` and the record's author role in the
  serializer, so a new `log_audit` call site discloses to nobody below admin
  until its writer says otherwise. `action_applied` no longer records `parameters` — a viewer
  holding a **403** on an object type was reading that object's property values
  out of the audit trail.
- `GET /pipelines` and `GET /pipelines/{name}` are now EDITOR-gated. They return
  `exec`-ed Python. A viewer's lineage need is served by `GET /transforms` and
  `GET /lineage`, which stay VIEWER.
- **A data-destroying migration.** `builds.error`, `build_tasks.error`,
  `sources.last_sync_error` and `schedules.last_error` are set to NULL and
  replaced by `*_failure_json`. Those columns hold prose of unknown provenance
  that can never be re-classified, they are *known* to contain live credentials,
  and they sit in a file whose permissions were themselves a shipped bug. What
  is lost is diagnostics for builds that already finished; the migration logs
  the row counts it cleared.
- `GET /workspace` returns `root` only to an admin. `GET /health/ready` returns
  `{"status": "unavailable"}` with no detail — it was handing an anonymous
  caller 120 characters of whatever the store driver said.
- `GET /datasets` and `GET /datasets/{name}` return `source` to an admin only,
  and everyone else gets `source_descriptor` — which table, in which format,
  built from an allowlist. `GET /sources` omits `config` entirely below admin. A source is
  admin-authored and editor-read, so the crossing runs through the middle of the
  record: an editor keeps the Laurelin-owned facts — name, connector kind,
  target dataset, last sync time and status, and a structured `Failure` when it
  went wrong — and the connection config is not disclosed at all. Not a better
  denylist over somebody else's config vocabulary; the absence of one. The
  Sources table says "admin only" in that column rather than drawing a blank,
  which reads as "no source configured" and ends with someone retyping a DSN.
- The credential gates on `PUT /dashboards/{name}` and `PUT /schedules/{name}`
  are now non-blocking **warnings** in the response body rather than a 400. The
  matcher is an authoring hint; nothing's confidentiality depends on it being
  right, so being wrong should cost an editor a banner, not a legitimate save.

`tests/test_redaction.py::test_the_authoring_hint_is_not_load_bearing` deletes
the matcher — monkeypatching `credential_in_free_text` to `lambda v: False` —
and re-runs the leak battery. It passes.

See also `SECURITY.md`: the server log is now the one place driver text lives,
which makes your log sink's readership a deployment decision.

**The UI, and three defects that only appeared once it was driven.** Every
withheld value now states that it is withheld and which role receives it —
"admin only" in a connector's From column, "editor only" on a build's
expectations, a boxed explanation where a federated dataset's endpoint used to
be. A blank is the one rendering that is not allowed: "nothing configured" is
what an operator reads from an empty field, and their next move is to type the
credential in again. Structured failures render as a code, an endpoint, a phase
and a `grep err-…` handle, so "the credential was never tested" and "the
endpoint refused the credential" remain one glance apart. The sidebar no longer
offers Transforms or Schedules to a viewer, and both pages say why if reached
directly.

Driving it as a real viewer found three things reading it did not:

- **Object-backed dashboard panels rendered as an empty box for everyone.** The
  run route returned `{groups, group_count, truncated}` for an object panel and
  `{columns, rows, row_count, truncated}` for a SQL one; the browser used to
  reconcile the two by deriving column names from the panel's own `group_by`
  and `metrics` — exactly the fields R2 stops sending. The route now normalizes
  both to one shape.
- **An object app silently showed the *unscoped* list.** `ObjectAppInfo.filters`
  is OPERATIONAL, and the client was the thing applying it, so "Aircraft in
  maintenance" listed every aircraft. New `GET /apps/{name}/objects` (VIEWER)
  applies the *stored* filters server-side and still resolves rows under the
  caller's own permissions — the same shape as the panel run route.
- **A dead Flight SQL engine reported "could not classify this one."** The ADBC
  driver connects lazily, so a refused endpoint surfaces from `query()` and
  never meets the connect pre-flight. `POST /engines/{name}/test` now re-probes
  when nothing else classified it, and adopts the answer only when the endpoint
  is genuinely unusable — a socket that opened and a query that failed is not an
  authentication problem, and saying so would send an operator to rotate a
  credential that is fine.

### Changed: the config redactor is an allowlist over key names, not a denylist

`redaction.redact_mapping` — what an **admin** sees when a connector config or a
federated dataset's source is read back — decided per key whether to disclose a
value, and its last branch was "disclose unless a shape rule objects". So a key
name it did not recognise was shown. Measured on this tree: a source registered
through `PUT /api/v1/sources/{name}` with `pw`, `bearer`, `pem`, `sas`,
`identity` or `bootstrap_servers` had every one of those values served verbatim
from `GET /api/v1/sources`, because none of them matches `password|secret|token|
…` and none of the values has a URL or `keyword=value` shape for the shape rules
to catch.

**Severity is low and it is worth saying why rather than leaving it implied.**
After the R1/R2 split this path guards admin-authored config displayed back to
admins — the same people who typed it, on a system they can already reach.
Nothing's confidentiality depends on it; `DatasetInfo.source` is
`AuthoredBy(Role.admin)` and `SourceInfo.config` is omitted entirely below
admin, and those annotations, not this function, are the boundary. What this
was is the same *losing shape* that produced criticals in rounds 1 and 3 — a
denylist over a vocabulary Laurelin does not own, since
`SourceUpsertRequest.config` is `dict[str, Any]`.

It is now an allowlist of fourteen shape keys (`type`, `table`, `query`,
`format`, `path`, the `url`/`uri` family, and the catalog/namespace/branch names
a registration carries), matching `export/secrets.NON_SECRET_SHAPE_KEYS` plus
the two endpoint keys the API keeps and a file export does not. Anything else is
withheld whatever it is called. The existing secret-name regex survives as a
*readability* choice — it picks `*****` over `***** (withheld)` for a key that
says what it held — so being incomplete now costs a reader a less specific
marker instead of costing disclosure.

The cost is real and bounded: a config key nobody named renders as
`***** (withheld)` until it is added to the list, which is a visible annoyance
fixed in one line rather than a silent leak. What an admin needs to tell one
registration from another is kept deliberately and tested against the two
screens that read it — `configSummary` in `views/Sources.tsx` and
`FederatedSource` in `views/Datasets.tsx` — because an allowlist that renders
every source identically has traded a theoretical leak for a screen nobody can
use.

### Fixed: sixteen defects from a fourth adversarial pass, all in the R1/R2 change itself

*(Sixteen is what this section enumerates: the fifteen bulleted defects plus the
guard defect at the end. An earlier draft of this heading said nineteen, which
counted the three UI defects recorded in the section above — those are listed
there, not here, and are not double-counted.)*

R1 and R2 were attacked by agents who had written neither. Every entry was
reproduced on a running server before it was fixed, and every fix was reverted
and watched to fail its own regression test before being restored.

The pattern behind most of them is one thing, not nineteen: **R1 and R2 were
applied to the read path, and the error path is a read path too.** A stored
instruction that a viewer may not read comes straight back out of the 400 that
says the instruction is broken.

**The last place confidentiality still rested on the free-text matcher.**

- **An EDITOR read five live credentials out of `GET /datasets/{name}`.**
  `DatasetInfo` is editor-authored, but `DatasetInfo.source` is written only by
  the three ADMIN registration routes — so the field crossed a privilege
  boundary inside a record that did not, and the only thing in the way was
  `redaction.redact_mapping` → `redact_value` → `keyword_credential`. Measured,
  through the real front door: `password='…'` (quoted, so `_KEYWORD_SECRET_RE`
  missed it), `Pwd='…'`, `Password:…`, a bare AWS key pair and a positional
  JDBC URL all shipped verbatim. One case was withheld, and only because the
  matcher happened to fire. Adding a quote defeated the boundary.

  Fields can now declare an authoring role above their record's
  (`audience.AuthoredBy`), so `source` reaches admin and nobody else. What an
  editor and a viewer get instead is `DatasetInfo.source_descriptor` — which
  table, in which format — built by Laurelin from an **allowlist of shape keys**
  with an identifier-shaped gate on every value. A key not on the list is absent
  whatever it is called; a value that is not identifier-shaped is absent
  whatever it contains. That question is decidable. "Does this contain a
  credential" is not, and nothing's confidentiality depends on it any more.

**Stored instructions coming back out of error paths.**

- **`POST /dashboards/{name}/panels/{panel_id}/run` was an oracle for the panel
  fields it withholds.** The SQL branch was converted; the object branch three
  lines below it was not. As a plain viewer, against panels an editor saved:
  `400 "Unknown group_by property 'postgresql://svc:…@internal-db:5432/x'"`,
  and the same for `metrics[].property` and `metrics[].op`. Each of those
  sentences is Laurelin's own, so R1 was satisfied and R2 was not: structure
  fixes R1's problem, only privilege fixes R2's. The message that names the
  offending field now goes to a principal who could have authored the panel;
  everyone else gets a `Failure` with a new `definition_stale` code and a
  `detail_ref`.
- **The same route 500'd on an ordinary dropped column.** It caught `ValueError`
  only, and `duckdb.BinderException` is not one — so a viewer's dashboard became
  a bare "Internal Server Error" with no code, no reference and nothing to act
  on. That is the empty box the whole R2 design is meant to avoid.
- **`GET /apps/{name}/objects` quoted an app's admin-authored `filters` back to
  a viewer.** Write-time validation closes this on day one and not on day two:
  rename a property in `ontology/*.yml` — an ordinary admin act — and the next
  viewer to open the app read the stale filter's name out of a 400. Note that
  this route was *added* by R2, to stop the client applying the filters: the
  instruction moved server-side and its text came back out the error path.
- **`POST /sources/{name}/sync` returned the admin's endpoint in its 502.** The
  route has no `dependencies=[...]`; its only gate is `_require_dataset_edit`,
  which a plain editor always passes and a viewer passes with an explicit
  `can_edit` grant. Both read `"The host for source:crm could not be resolved at
  secret-db.internal.corp:55999"` while their own `GET /sources/crm` correctly
  carries no `config` at all. `HTTPException(detail=…)` never passes through
  `serialize.dump`, so R2 had no jurisdiction over it until asked:
  `serialize.detail_for` now renders in full above the level that authored the
  configuration and briefly below it. Reach included MCP, which surfaces a 502
  `detail` verbatim into `LaurelinError`.

**The two global exception handlers were a standing bypass of R1.**

- **An EDITOR read the operator's S3 warehouse credential out of a 400.**
  `@app.exception_handler(ValueError)` turns any uncaught exception of that type
  into a response body carrying that library's raw words — and
  `pyarrow.lib.ArrowInvalid` **is** a `ValueError`, `ArrowKeyError` **is** a
  `KeyError`. Uploading a CSV to an Iceberg dataset with an `s3://KEY:SECRET@…`
  warehouse returned `{"detail": "Not a valid bucket name:
  'AKIAICESENT:ICESENTINELKEY@icebucket'"}`. R1 was being honoured catch site by
  catch site, with no net underneath. `failure.is_first_party` decides on the
  **deepest traceback frame** — where the `raise` is written, which is a fact,
  rather than on the exception's type, which is not — and anything else becomes
  a `Failure`.
- The same rule now applies to the route-level catches *above* that net.
  `failure.safe_detail` replaced every `HTTPException(detail=str(exc))` in the
  API: a no-op when Laurelin raised the exception, a `Failure` when a library
  did. Most of those blocks catch first-party validation and echo the caller's
  own input, but several wrap a call into pyiceberg or pyarrow — the same
  mechanism as the disclosure above, one level down.
- **`FederationError` had no handler at all**, so `GET /datasets/{name}/rows`
  500'd for every role the moment an upstream table was renamed away. Nothing
  leaked (Starlette's 500 is bare) but the message it would have carried
  interpolated DuckDB's `LINE 1:` echo of the `postgres_scan(...)` call, and
  therefore the DSN, twice. Now a 502 with a structured failure.

**The audit trail: two mechanisms answering one question, and the second
cancelling the first.**

- **`GET /audit/mine` (VIEWER) re-attached the raw details bag** with an explicit
  `| {"details": entry.details}` override, in the module that defines the single
  serialization point. Its justification — "the details came from their own
  request" — is false by construction for a whole class of rows: a viewer
  triggering a sync supplies a *name*, and Laurelin builds the rest of the bag
  from an admin's connector config. A viewer read a whole `Failure` this way:
  endpoint, driver, `detail_ref` and rendered message.
- **The same route bypassed the migration's fail-closed default.**
  `_migrate_failures` stamps every pre-existing row `admin` precisely because
  "the rows were written by callers who had no idea who would read them", and
  `list_audit`'s `actor` branch skipped that filter. Reproduced: a user demoted
  from editor to viewer read a live DSN and password out of a migrated row.
- **`min_read_role=Role.editor` was inert.** `list_audit` chose which rows an
  editor saw and then the serializer dropped `details` from every one of them,
  because `AuditEvent` is class-level admin. All five declarations in the tree
  were dead code, each with a comment asserting a disclosure that did not
  happen — while `/audit/mine` handed a viewer the same bag whole. The privilege
  ordering was inverted. A row's `min_read_role` is now that record's author
  role, so one mechanism decides which rows are offered and the same declaration
  decides how much of each is filled in. Writers put `Failure.audit_projection()`
  in the bag rather than the whole record, because `details` is an open dict and
  the serializer cannot reach inside it.

**Serialization.**

- **Recursion widened instead of narrowing.** A `Failure` (editor-authored)
  nested in a `SourceInfo` (admin-authored) was dumped in **full** to an editor
  who had correctly been given only the projection of its parent — restoring the
  admin's `endpoint`, which is rebuilt from the one field
  `source_routes._public` exists to withhold. Descending into a record can now
  only ever disclose less, and `Failure.endpoint` declares `AuthoredBy(admin)`
  besides.
- **The "one serialization point" was not one.** One site in `routes.py` and
  five in `auth_routes.py` called `model_dump` directly, and the guard meant to catch
  that allowlisted three files wholesale — including the two they were in.
  Nothing leaked, because every field involved happened to be PRESENTATION; both
  were unannotated paths where a field added tomorrow ships. The guard is
  per-line now, and the opt-out is an inline `# serialize-ok:` marker with a
  reason. `as_author`, R2's deliberate escape hatch, had **no production caller
  at all**, so the test policing it passed vacuously; `laurelin/cli.py` uses it
  for real.

**Product breakage introduced by the fix.**

- **`_preserve_operational` made a panel's operational fields un-clearable.** It
  inherited a stored value whenever the submitted one was falsy, which cannot
  tell "the client omitted this key" from "the editor cleared it". An editor
  clearing `group_by` got `200` and the old value back — a write silently
  rejected with a success status — and converting a SQL panel to an object panel
  was unreachable, returning `400 "A panel draws from either sql or object_type,
  not both"` about text the server had just re-inserted. Absence is the signal
  now, which is exactly what `serialize._projection` produces, because it omits
  operational keys rather than blanking them.
- **`Failure.driver` named the wrong library on two paths.**
  `_driver_failure` hardcoded `duckdb` and also serves the ClickHouse and
  StarRocks routes, so a `chdb` failure was filed under duckdb; and the `http`
  connector claimed `requests` while `_pull_http` uses `urllib.request`. Not a
  disclosure — but `driver` exists so an operator knows whose log line to read,
  and a closed set populated wrongly is worse than an empty one. Read off the
  raising class's module now, which is a fact.

**The guard that was supposed to catch all of this inspected 24 of 144 routes.**
It swept GET only, treated a 200 as the only body worth checking, and silently
`continue`d past any route that 404'd — so 78 non-GET routes were outside it by
construction, three of the disclosures above were in **4xx** bodies, one was on a
**POST**, one was only visible to an *editor* (it only ever logged in as a
viewer), and `GET /dashboards/{name}` — the route that leaked in all three
previous rounds — was never checked once, because the fixture's dashboard was
called `board` while `PATH_PARAMS["name"]` said `sales`. Two probe routes added
to prove it both returned panel SQL to a viewer with the suite fully green.

It now drives **every method of every route at two privilege levels** and asserts
on the body whatever the status code is; every seeded record shares one name so a
single parameter addresses all of them; and a GET that 404s even for an admin
fails a companion test unless it is listed with a reason.

**Known and accepted, not fixed.** An editor's chosen *captions* reach a viewer
by design — a panel's `title`, and the `alias` on a metric, which becomes the
column header a viewer reads. An editor who puts a credential in a column header
has disclosed it to their own audience deliberately, the same way they would by
putting it in the panel title. The invariant the guard states is about
*instructions* — `sql`, `group_by`, `filters`, `search`, metric `op` and
`property` — not about labels. See `SECURITY.md`.

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

### Security — three fail-open bugs that predate the new backends

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

### Fixed: four defects in the object overlay — two governance, one paging, one that would have scaled badly

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

**The first release we intend to publish** — and, as of this writing, still
unpublished: `0.2.0` has never been tagged or uploaded either, so nothing here
has reached a user. `0.1.0` existed only in the source tree. There is nothing
to upgrade from, and this describes what Laurelin *is* rather than what changed
since something you could have installed.

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
