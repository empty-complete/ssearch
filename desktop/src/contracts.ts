/** P2 DTO projection. This module has no runtime host imports. */
export type JsonValue = null | boolean | number | string | JsonValue[] | JsonObject;
export interface JsonObject { [key: string]: JsonValue }
export interface SearchRequest {
  query: string;
  source_ids?: string[] | null;
  limit?: number;
  mode?: 'literal' | 'lexical' | 'hybrid';
  project_id?: string | null;
}
export interface ResultMetadata {
  result_id: string;
  source_id: string;
  name: string;
  relative_path: string;
  full_path: string;
  size: number;
  modified_ns: number;
  stale: boolean;
  project_id: string | null;
  matched_fields: string[];
  score: number | null;
  match_kind: string;
}
export interface ProjectOption {
  project_id: string;
  canonical_name: string;
  source_ids: string[];
}
export interface SearchResponse {
  results: ResultMetadata[];
  query_interpretation: {
    original_query: string;
    normalized_query: string;
    project_id: string | null;
    expanded_terms: string[];
  };
  applied_aliases: Array<{
    alias_id: string;
    project_id: string;
    source_id: string;
    matched_text: string;
    canonical_name: string;
  }>;
  ambiguities: Array<{ term: string; options: ProjectOption[] }>;
  catalog_generation: number;
  warnings: string[];
}
export interface WireError {
  code: string;
  message: string;
  retryable: boolean;
  details: JsonObject;
}
export type WireEnvelope<T> = { schema_version: 1; request_id: string } & (
  { data: T; error: null } | { data: null; error: WireError }
);
export interface SettingsSnapshot {
  schema_version: number;
  revision: number;
  settings: JsonObject;
  mutable_fields: string[];
}
export interface SettingsValidation {
  valid: boolean;
  expected_revision: number;
  normalized: JsonObject | null;
  diff: Array<{ path: string; before: JsonValue; after: JsonValue }>;
  warnings: string[];
  errors: Array<{ path: string; code: string; message: string }>;
}
export interface JobSnapshot {
  job_id: string;
  source_id: string | null;
  state: string;
  phase: string;
  started_at: string | null;
  last_progress_at: string | null;
  discovered: number;
  processed: number;
  succeeded: number;
  skipped: number;
  errors: number;
  total: number | null;
  error_code: string | null;
}
export interface HealthSnapshot {
  liveness: string;
  readiness: string;
  app_version: string;
  service_version: string;
  api_version: string;
  observed_at: string;
  config_version: { schema_version: number; revision: number };
  index_generation: number;
  components: {
    sources: Array<{
      source_id: string; state: string; checked_at: string | null;
      last_success_at: string | null; stale: boolean; error_code: string | null;
      catalogued_files: number | null; metadata_embedded_files: number | null;
      catalog_counts_checked_at: string | null; catalog_counts_stale: boolean;
    }>;
    model: {
      state: string; model_id: string | null; checked_at: string | null;
      stale: boolean; lexical_available: boolean;
    };
    database: {
      engine: string; version: string | null; extension_version: string | null;
      runtime_state: string; checked_at: string | null; stale: boolean; error_code: string | null;
    };
    storage: {
      physical_owned_bytes: number | null; pgdata_bytes: number | null;
      wal_bytes: number | null; temp_bytes: number | null; cache_bytes: number | null;
      outbox_bytes: number | null; backup_bytes: number | null; staging_bytes: number | null;
      database_logical_bytes: number | null; quota_bytes: number | null;
      free_bytes: number | null; reserved_bytes: number | null;
      checked_at: string | null; stale: boolean; maintenance_required: boolean;
    };
    jobs: JobSnapshot[];
    outbox: { queued: number; sent: number; last_error: string | null; repair_blocked: boolean };
  };
  capabilities: string[];
}
export interface MethodArguments {
  search: SearchRequest;
  fetch: { result_id: string; source_ids?: string[] | null };
  status: Record<string, never>;
  settings_get: Record<string, never>;
  settings_validate: { candidate: JsonObject; expected_revision: number };
  settings_apply: { candidate: JsonObject; expected_revision: number };
  start_catalog: { source_ids?: string[] | null };
  cancel_job: { job_id: string };
}
export interface MethodResults {
  search: SearchResponse;
  fetch: ResultMetadata;
  status: HealthSnapshot;
  settings_get: SettingsSnapshot;
  settings_validate: SettingsValidation;
  settings_apply: SettingsSnapshot;
  start_catalog: { job_ids: string[] };
  cancel_job: JobSnapshot;
}
export type UiMethod = keyof MethodArguments;
/** Plain values only: AbortSignal stays in the browser client. */
export interface CallControl { request_id: string; cancel?: true }
export interface DesktopApi {
  search(args: SearchRequest, control: CallControl): Promise<WireEnvelope<SearchResponse>>;
  fetch(args: MethodArguments['fetch'], control: CallControl): Promise<WireEnvelope<ResultMetadata>>;
  status(args: MethodArguments['status'], control: CallControl): Promise<WireEnvelope<HealthSnapshot>>;
  settingsGet(args: MethodArguments['settings_get'], control: CallControl): Promise<WireEnvelope<SettingsSnapshot>>;
  settingsValidate(args: MethodArguments['settings_validate'], control: CallControl): Promise<WireEnvelope<SettingsValidation>>;
  settingsApply(args: MethodArguments['settings_apply'], control: CallControl): Promise<WireEnvelope<SettingsSnapshot>>;
  startCatalog(args: MethodArguments['start_catalog'], control: CallControl): Promise<WireEnvelope<{ job_ids: string[] }>>;
  cancelJob(args: MethodArguments['cancel_job'], control: CallControl): Promise<WireEnvelope<JobSnapshot>>;
}
