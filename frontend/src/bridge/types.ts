/** Projection of src/atom_search/service/contracts.py. Wire keys stay snake_case. */
export interface SearchRequest { query: string; source_ids?: string[] | null; limit?: number; mode?: 'hybrid' | 'lexical' | 'semantic'; project_id?: string | null }
export interface ResultMetadata { result_id: string; source_id: string; name: string; relative_path: string; full_path: string; size: number; modified_ns: number; stale: boolean; project_id: string | null; matched_fields: string[]; score: number | null; match_kind: string }
export interface ProjectOption { project_id: string; canonical_name: string; source_ids: string[] }
export interface SearchResponse { results: ResultMetadata[]; query_interpretation: { original_query: string; normalized_query: string; project_id: string | null; expanded_terms: string[] }; applied_aliases: Array<{ alias_id: string; project_id: string; source_id: string; matched_text: string; canonical_name: string }>; ambiguities: Array<{ term: string; options: ProjectOption[] }>; catalog_generation: number; warnings: string[] }
export interface WireError { code: string; message: string; retryable: boolean; details: Record<string, unknown> }
export type WireEnvelope<T> = { schema_version: number; request_id: string } & ({ data: T; error: null } | { data: null; error: WireError });
export interface SearchService { search(request: SearchRequest, signal: AbortSignal): Promise<SearchResponse> }
