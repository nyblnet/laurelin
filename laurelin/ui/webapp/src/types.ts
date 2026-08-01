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
  root: string;
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

export interface Dataset {
  name: string;
  description: string;
  created_at: string;
  latest_version: number | null;
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
  filters: Record<string, string>;
  search_placeholder: string;
  actions: string[];
  links: string[];
  created_at: string;
  created_by: string;
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

export interface DashboardPanel {
  id: string;
  title: string;
  /** Source A: raw SQL over datasets. Exactly one source is set. */
  sql: string;
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

export interface Dashboard {
  name: string;
  title: string;
  description: string;
  panels: DashboardPanel[];
  created_at: string;
  created_by: string;
  updated_at: string;
}

export type SourceType = "postgres" | "http" | "file";

export interface Source {
  name: string;
  type: SourceType;
  dataset: string;
  // Secret-bearing values arrive redacted ("*****") from the API.
  config: Record<string, unknown>;
  created_at: string;
  created_by: string;
  last_sync_at: string | null;
  last_sync_status: "succeeded" | "failed" | null;
  last_sync_error: string | null;
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
  error: string | null;
  rows_written: number | null;
  output_version: number | null;
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
  error: string | null;
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
  objects: number;
}

export interface ObjectTypeDetail extends ObjectTypeDef {
  links: LinkTypeDef[];
  actions: ActionDef[];
  index?: ObjectIndexStatus;
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
  last_error: string | null;
  last_build_id: string | null;
  created_at: string;
  created_by: string;
}

export interface AuditEvent {
  id: number;
  timestamp: string;
  actor: string;
  action: string;
  details: Record<string, unknown>;
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
  error: string | null;
  bytes: number;
}

export interface PipelineFileContent {
  name: string;
  content: string;
}

export interface PipelineWriteResult {
  name: string;
  transforms: string[];
  collect_error: string | null;
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
