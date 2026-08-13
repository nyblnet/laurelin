// Types mirroring the Laurelin REST API (docs/ARCHITECTURE.md).

export type Role = "viewer" | "editor" | "admin";

export interface User {
  id: string;
  username: string;
  role: Role;
  created_at: string;
  disabled: boolean;
  superadmin?: boolean;
  // Present in multi-workspace mode on /auth/status and /auth/me: the
  // workspaces this user belongs to and their role in each.
  workspaces?: UserWorkspace[];
}

export interface UserWorkspace {
  slug: string;
  name: string;
  description?: string;
  role: Role;
}

export interface OidcStatus {
  enabled: boolean;
  provider_name?: string;
}

export interface AuthStatus {
  auth_required: boolean;
  setup_required: boolean;
  multi?: boolean;
  oidc?: OidcStatus;
  saml?: OidcStatus;
  user: User | null;
}

export interface WorkspaceSummary {
  slug: string;
  name: string;
  description: string;
  created_at: string;
  members?: number;
}

export interface WorkspaceMember {
  username: string;
  role: Role;
}

export interface WorkspaceInfo {
  name: string;
  description: string;
  /** Admin only — the server's filesystem layout. Absent for everyone else,
   *  which is why it is optional rather than "". */
  root?: string;
}

// -- Structured failures (R1) -----------------------------------------------

/**
 * Laurelin's own vocabulary for why something failed. A driver's sentence is
 * never persisted, so this closed set is what the UI has to speak. Each member
 * maps to one operator action, which is the whole reason the set exists —
 * `auth_rejected` means rotate the credential, `endpoint_unreachable` means
 * open the firewall. See `laurelin/core/failure.py`.
 */
export type FailureCode =
  | "credential_malformed"
  | "endpoint_unresolvable"
  | "endpoint_unreachable"
  | "endpoint_timeout"
  | "auth_rejected"
  | "database_missing"
  | "permission_denied"
  | "relation_missing"
  | "column_missing"
  | "schema_incompatible"
  | "statement_invalid"
  | "resource_exhausted"
  | "definition_stale"
  | "transform_failed"
  | "expectation_failed"
  | "remote_failed";

/**
 * A failure as it arrives on the wire.
 *
 * Only `code` and `subject` are PRESENTATION, so a reader below the record's
 * authoring role (a viewer looking at a build) receives exactly those two and
 * nothing else. Every other field is optional here for that reason — this type
 * describes both projections, and the UI must never assume the wide one.
 */
export interface Failure {
  code: FailureCode;
  subject: string;
  phase?: string;
  endpoint?: string;
  driver?: string;
  exc_class?: string;
  vendor_code?: string;
  counters?: Record<string, number>;
  /** "err-<12 hex>" — the grep handle for the full text in the server log. */
  detail_ref?: string;
  at?: string;
  /** Server-rendered sentence; present on `as_dict()` payloads. */
  message?: string;
}

/** A non-blocking authoring hint. Explicitly not a security control. */
export interface AuthoringWarning {
  field: string;
  hint: string;
}

export interface ColumnSchema {
  name: string;
  type: string;
}

export interface DatasetVersion {
  dataset: string;
  version: number;
  created_at: string;
  row_count: number;
  schema: ColumnSchema[];
  path: string;
  build_id: string | null;
  source: string;
}

// Mirrors DATASET_KINDS in laurelin/core/models.py. Kept exhaustive on purpose:
// every kind comparison in the Datasets view narrows on this union, so a kind
// the server can return but this type cannot name falls through to the managed
// branch and renders controls the server will refuse.
export type DatasetKind =
  | "managed"
  | "federated"
  | "iceberg"
  | "clickhouse"
  | "starrocks";

export interface Dataset {
  name: string;
  description: string;
  created_at: string;
  latest_version: number | null;
  kind?: DatasetKind;
  /**
   * The (redacted) source Laurelin scans, for anything it does not hold itself.
   *
   * R2: OPERATIONAL **and `AuthoredBy(admin)`**. It is a connection config, and
   * the only three routes that write it are admin-gated, so it reaches admin
   * and nobody else — even though a *dataset* is editor-authored. It used to
   * reach an editor with `redact_mapping` in front of it, and a plain editor
   * read five live credentials through that: a quoted libpq conninfo, a quoted
   * ODBC keyword string, a colon-delimited form, a bare AWS key pair and a
   * positional JDBC URL.
   */
  source?: Record<string, unknown>;
  /**
   * Which table, in which format — Laurelin's own description of the source,
   * built from an allowlist of shape keys with an identifier-shaped gate on
   * every value. PRESENTATION: it is safe by construction rather than by
   * recognition, so every role gets it, and the "needs credentials" pill an
   * operator notices on the *list* rides here now that `source` does not.
   */
  source_descriptor?: {
    type?: string;
    table?: string;
    format?: string;
    catalog?: string;
    database?: string;
    namespace?: string;
    branch?: string;
    needs_credentials?: boolean;
    data_state?: string;
  };
  // Present on permission-aware responses (dataset list/detail): the current
  // user's effective access to this dataset.
  permissions?: ObjectTypePermission;
}

export interface DatasetDetail extends Dataset {
  versions: DatasetVersion[];
}

export interface UploadPreview {
  suggested_name: string;
  columns: ColumnSchema[];
  rows: Record<string, unknown>[];
  sampled_rows: number;
  /** The sample hit its cap, so the file has at least this many rows. */
  truncated: boolean;
}

export interface RowsPage {
  rows: Record<string, unknown>[];
  row_count: number;
}

export interface QueryResult {
  columns: string[];
  rows: Record<string, unknown>[];
  row_count: number;
  truncated: boolean;
}

export interface ObjectApp {
  name: string;
  title: string;
  description: string;
  object_type: string;
  columns: string[];
  /**
   * OPERATIONAL, and absent below admin: a filter is an instruction, not a
   * caption, even though it sits between two captions in the record.
   *
   * The app's object list is therefore fetched from
   * `GET /apps/{name}/objects`, which applies the *stored* filters server-side
   * — the same shape as a dashboard panel's run route. A client that had to
   * hold the filters in order to apply them would either crash without them or,
   * worse, quietly show the unscoped list.
   */
  filters?: Record<string, string>;
  search_placeholder: string;
  actions: string[];
  links: string[];
  created_at: string;
  created_by?: string;
  updated_at: string;
}

export type ChartKind = "table" | "bar" | "line" | "area" | "stat";

export interface AggregateMetric {
  op: string;
  property?: string | null;
  alias?: string;
}

export interface AggregateResult {
  groups: Record<string, unknown>[];
  group_count: number;
  truncated: boolean;
}

/**
 * A panel, in both of the shapes the server sends.
 *
 * R2 splits a panel down the middle: `id`/`title`/`chart`/`x`/`y`/`width` are
 * PRESENTATION — written for the person reading the picture — and everything
 * that describes *how to get the numbers* is OPERATIONAL and reaches only a
 * principal who could have written it. A viewer's panel therefore has no `sql`
 * key at all (not `sql: ""`), which is deliberate on the server's side and
 * load-bearing here: an absent key round-trips through a PUT as "leave it
 * alone", an empty string round-trips as "erase it".
 *
 * Hence every operational field is optional. `hasSource()` below is the one
 * place that asks whether this panel arrived whole.
 */
export interface DashboardPanel {
  id: string;
  /** Always non-empty on a stored panel: the server fills "Panel {n}". */
  title: string;
  /** Source A: raw SQL over datasets. OPERATIONAL — absent below editor. */
  sql?: string;
  /** Source B: an aggregation over ontology objects (sees the edit overlay). */
  object_type?: string;
  group_by?: string[];
  metrics?: AggregateMetric[];
  filters?: Record<string, string>;
  search?: string;
  chart: ChartKind;
  x: string;
  y: string[];
  width: number; // 1..12 columns
}

/** True when this panel arrived with its operational half — i.e. we may edit
 *  it. False for a viewer's projection, where editing would write back a hole. */
export function panelIsWhole(p: DashboardPanel): boolean {
  return p.sql !== undefined || p.object_type !== undefined;
}

export interface Dashboard {
  name: string;
  title: string;
  description: string;
  panels: DashboardPanel[];
  created_at: string;
  /** Editor+ only. */
  created_by?: string;
  updated_at: string;
  /** Present on write responses. Advisory; never a refusal. */
  warnings?: AuthoringWarning[];
}

/** What POST /dashboards/{name}/panels/{id}/run returns: rows, nothing else. */
export type PanelRunResult = QueryResult;

export type SourceType = "postgres" | "http" | "file";

export interface Source {
  name: string;
  type: SourceType;
  dataset: string;
  /**
   * Admin only, and still redacted there. A source is admin-authored and
   * editor-read, so R2 withholds the connector config from an editor entirely
   * rather than running a key-name denylist over somebody else's vocabulary.
   * Absent, not empty — an editor screen must say "admin only", not draw a
   * blank that reads as "nothing configured".
   */
  config?: Record<string, unknown>;
  created_at: string;
  created_by?: string;
  last_sync_at: string | null;
  last_sync_status: "succeeded" | "failed" | null;
  /** R1: replaced `last_sync_error`, which held the driver's own sentence. */
  last_sync_failure: Failure | null;
  last_sync_version: number | null;
  last_sync_rows: number | null;
}

export interface TransformSummary {
  name: string;
  output: string;
  inputs: string[];
  kind: string;
}

export interface LineageNode {
  id: string;
  type: "dataset" | "transform";
}

export interface LineageGraph {
  nodes: LineageNode[];
  edges: { from: string; to: string }[];
}

export type BuildStatus = "pending" | "running" | "succeeded" | "failed";

export interface BuildTask {
  transform_name: string;
  output_dataset: string;
  status: BuildStatus;
  started_at: string | null;
  finished_at: string | null;
  /** R1: replaced `error: string`, which held f"{type(exc).__name__}: {exc}". */
  failure: Failure | null;
  rows_written: number | null;
  output_version: number | null;
  /** OPERATIONAL: an expectation `message` is prose an editor wrote in a
   *  pipeline file. Absent for a viewer — see `ExpectationsCell`. */
  expectations?: ExpectationResult[];
}

export interface ExpectationResult {
  expectation: string;
  passed: boolean;
  severity: "error" | "warn";
  measured: number;
  message: string;
}

export interface Build {
  id: string;
  targets: string[];
  status: BuildStatus;
  started_at: string | null;
  finished_at: string | null;
  failure: Failure | null;
  tasks: BuildTask[];
}

export interface PropertyDef {
  type: string;
  display_name: string | null;
  description: string;
}

export interface ObjectTypePermission {
  can_view: boolean;
  can_edit: boolean;
}

export interface ObjectTypeDef {
  api_name: string;
  display_name: string | null;
  description: string;
  backing_dataset: string;
  primary_key: string;
  title_property: string | null;
  properties: Record<string, PropertyDef>;
  // Present on responses that are permission-aware (object-types list/detail):
  // the current user's effective access to this type.
  permissions?: ObjectTypePermission;
}

export type Cardinality = "one_to_one" | "one_to_many" | "many_to_many";

export interface LinkTypeDef {
  api_name: string;
  display_name: string | null;
  from: string;
  to: string;
  cardinality: Cardinality;
  from_property: string;
  to_property: string;
}

export type ActionKind = "create" | "update" | "delete";

export interface ActionParameterDef {
  type: string;
  required: boolean;
  description: string;
}

export interface ActionDef {
  api_name: string;
  display_name: string | null;
  description: string;
  object_type: string;
  kind: ActionKind;
  parameters: Record<string, ActionParameterDef>;
}

export interface ObjectIndexStatus {
  indexed: boolean;
  /** A stale index is bypassed, so this is what decides whether it's used. */
  fresh: boolean;
  /**
   * How many objects the shared materialization holds — **null for a caller
   * the backing dataset's row-level security narrows.** It counts every
   * tenant's objects, so handing it to a user who can see three of six would
   * disclose the other three's existence. Same reason for `lag` and
   * `applied_seq`: they describe the shared store, not this user's slice.
   */
  objects: number | null;
  /**
   * Edits recorded since the materialization last caught up. On its own it
   * cannot say whether the next write will clear it — a store that is a
   * dataset-version behind accumulates lag exactly like one that is merely
   * behind by edits, because catch-up bails on the version mismatch before it
   * replays anything. `stale_version` and `stale_definition` are what settle
   * that; read them first.
   */
  lag: number | null;
  /** Where the materialized objects live — "metadata" or "starrocks". */
  store: string | null;
  /** Last edit-log position the materialization has applied. */
  applied_seq: number | null;
  /** The backing dataset's version the store was built from. */
  dataset_version: number | null;
  /** The backing dataset's version now. */
  current_dataset_version: number | null;
  /**
   * The store was built from an older dataset version. A version can rewrite
   * any row and renumbers every ordinal, so no incremental delta expresses it:
   * lag will keep climbing and only a rebuild clears it.
   *
   * Unlike the counters above this is *not* withheld from a policied caller —
   * a version number counts versions, not rows — so it is null only when the
   * type has no store at all.
   */
  stale_version: boolean | null;
  /** The store was built from an older object-type definition. Same remedy. */
  stale_definition: boolean | null;
}

export interface ObjectTypeDetail extends ObjectTypeDef {
  links: LinkTypeDef[];
  actions: ActionDef[];
  index?: ObjectIndexStatus;
}

/** Result of folding the edit overlay into a new backing-dataset version. */
export interface WritebackResult {
  object_type: string;
  /** Edit-log rows marked folded. 0 means nothing happened. */
  folded: number;
  /** The new version — or, when folded is 0, the unchanged current one. */
  version: number | null;
  /** Absent on the no-edits early return, so it must stay optional. */
  row_count?: number;
  /** Objects reindexed after the write; null when the type has no
   *  materialization, which is normal rather than a failure. */
  objects: number | null;
}

export type OntologyObject = Record<string, unknown> & {
  __pk: string;
  __title: string;
};

export interface ObjectQueryResult {
  objects: OntologyObject[];
  total: number;
  /**
   * True when `total` is a floor rather than an exact count. A search stops
   * counting past a cap, because counting every substring match is the one
   * thing no index makes cheap. Browsing is never capped.
   */
  total_capped?: boolean;
}

export type ScheduleTrigger = "cron" | "upstream";
export type ScheduleAction = "build" | "sync";

export interface Schedule {
  name: string;
  enabled: boolean;
  trigger: ScheduleTrigger;
  cron: string;
  upstream_dataset: string;
  action: ScheduleAction;
  targets: string[];
  source: string;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: "succeeded" | "failed" | null;
  /** R1: replaced `last_error`, which held the driver's own sentence. */
  last_failure: Failure | null;
  last_build_id: string | null;
  created_at: string;
  created_by: string;
  /** Present on the write response. Advisory; the save already succeeded. */
  warnings?: AuthoringWarning[];
}

export interface AuditEvent {
  id: number;
  timestamp: string;
  actor: string;
  action: string;
  /**
   * The open bag. It is OPERATIONAL on an admin-authored record, so only an
   * admin receives it on `GET /audit` — and everyone receives their own rows
   * whole on `GET /audit/mine`, because you cannot learn a secret from a row
   * you wrote. Absent, not `{}`: the difference is "you may not read this" vs
   * "there was nothing to read", and the screen has to say which.
   */
  details?: Record<string, unknown>;
}

export interface ApiToken {
  id: string;
  name: string;
  username: string | null;
  created_at: string;
  last_used_at: string | null;
}

// -- Pipeline (transform) authoring -----------------------------------------

export interface PipelineFileInfo {
  name: string;
  transforms: string[];
  /** The listing carries a boolean and no detail: "will not import" is an
   *  authoring fact, the reason belongs on the detail route. */
  failed: boolean;
  bytes: number;
}

export interface PipelineFileContent {
  name: string;
  content: string;
  /** Why this file will not import, if it will not. */
  failure: Failure | null;
}

export interface PipelineWriteResult {
  name: string;
  transforms: string[];
  /** Non-fatal: the file was written, but the DAG does not collect. */
  collect_error: Failure | null;
}

// -- Groups & ontology permissions ------------------------------------------

export type SubjectKind = "everyone" | "role" | "group" | "user";

export interface Grant {
  subject_kind: SubjectKind;
  subject: string;
  can_view: boolean;
  can_edit: boolean;
}

export interface ObjectTypeGrants {
  object_type: string;
  grants: Grant[];
}

export interface DatasetGrants {
  dataset: string;
  grants: Grant[];
}

// -- Row-level security & column masking ------------------------------------

export interface PolicySubject {
  subject_kind: SubjectKind;
  subject: string;
}

export interface RowRule extends PolicySubject {
  values: string[];
}

export interface RowPolicy {
  column: string;
  rules: RowRule[];
}

export type MaskMode = "null" | "redact" | "hash";

export interface ColumnMask {
  column: string;
  mode: MaskMode;
  exempt: PolicySubject[];
}

export interface DatasetPolicy {
  row_policy: RowPolicy | null;
  column_masks: ColumnMask[];
}

export interface DatasetPolicyEntry {
  dataset: string;
  policy: DatasetPolicy | null;
}

// -- Classification markings ------------------------------------------------

export interface Marking {
  name: string;
  description: string;
  created_at?: string;
}

export interface DatasetMarkingsEntry {
  dataset: string;
  explicit: string[];
  effective: string[];
}

export interface UserClearances {
  username: string;
  markings: string[];
}

export interface Group {
  name: string;
  members: string[];
  created_at?: string;
}

// -- Portability: workspace export / import ---------------------------------
//
// Mirrors laurelin/export/manifest.py and laurelin/export/reader.py. The shapes
// are wide because the manifest's job is to be *read*: an archive that omits a
// credential or a dataset's rows has to say so somewhere a person will look,
// and this is that somewhere.

/** Whether a dataset's bytes are in the archive — and if not, why not. */
export type DataState =
  | "included"
  | "elsewhere"
  | "elsewhere_absolute_path"
  | "metadata_only";

/** One secret the export refused to carry, and the route that puts it back. */
export interface Withheld {
  table: string;
  /** The row's primary key, or "*" when the field is withheld from every row. */
  row: string;
  /** A column, or a dotted path inside a JSON column. */
  field: string;
  reason: string;
  required_for: string;
  resupply: string;
}

export interface NotExportedTable {
  table: string;
  cls: string;
  reason: string;
  rebuild: string;
}

export interface DatasetPlan {
  name: string;
  kind: string;
  data_state: DataState;
  versions: number;
  parts: number;
  bytes: number;
  reason: string;
  note: string;
}

/** A principal the imported rules name but the archive cannot create. */
export interface PrincipalRef {
  kind: "user" | "group";
  name: string;
  referenced_by: string[];
}

export interface PipelineWarning {
  file: string;
  line: number;
  pattern: string;
  preview: string;
}

export interface ExportTableStat {
  rows: number;
  columns: string[];
}

export interface ExportOrigin {
  workspace_name: string;
  workspace_dir: string;
  origin_id: string;
  origin_slug: string;
  metadata_dialect: string;
  mode: string;
  multi_slug: string | null;
  data_plane: string;
}

export interface ExportScope {
  data: boolean;
  audit: boolean;
  membership: boolean;
  datasets: string[] | null;
}

export interface ExportManifest {
  format_version: number;
  laurelin_version: string;
  created_at: string;
  created_by: string;
  origin: ExportOrigin;
  scope: ExportScope;
  tables: Record<string, ExportTableStat>;
  datasets: DatasetPlan[];
  withheld: Withheld[];
  not_exported: NotExportedTable[];
  environment_resupply: string[];
  principals: PrincipalRef[];
  content_warnings: PipelineWarning[];
  nulled_error_fields: Record<string, number>;
  governance_fingerprint: GovernanceFingerprint | Record<string, never>;
  /** Preview only: the part set the manifest implies, before anything streams. */
  estimated_parts?: number;
  estimated_part_bytes?: number;
}

export interface Collision {
  kind: "dataset" | "group" | "marking" | "user";
  name: string;
  severity: "refusal" | "widening-risk" | "note";
  detail: string;
  resolution: string;
}

export interface QuarantinedClearance {
  username: string;
  marking: string;
  marking_at_source: string;
}

export interface ImportReport {
  applied: boolean;
  dry_run: boolean;
  merge: boolean;
  manifest: ExportManifest | null;
  target_not_pristine: Record<string, number>;
  collisions: Collision[];
  rows_imported: Record<string, number>;
  /** Rows that travelled and were deliberately not written — the bindings. */
  rows_quarantined: Record<string, number>;
  principals: PrincipalRef[];
  withheld: Withheld[];
  datasets: DatasetPlan[];
  marking_renames: Record<string, string>;
  quarantined_clearances: QuarantinedClearance[];
  dataset_renames: Record<string, string>;
  parts_written: number;
  bytes_written: number;
  files_written: string[];
  import_state: string;
  warnings: string[];
  /** The digest a `merge` must quote back, so a stale report cannot be applied. */
  report_sha256: string;
}

export interface ImportState {
  imported: boolean;
  import_state: string | null;
  pipelines_acknowledged: boolean;
  origin_id: string | null;
  imported_at: string | null;
  content_warnings: PipelineWarning[];
}

/** One (principal, dataset) answer, captured through every enforcement path. */
export interface GovernanceCell {
  can_view: boolean;
  can_edit: boolean;
  effective_markings: string[];
  decision: string;
  sql: string;
  table?: string;
  arrow?: string;
  duckdb?: string;
  rows?: Record<string, string[] | string>;
  visible?: Record<string, string[] | string>;
}

export interface GovernanceFingerprint {
  principals: string[];
  datasets: string[];
  /** Keyed "principal|dataset". */
  cells: Record<string, GovernanceCell>;
  /** Keyed "principal|api_name" -> [can_view, can_edit]. */
  object_types: Record<string, [boolean, boolean]>;
}

export interface FingerprintResponse {
  fingerprint: GovernanceFingerprint;
  /** Named principals the workspace could not resolve — after an inert import,
   *  this is every one of them, and that absence is the point. */
  unresolved_principals: string[];
}

/** On-disk permissions of one path in the workspace. Name only, never a path:
 *  the store's "path" on Postgres is a DSN. */
export interface FileSecurityEntry {
  name: string;
  exists: boolean;
  mode: string | null;
  world_accessible: boolean;
  group_accessible: boolean;
  /** Group *write*, reported apart from group read: it is an admin grant on
   *  metadata.db, not a share. */
  group_writable: boolean;
}

export interface WorkspaceFileSecurity {
  dialect: string;
  file_backed: boolean;
  store_is_remote: boolean;
  strict_mode: boolean;
  directory: FileSecurityEntry;
  files: FileSecurityEntry[];
  note: string | null;
}
