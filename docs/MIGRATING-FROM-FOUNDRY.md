# Migrating from Palantir Foundry — a runbook for an AI agent

This document is written for an AI agent connected to a Laurelin server over
MCP (`laurelin mcp --url <server> --token <token>`), tasked with
reconstructing a Foundry workspace. Every step names the MCP tool to call and
the verification call that proves the step landed. A human can follow it too,
but the imperative voice is aimed at you, the agent.

**What "done" means:** the sources are registered and synced, the pipelines
are rebuilt as flows, the ontology (object types, links, actions) is live, the
governance (markings, clearances, grants, row policies, column masks) is at
least as strict as Foundry's, the dashboards render, the schedules fire — and
every one of those facts has been verified by a read call, not assumed from a
200.

**What you cannot do, by design:**

- **Bulk row data does not travel over MCP.** Rows enter Laurelin through
  sources (`create_source` + `sync_source`), file upload in the UI, or builds.
  There is no "insert rows" tool and you must not look for one. Point a `file`
  or `http` source at the exported Foundry data instead (Phase 3).
- **You cannot author Python transforms.** Only no-code flows. A transform the
  Flow IR cannot express is flagged for a human (§ "Hard translations"), never
  approximated silently and never worked around.
- **Workshop applications do not migrate.** Laurelin has no Workshop
  equivalent, permanently. Record each Workshop app you find and move on.

Every tool call you make runs through the same REST route, permission check,
and audit log as a human user of the web UI. There is no agent side door: a
403 means the token genuinely lacks the right, and every mutation you make is
attributed to the token's user in the audit log.

---

## 1. Preconditions

Confirm all of these before mutating anything.

1. **A server URL and an ADMIN API token.** Most of the migration (ontology
   definitions, sources, governance) is admin-gated. With an editor token you
   can author datasets, flows, dashboards and schedules but nothing in
   Phases 1–2 or 6; stop and report rather than partially migrating.
2. **Author as a durable service principal**, e.g. a `migration-svc` user,
   not a person's account. Every flow you save is stamped server-side with the
   token's username as its author, and **every future build re-checks that
   author's read access to the flow's input datasets**. If the author account
   is later deleted or loses a grant, those builds start failing. Mint the
   token from an account that will outlive the migration.
3. **Single- vs multi-workspace.** On a single-workspace server the workspace
   admin can call `create_user`. On a multi-workspace server, `POST /users` is
   superadmin-gated: `create_user` will 403 for a workspace admin, and user
   creation must be done by the server operator. Detect this early — create a
   throwaway test user first (Phase 1) and report if it is refused. Select the
   workspace with the client's `workspace` option (the `X-Laurelin-Workspace`
   header).
4. **Lock posture.** A server started with `--lock-flows` /
   `LAURELIN_LOCK_FLOWS=1` refuses all flow authoring with 403, deliberately.
   That is deployment policy, not an error: stop, report, and ask the operator
   to unlock for the migration window. (`--lock-pipelines` blocks *Python*
   pipeline authoring and does not affect you; you cannot author Python
   anyway.)
5. **Credential posture.** `create_source` config transits your conversation
   transcript. For secret-bearing sources (Postgres DSNs with passwords), the
   preferred pattern is: a human admin pre-creates the source in the UI, and
   you only ever call `sync_source`. Use `create_source` yourself for
   non-secret sources (`file` paths, public `http` URLs). Never ask a human to
   paste a credential into the chat.
6. **The Foundry-side inventory.** You need, from the source Foundry
   instance: the dataset list with schemas and exported data files
   (CSV/Parquet), the pipeline definitions, the ontology (object types,
   properties, links, action types), the markings and their memberships, the
   project role assignments, the dashboards/analyses, and the schedules.
   How that inventory is extracted from Foundry is outside this document;
   what matters here is that each artifact lands in a phase below.

---

## 2. Concept map: Foundry → Laurelin

| Foundry | Laurelin | Tool(s) | Notes |
|---|---|---|---|
| Dataset | Dataset | `create_dataset` | Names: `^[a-z][a-z0-9_]*$`. |
| Data Connection source + sync | Source (+ schedule) | `create_source`, `sync_source`, `upsert_schedule` | Types: `postgres`, `http`, `file`. |
| Pipeline Builder pipeline | Flow (no-code IR) | `write_flow`, `preview_flow` | One flow = one output dataset, same name. |
| Code Repository transform (Python/Java) | Flow **if expressible**, else flag for human | `write_flow` | No Python authoring over MCP. See § Hard translations. |
| Ontology object type | Object type | `put_object_type` | Exactly one backing dataset and one primary key per type. |
| Ontology link type | Link type | `put_link_type` | Foreign-key style: `from_property` → `to_property`. |
| Ontology action type | Action type | `put_action_type` | Kinds: `create`, `update`, `delete`. |
| Object Storage V2 edits | Object index + writeback overlay | `build_object_index`, `enable_writeback` | **Current state only — Foundry edit history does not migrate.** |
| Contour / Quiver analysis | Dashboard panel (`sql`, `object_type` or `flow`) | `upsert_dashboard`, `run_dashboard_panel` | Explore-style point-and-click analyses are flow panels. |
| Markings / Organizations | Markings + user clearances | `create_marking`, `set_dataset_markings`, `set_user_clearances` | Markings propagate down lineage automatically. |
| Project roles (Owner/Editor/Viewer…) | Global role (viewer/editor/admin) + groups + per-dataset grants | `create_user`, `create_group`, `set_group_members`, `set_dataset_grants`, `set_object_type_grants` | Lossy; map fail-closed. See § Hard translations. |
| Restricted views / row policies | Dataset row policy + column masks | `set_dataset_policy` | Value-list rules per subject; masks: `null`/`redact`/`hash`. **Row-policy only datasets no flow reads** (§ 6.3): a row policy on a flow's source refuses that flow's builds and edits, fail-closed. |
| Schedules | Schedules (`cron` or `upstream` trigger) | `upsert_schedule`, `run_schedule` | `upstream` = build when an input dataset gains a version. |
| Functions (server-side code) | No equivalent | — | Flag for human. |
| Workshop | No equivalent, permanently | — | Record and report. |

---

## 3. The ordered playbook

The order below is load-bearing. Datasets must exist before ontology types
that back onto them and before any governance call that names them (those
routes 404 on an unknown dataset — structure first, governance second, on
purpose). Users must exist before group memberships. When in doubt, the 404
you get for inverting the order is listed in § Failure modes.

After every phase, run the phase's verification call before moving on. A
migration that only checks at the end cannot tell which phase lied.

### Phase 0 — survey the target

Call `list_datasets`, `list_object_types`, `list_transforms`, `list_sources`,
`list_dashboards`, `get_lineage`. If the workspace is not empty, everything
you author must avoid colliding with what a human already built: a `write_flow`
against a dataset that already has a producer is a 409, and an ontology
`api_name` already defined in a hand-written file is a 409. Record what exists.

### Phase 1 — identity

Users before groups-with-members; members must already exist.

1. `create_user` — one per migrating principal (or per service account if the
   customer maps humans later). Arguments: `username`, `password`, `role`
   (`viewer` / `editor` / `admin`). Remember the multi-workspace caveat
   (§ 1.3).
2. `create_group` — one per Foundry team/project group. Argument: `name`.
3. `set_group_members` — `name`, `members: ["user1", "user2"]`.

Verify: `set_group_members` echoes the membership back. A 404 here means the
*group* does not exist; a member username that does not exist yet is a **400**
naming the user (`Unknown user: 'x'`) — Phase 1 order, users before
memberships.

### Phase 2 — markings

`create_marking` for every Foundry marking name, before any dataset carries
one:

```json
{"name": "phi", "description": "Protected health information (from Foundry marking PHI)"}
```

Clearances come later (Phase 6): granting clearances before the data is
loaded is harmless, but setting them after the grants are in place lets you
verify the whole governance surface at once.

### Phase 3 — sources and raw data

For every Foundry Data Connection sync, register a source targeting a raw
dataset. `create_dataset` first if you want a description on it; `sync_source`
will create the dataset if it does not exist.

```json
create_source: {
  "name": "orders_raw_src",
  "type": "file",
  "dataset": "orders_raw",
  "config": {"path": "/imports/foundry/orders.parquet"}
}
```

- `file`: `config.path` is a path **on the server's filesystem** — the
  operator must have placed the exported Foundry files there. Formats: CSV or
  Parquet.
- `http`: `config.url` (+ optional `format`, `headers`) for data reachable
  over HTTP.
- `postgres`: `config.url` + `table` or `query` — prefer human pre-creation
  (§ 1.5).

Then `sync_source` with the source's `name`, and verify with
`dataset_schema` + `dataset_rows` (spot-check counts against the Foundry
export). This is the **only** road for bulk rows; do not attempt to move data
through any other tool.

Seed/lookup tables follow the same road: export them as files, register a
`file` source each, sync.

### Phase 4 — flows (the pipelines)

For each Foundry pipeline, in dependency order (upstream transforms first —
`write_flow` validates every referenced column against the live schema, so a
flow over a dataset that does not exist yet is refused):

1. `flow_dataset_schema` for each input dataset — the column names and kinds
   you may reference. Never guess a column name.
2. Translate the transform into Flow IR (§ Hard translations has the
   expressibility test and a worked example).
3. `preview_flow` with the draft — sample rows without saving. The preview
   runs **as your token** with your row policies and masks; the eventual build
   runs unpolicied, so preview counts can legitimately be lower than build
   counts.
4. `write_flow` with `name` (which is also the output dataset name) and the
   IR. The author is stamped from your token; an `author` field in the body is
   ignored.
5. `run_build` with `targets: ["<flow_name>"]` and `wait: false`, then poll
   `get_build` with the returned build id until `succeeded`/`failed`. Never
   `wait: true` for real workloads — the client times out at 60s and the build
   keeps running without you.
6. Verify: `dataset_rows` on the output, row counts against Foundry.

### Phase 5 — ontology

Backing datasets must exist (Phase 3/4) or `put_object_type` 404s.

1. `put_object_type` per Foundry object type:

```json
{
  "api_name": "customer",
  "backing_dataset": "customers_clean",
  "primary_key": "customer_id",
  "title_property": "name",
  "properties": {
    "customer_id": {"type": "string"},
    "name": {"type": "string"},
    "region": {"type": "string"},
    "lifetime_value": {"type": "double"}
  }
}
```

   A property name missing from the backing dataset's current schema is a
   `warning` in the response, not a refusal (the column may arrive with a
   later build) — read the warnings and check they are all expected.

2. `put_link_type` per link: `api_name`, `from_type`, `to_type`,
   `from_property`, `to_property`, `cardinality` (`one_to_many` default).
   Both object types must already exist.
3. `put_action_type` per action: `api_name`, `object_type`, `kind`
   (`create`/`update`/`delete`), `parameters` (same `{name: {type, required}}`
   shape as Foundry action parameters).
4. Where the Foundry type had Object Storage V2 edits: `build_object_index`
   then `enable_writeback` on the type (§ 4.3 first if the type backs onto a
   flow output — writeback refuses transform-backed datasets by default).
   Only the **current** object state
   migrates — apply it via `apply_action` calls per edited object if the
   customer needs the overlay reproduced, and record that edit *history* is
   not migrated.

Verify: `get_object_type` for each type; `search_objects` returns objects;
`aggregate_objects` counts match the Foundry object counts.

### Phase 6 — governance tightening

All admin-gated. Datasets and types must exist — deliberately, so a policy
can never be created pointing at nothing and silently "succeed".

1. `set_dataset_grants` per dataset that was not world-readable in Foundry:

```json
{
  "dataset": "customers_clean",
  "grants": [
    {"subject_kind": "group", "subject": "sales", "can_view": true, "can_edit": false},
    {"subject_kind": "user", "subject": "dana", "can_view": true, "can_edit": true}
  ]
}
```

   `subject_kind` ∈ `everyone` / `role` / `group` / `user` (subject empty for
   `everyone`). **Setting any grant list removes the default open access** —
   fail-closed, which is what you want.

2. `set_object_type_grants` — same grant shape, per object type.
3. `set_dataset_policy` for Foundry restricted views:

```json
{
  "dataset": "orders_clean",
  "row_policy": {
    "column": "region",
    "rules": [
      {"subject_kind": "group", "subject": "emea_analysts", "values": ["eu", "uk"]}
    ]
  },
  "column_masks": [
    {"column": "email", "mode": "hash", "exempt": [{"subject_kind": "role", "subject": "admin"}]}
  ]
}
```

   A row policy with no matching rule for a user means that user sees **no
   rows** — fail-closed again. Mask modes: `null`, `redact` (`***`), `hash`
   (stable pseudonym — use it when Foundry semantics need equality joins to
   keep working).

   **The row-policy placement rule: row-policy only datasets that no flow
   reads.** Flow governance refuses a row-policied *source* unconditionally —
   a flow's output is a new dataset without the policy, so building from one
   would launder every row. Concretely: the moment a row policy lands on a
   dataset any flow reads, that flow's builds start failing (`FlowRefused`),
   and its `preview_flow` / `write_flow` return 400 for everyone including
   the admin author — and the failure surfaces on the *next scheduled
   rebuild*, not at the moment you set the policy. The `set_dataset_policy`
   response returns a `warnings` list naming the affected transforms — treat
   a non-empty `warnings` as a stop sign. When a Foundry restricted view
   feeds a pipeline, put the row policy on the pipeline's **terminal output**
   (policying a flow's output is fine) or on an unpolicied copy that no flow
   reads, and record the difference in the exceptions report. Column masks
   do not trigger this refusal (masked source columns flow through masked at
   preview, unmasked at build).

4. `set_dataset_markings` per dataset carrying a Foundry marking. Markings
   **propagate down lineage**: marking an input marks its downstream outputs
   on the next recompute, so mark the rawest dataset that carried the Foundry
   marking. The response reports only *that dataset's* explicit and effective
   markings — to see what propagated downstream, call
   `list_dataset_markings`, which returns explicit + effective for every
   dataset.
5. `set_user_clearances` per user: the list of markings they may read
   through. No clearance, no access, regardless of grants.

Verify by **reading the governance state back**, not by trusting write
echoes: `list_dataset_grants`, `list_dataset_policies`,
`list_dataset_markings`, `list_object_type_grants` and
`get_user_clearances` return the stored state (all admin-gated, like the
writes). Confirm every Foundry rule has a Laurelin counterpart there, and
write down every rule that has none (§ Hard translations, lossy governance).

Better still, verify **as the restricted principals**: a viewer token
exercising `dataset_rows` on a policied dataset proves the rows and masks
end-to-end. Know that a viewer **cannot mint an API token through the API**
(`POST /tokens` is editor-gated), so a restricted-viewer token must be
minted by the operator at service level — ask for one; do not treat the 403
as an error, and fall back to the read-back tools above if no such token is
available.

### Phase 7 — dashboards

One `upsert_dashboard` per Foundry dashboard/analysis; the panel list rides
in the same call:

```json
{
  "name": "revenue_ops",
  "title": "Revenue Ops",
  "panels": [
    {"id": "by_region", "title": "Revenue by region", "chart": "bar",
     "x": "region", "y": ["total"],
     "sql": "SELECT region, sum(amount) AS total FROM orders_clean GROUP BY region"},
    {"id": "open_orders", "title": "Open orders", "chart": "stat",
     "object_type": "order",
     "metrics": [{"op": "count", "alias": "open"}],
     "filters": {"status": "open"}}
  ]
}
```

Rules that will bite you if ignored:

- Each panel: exactly **one** of `sql`, `object_type`(+`metrics`), or `flow`.
- Chart something the ontology models with an `object_type` panel, not SQL:
  SQL reads the backing dataset and does not see writeback edits.
- 50 panels per dashboard, `chart` ∈ `table`/`bar`/`line`/`area`/`stat`/
  `pie`/`scatter`, `width` 1–12.
- Updating an existing dashboard: for a panel `id` that already exists, query
  fields **omitted** from your payload are inherited from the stored panel;
  send a field explicitly to change it.

Verify: `get_dashboard` (your editor/admin token gets the full document
back), then `run_dashboard_panel` per panel — it executes server-side as the
caller and returns `{columns, rows, row_count, truncated}`. A viewer token,
by design, can run panels but never read their `sql`/`flow`/aggregation
definition.

### Phase 8 — schedules

`upsert_schedule` per Foundry schedule:

```json
{"name": "nightly_build", "trigger": "cron", "cron": "0 6 * * *",
 "action": "build", "targets": ["orders_clean", "customers_clean"]}
```

```json
{"name": "orders_follow", "trigger": "upstream", "upstream_dataset": "orders_raw",
 "action": "build", "targets": ["orders_clean"]}
```

```json
{"name": "orders_pull", "trigger": "cron", "cron": "*/30 * * * *",
 "action": "sync", "source": "orders_raw_src"}
```

A bad cron expression fails at save time, not silently never. A referent that
does not exist — a `sync` source that is not registered, a `build` target no
transform produces, an `upstream` dataset that does not exist — **saves but
warns**: read the response's `warnings` and treat any entry as a mistake to
fix (a warned schedule fails, or never fires, when its window arrives).
Verify with `run_schedule` (fires it now, through the scheduler's normal
path — 409 if disabled), then poll `get_build` / check `dataset_rows`.

### Phase 9 — final verification

- `get_lineage`: every Foundry dataset dependency has a matching edge.
- `aggregate_objects` per object type vs Foundry counts. **Not `query_sql`**
  once writeback edits exist: SQL reads the backing dataset and will
  disagree with the object layer, correctly.
- `run_dashboard_panel` for every panel: rows, not errors.
- Governance read-back: `list_dataset_grants`, `list_dataset_policies`,
  `list_dataset_markings`, `list_object_type_grants`, `get_user_clearances`
  against the Foundry inventory — every rule accounted for, in the stored
  state, not in your write echoes.
- Audit review (a human or admin token, `GET /api/v1/audit` over REST): every
  mutation of the migration is present, attributed to the migration
  principal.
- Deliver the exceptions report: every transform flagged for a human, every
  unmapped governance rule, every Workshop app, the note that object edit
  history did not migrate.

---

## 4. Hard translations

### 4.1 Code Repository / Pipeline Builder transforms → Flow IR

The Flow IR is closed: ten node kinds — `source`, `filter`, `select`,
`rename`, `derive`, `cast`, `join`, `aggregate`, `dedupe`, `sort` — a DAG,
no raw SQL and no free-text expressions anywhere. Every node is
`{"id", "kind", "inputs": [upstream node ids], "params": {...}}`; every kind
takes exactly one input except `source` (zero) and `join` (two, **ordered**:
left then right). The `params` for each kind:

| Kind | `params` | Notes |
|---|---|---|
| `source` | `{"dataset": "<name>"}` | |
| `filter` | `{"predicate": <expr>}` | Root of `<expr>` must be boolean. |
| `select` | `{"mode": "keep"\|"drop", "columns": ["a", "b"]}` | `mode` is required. |
| `rename` | `{"pairs": [{"from": "old", "to": "new"}, ...]}` | No duplicate `from`s or `to`s. |
| `derive` | `{"name": "<new column>", "expr": <expr>}` | |
| `cast` | `{"column": "<col>", "to": "varchar"\|"bigint"\|"double"\|"boolean"\|"date"\|"timestamp"}` | |
| `join` | `{"how": "inner"\|"left", "keys": [{"left": "<left col>", "right": "<right col>"}, ...]}` | `keys` is a list of objects, one per key pair. |
| `aggregate` | `{"group_by": ["c1"], "aggs": [{"fn": "<fn>", "column": "<col>", "as": "<alias>"}]}` | `fn` ∈ `count_star` (no `column`), `count`, `count_distinct`, `sum`, `avg`, `min`, `max`, `any_value`, `median`. `as` is required. |
| `dedupe` | `{"keys": ["c1"], "order_by": [{"column": "<c>", "dir": "asc"\|"desc"}], "keep": "first"\|"last"}` | `order_by` is mandatory — dedupe without an order is a coin flip. |
| `sort` | `{"by": [{"column": "<c>", "dir": "asc"\|"desc", "nulls": "first"\|"last"}]}` | `nulls` defaults to `last`. |

Expressions (`<expr>` in `filter`/`derive`) are trees of three leaf/node
forms: `{"t": "col", "name": "<column>"}`,
`{"t": "lit", "type": "string"|"bigint"|"double"|"boolean"|"date"|"timestamp"|"null", "value": ...}`,
and `{"t": "op", "op": "<op>", "args": [<expr>, ...]}` with ops:
`and or not eq ne lt lte gt gte is_null is_not_null in not_in like`
(boolean-result), `add sub mul div if_else coalesce upper lower trim length
abs round floor concat date_trunc` (`date_trunc` units: `year quarter month
week day hour`).

**Expressibility test, in order:**

1. Only column selections, renames, casts, filters on column/constant
   comparisons, derived columns from arithmetic/date-trunc, inner/left
   joins, group-by aggregations from the list above, dedupe, sort? →
   **Flow.**
2. Needs window functions, pivots/unpivots, UDFs, regex extraction,
   recursive/iterative logic, ML scoring, right/full/cross joins, or calls
   external services? → **Flag for a human.** Write the transform's name,
   inputs, output, and the exact unexpressible operation into your exceptions
   report. Do **not** approximate it, and do not attempt Python: there is no
   Python-authoring tool, and that is a security boundary, not a gap.

A worked example — "orders excluding returns, revenue by region, largest
first" as a `write_flow` call (`name` is the output dataset name; `output`
must equal it):

```json
{
  "name": "busy_regions",
  "flow": {
    "name": "busy_regions",
    "output": "busy_regions",
    "terminal": "n3",
    "nodes": [
      {"id": "n0", "kind": "source", "inputs": [], "params": {"dataset": "orders_raw"}},
      {"id": "n1", "kind": "filter", "inputs": ["n0"], "params": {
        "predicate": {"t": "op", "op": "ne", "args": [
          {"t": "col", "name": "status"},
          {"t": "lit", "type": "string", "value": "returned"}]}}},
      {"id": "n2", "kind": "aggregate", "inputs": ["n1"], "params": {
        "group_by": ["region"],
        "aggs": [{"fn": "sum", "column": "amount", "as": "total"}]}},
      {"id": "n3", "kind": "sort", "inputs": ["n2"], "params": {
        "by": [{"column": "total", "dir": "desc", "nulls": "last"}]}}
    ]
  }
}
```

Workflow per transform: `flow_dataset_schema` → build IR → `preview_flow` →
fix refusals (they name the node and field) → `write_flow` → `run_build` →
`get_build` → `dataset_rows`.

### 4.2 Multi-datasource Foundry object types

A Foundry object type can back onto several datasources; a Laurelin object
type backs onto exactly one dataset with one primary key. **Denormalize
first**: author a flow that joins the datasources into one dataset, then
define the object type over it.

Sequence: `flow_dataset_schema` on each part → `write_flow` with `join`
nodes (left join, keyed on the shared id; `select`/`rename` to the property
names you want) → `run_build` → `put_object_type` over the flow's output.
The join flow is now the type's provenance in `get_lineage`, which is what
you want an auditor to see.

If the same type also had Object Storage V2 edits, read § 4.3 **before**
calling `enable_writeback`: a type backed by a flow output refuses writeback
by default, deliberately.

### 4.3 Object Storage V2 edit history

Laurelin's writeback is an overlay of **current** object state on top of the
backing dataset. Foundry's per-edit history does not migrate. If the customer
needs current edited values: `enable_writeback` on the type, then
`apply_action` per edited object with the current Foundry value. Each of
those applications is attributed to *your* migration principal in the audit
log — say so in the report; do not attempt to forge historical actors (you
cannot: attribution is server-side).

**When the type backs onto a flow output (the § 4.2 denormalization
recipe), `enable_writeback` refuses with a 400 by default**: folding edits
into a transform-produced dataset hands them to the next build to overwrite,
silently. The tool takes `allow_transform_backed: true` to override for a
deliberate one-shot import — use it only when you accept that **the next
build of that flow discards the folded values**. For a type that needs
*ongoing* edits on top of a denormalized dataset, the honest options are:
keep the edits in the overlay (skip `enable_writeback`; `aggregate_objects`
and object panels see the overlay anyway), or make the denormalizing flow a
one-shot (build it once, author no schedule for it) and record that in the
exceptions report.

Afterwards, remember the read rule: `aggregate_objects` sees the overlay,
`query_sql` sees the backing dataset. They will disagree, correctly.

### 4.4 Governance is lossy — map it fail-closed

Foundry's permission model (organizations, projects, roles-per-project,
markings, checkpoints) is richer than Laurelin's (global role + groups +
per-resource grants + markings + row/column policy). The mapping rule is:
**when a Foundry rule has no exact Laurelin equivalent, the Laurelin result
must be at least as restrictive.**

- Foundry project role that granted access to *some* project datasets → a
  Laurelin group with `can_view` grants on exactly those datasets. Fewer
  grants, not `everyone`.
- Any doubt about whether a marking applies → apply it. Removing a marking
  later is an admin action with an audit trail; a leak is forever.
- Any Foundry rule you could not map at all → an entry in the exceptions
  report with the rule, the affected resources, and the fail-closed stance
  you took instead. That written record is a deliverable, not an apology.

### 4.5 Contour / Quiver analyses

Three panel sources, in order of preference:

1. **`object_type` panel** — the analysis was an aggregation over ontology
   entities. Sees writeback edits.
2. **`flow` panel** — the analysis was multi-step point-and-click over
   datasets (Foundry Contour paths). Store the Flow IR in the panel's `flow`
   field — the same document shape as 4.1 including `name`/`output` (equal to
   each other; a panel flow's name does not create a dataset). It is compiled
   per run against the live schema, and the panel's chart bindings are
   validated at save.
3. **`sql` panel** — the fallback, raw SQL over datasets.

For all three, the viewer-facing behavior is identical: viewers run panels
server-side as themselves (`run_dashboard_panel`) and receive results shaped
by their own grants, row policies and masks — never the query text.

---

## 5. Failure modes you will hit

| Symptom | Meaning | What to do |
|---|---|---|
| 403 with a role message | The token's role is below the route's gate (e.g. editor calling an admin tool). | Stop. Report the tool and the role needed. Do not retry. |
| 403 naming a lock / flows being locked | `LAURELIN_LOCK_FLOWS=1` on the server. | Deployment policy. Report; ask the operator for a migration window. |
| 401 | Bad or expired token. | Stop; get a new token from the operator. |
| 409 from `write_flow`: another transform produces this dataset | One producer per dataset — a human (or an earlier you) already built it. | `list_transforms` / `get_lineage` to find the producer; reconcile, don't overwrite. |
| 409 from `write_flow` naming a *code transform* | A Python pipeline file already uses the flow's name or produces its output; a flow can never replace or share a name with code. | Pick a different flow/output name, or a human removes the pipeline file. |
| 400 from `preview_flow`/`write_flow`, or `FlowRefused` in a build task, saying a source **has a row policy** | A row policy landed on a dataset this flow reads; flow governance refuses row-policied sources unconditionally, fail-closed. | § 6.3 placement rule: move the row policy to the flow's terminal output (or an unpolicied copy), then rebuild. |
| 400 from `enable_writeback`: dataset is produced by a transform | The type backs onto a flow/pipeline output; folding edits there hands them to the next build to overwrite. | § 4.3: pass `allow_transform_backed: true` only for a deliberate one-shot import, or keep edits in the overlay. |
| `upsert_schedule` returns `warnings` | The schedule saved, but names a referent that does not exist (source, build target, upstream dataset) — it will fail or never fire. | Fix the typo or author the missing flow/source, then re-save until `warnings` is empty. |
| 409 from ontology authoring: `api_name` defined in a hand-written file | The definition lives in a YAML file a human authored; the API refuses to shadow or delete it. | Edit belongs to the human; report which file the response names. |
| 404 from a governance call naming a dataset | Order of operations: the dataset does not exist yet. | You are in Phase 6 before finishing Phase 3/4. Fix the order. |
| 400 from `set_group_members` naming a user | A member username does not exist yet (a 404 means the *group* is missing). | Phase 1 order: users before memberships. |
| Preview rows < build rows | Preview runs policied as you; builds run unpolicied. | Expected. Compare *build* output to Foundry, not preview. |
| `aggregate_objects` ≠ `query_sql` counts | Writeback edits exist; SQL reads the backing dataset. | Expected and correct. Trust the object layer for object questions. |
| Deleted-and-recreated flow output still carries markings | Propagated markings are retained fail-closed on delete. | Expected; nothing downstream was declassified. An admin can adjust markings explicitly. |
| `run_build(wait=true)` times out | Client timeout is 60s; the build continues server-side. | Always `wait: false` + poll `get_build`. |
| `create_user` 403 on an admin token | Multi-workspace server: user creation is superadmin-only. | Report; the server operator creates users. |

---

## 6. Tool inventory referenced by this guide

Read/verify: `list_datasets`, `dataset_schema`, `dataset_rows`, `query_sql`,
`list_object_types`, `get_object_type`, `search_objects`,
`aggregate_objects`, `get_object`, `get_linked_objects`, `list_actions`,
`list_transforms`, `get_lineage`, `get_build`, `list_sources`,
`list_dashboards`, `get_dashboard`.

Author — datasets/pipelines (editor): `create_dataset`,
`flow_dataset_schema`, `preview_flow`, `write_flow`, `delete_flow`,
`run_build`, `sync_source`.

Author — sources (admin): `create_source`, `delete_source`.

Author — ontology (admin; index/writeback editor): `put_object_type`,
`delete_object_type`, `put_link_type`, `delete_link_type`,
`put_action_type`, `delete_action_type`, `build_object_index`,
`enable_writeback`.

Governance (admin): `create_marking`, `set_dataset_markings`,
`set_user_clearances`, `set_dataset_grants`, `set_object_type_grants`,
`set_dataset_policy`, `create_user`, `create_group`, `set_group_members`.

Governance read-back (admin): `list_dataset_markings`,
`list_dataset_grants`, `list_dataset_policies`, `list_object_type_grants`,
`get_user_clearances` — verify governance by reading the stored state, never
by re-issuing writes.

Presentation (editor; panel runs viewer-callable): `upsert_dashboard`,
`run_dashboard_panel`, `upsert_schedule`, `run_schedule`.

Write-back to objects (per-action permissions): `apply_action`.

If a tool named here is missing from your `tools/list`, you are connected to
an older Laurelin — report the server version instead of improvising with a
different tool.
