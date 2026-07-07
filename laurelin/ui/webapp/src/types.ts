// Types mirroring the Laurelin REST API (docs/ARCHITECTURE.md).

export type Role = "viewer" | "editor" | "admin";

export interface User {
  id: string;
  username: string;
  role: Role;
  created_at: string;
  disabled: boolean;
}

export interface AuthStatus {
  auth_required: boolean;
  setup_required: boolean;
  user: User | null;
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
}

export interface DatasetDetail extends Dataset {
  versions: DatasetVersion[];
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

export interface ObjectTypeDef {
  api_name: string;
  display_name: string | null;
  description: string;
  backing_dataset: string;
  primary_key: string;
  title_property: string | null;
  properties: Record<string, PropertyDef>;
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

export interface ObjectTypeDetail extends ObjectTypeDef {
  links: LinkTypeDef[];
  actions: ActionDef[];
}

export type OntologyObject = Record<string, unknown> & {
  __pk: string;
  __title: string;
};

export interface ObjectQueryResult {
  objects: OntologyObject[];
  total: number;
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
