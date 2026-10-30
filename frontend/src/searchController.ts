import type { ProjectOption, ResultMetadata, SearchResponse, SearchService } from './bridge/types';
import { SearchClientError } from './bridge/client';

export interface SearchSnapshot {
  query: string;
  scope: string[] | null;
  project: ProjectOption | null;
  state: 'idle' | 'loading' | 'ready' | 'empty' | 'error';
  response: SearchResponse | null;
  error: string | null;
  selectedId: string | null;
  generation: number;
}

function sameScope(left: string[] | null, right: string[] | null) {
  return left === null || right === null ? left === right
    : left.length === right.length && left.every((id, index) => id === right[index]);
}

export class SearchController {
  private snapshot: SearchSnapshot;
  private listeners = new Set<() => void>();
  private abort: AbortController | null = null;
  private resultGeneration = -1;

  constructor(private service: SearchService, scope: string[] | null = null, query = '') {
    this.snapshot = {
      query, scope: scope === null ? null : [...scope], project: null,
      state: 'idle', response: null, error: null, selectedId: null, generation: 0,
    };
  }

  getSnapshot = () => this.snapshot;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  private update(next: Partial<SearchSnapshot>) {
    this.snapshot = { ...this.snapshot, ...next };
    this.listeners.forEach(listener => listener());
  }

  private invalidate(next: Partial<SearchSnapshot>) {
    this.abort?.abort();
    this.resultGeneration = -1;
    this.update({
      generation: this.snapshot.generation + 1, selectedId: null,
      response: null, error: null, state: 'idle', ...next,
    });
  }

  edit(query: string) {
    if (query !== this.snapshot.query) this.invalidate({ query, project: null });
  }

  setScope(scope: string[] | null) {
    if (sameScope(scope, this.snapshot.scope)) return;
    this.invalidate({ scope: scope === null ? null : [...scope], project: null });
    if (this.snapshot.query.trim()) void this.submit();
  }

  async chooseProject(projectId: string | null, generation = this.snapshot.generation) {
    if (generation !== this.snapshot.generation) return;
    // Clearing the current filter is also a recovery action after failure.
    // A new project still requires a current, accepted ambiguity response.
    if (projectId !== null && this.resultGeneration !== generation) return;
    if (projectId === null && !this.snapshot.project) return;
    const option = this.snapshot.response?.ambiguities.flatMap(item => item.options)
      .find(item => item.project_id === projectId);
    if (projectId !== null && !option) return;
    this.invalidate({ project: option ?? null });
    await this.submit();
  }

  async submit() {
    this.abort?.abort();
    const abort = new AbortController();
    this.abort = abort;
    const { query, scope, project, selectedId } = this.snapshot;
    const generation = this.snapshot.generation + 1;
    this.resultGeneration = -1;
    this.update({ generation, error: null, state: query.trim() ? 'loading' : 'idle' });
    if (!query.trim()) return;

    try {
      const response = await this.service.search({
        query, source_ids: scope === null ? null : [...scope], limit: 50,
        mode: 'hybrid', project_id: project?.project_id ?? null,
      }, abort.signal);
      if (abort.signal.aborted || generation !== this.snapshot.generation) return;
      this.resultGeneration = generation;
      this.update({
        response, state: response.results.length ? 'ready' : 'empty',
        selectedId: response.results.some(result => result.result_id === selectedId) ? selectedId : null,
      });
    } catch (error) {
      if (abort.signal.aborted || generation !== this.snapshot.generation) return;
      this.update({
        state: 'error', response: null, selectedId: null,
        error: error instanceof SearchClientError ? error.code : 'service_unavailable',
      });
    }
  }

  select(id: string | null) {
    if (this.resultGeneration !== this.snapshot.generation) return;
    const exists = this.snapshot.response?.results.some(result => result.result_id === id);
    this.update({ selectedId: exists ? id : null });
  }

  revealCandidate(id = this.snapshot.selectedId): ResultMetadata | null {
    if (this.resultGeneration !== this.snapshot.generation || this.snapshot.state !== 'ready') return null;
    return this.snapshot.response?.results.find(result => result.result_id === id) ?? null;
  }

  dispose() {
    this.abort?.abort();
    this.listeners.clear();
    this.resultGeneration = -1;
  }
}
