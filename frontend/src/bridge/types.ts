/** Shared, host-free P2 projection; type-only exports never enter the renderer bundle. */
export type {
  SearchRequest, ResultMetadata, ProjectOption, SearchResponse, WireError, WireEnvelope,
  JsonObject, JsonValue, SettingsSnapshot, SettingsValidation, JobSnapshot,
  HealthSnapshot, DesktopApi, CallControl,
} from '../../../desktop/src/contracts';
import type {
  SearchRequest, SearchResponse, MethodArguments, MethodResults,
} from '../../../desktop/src/contracts';

export interface SearchService {
  search(request: SearchRequest, signal: AbortSignal): Promise<SearchResponse>;
}
export interface SearchClient extends SearchService {
  search(request: SearchRequest, signal?: AbortSignal): Promise<SearchResponse>;
  fetch(request: MethodArguments['fetch'], signal?: AbortSignal): Promise<MethodResults['fetch']>;
  status(signal?: AbortSignal): Promise<MethodResults['status']>;
  settingsGet(signal?: AbortSignal): Promise<MethodResults['settings_get']>;
  settingsValidate(request: MethodArguments['settings_validate'], signal?: AbortSignal): Promise<MethodResults['settings_validate']>;
  settingsApply(request: MethodArguments['settings_apply'], signal?: AbortSignal): Promise<MethodResults['settings_apply']>;
  startCatalog(request: MethodArguments['start_catalog'], signal?: AbortSignal): Promise<MethodResults['start_catalog']>;
  cancelJob(request: MethodArguments['cancel_job'], signal?: AbortSignal): Promise<MethodResults['cancel_job']>;
}
