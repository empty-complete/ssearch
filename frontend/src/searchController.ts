import type { ResultMetadata, SearchResponse, SearchService } from './bridge/types';

export interface SearchSnapshot {
  query: string;
  state: 'idle' | 'loading' | 'ready' | 'empty' | 'error';
  response: SearchResponse | null;
  selectedId: string | null;
  generation: number;
}

export class SearchController {
  private snapshot: SearchSnapshot = {
    query: '', state: 'idle', response: null, selectedId: null, generation: 0,
  };
  private listeners = new Set<() => void>();
  private abort: AbortController | null = null;
  private resultGeneration = -1;

  constructor(private service: SearchService) {}

  getSnapshot = () => this.snapshot;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  private update(next: Partial<SearchSnapshot>) {
    this.snapshot = { ...this.snapshot, ...next };
    this.listeners.forEach(listener => listener());
  }

  edit(query: string) {
    this.abort?.abort();
    this.resultGeneration = -1;
    this.update({
      query, generation: this.snapshot.generation + 1,
      selectedId: null, response: null, state: 'idle',
    });
  }

  async submit() {
    this.abort?.abort();
    const abort = new AbortController();
    this.abort = abort;
    const generation = this.snapshot.generation + 1;
    const query = this.snapshot.query;
    this.resultGeneration = -1;
    this.update({
      generation, response: null, selectedId: null,
      state: query.trim() ? 'loading' : 'idle',
    });
    if (!query.trim()) return;

    try {
      const response = await this.service.search({
        query, source_ids: null, limit: 50, mode: 'hybrid', project_id: null,
      }, abort.signal);
      if (abort.signal.aborted || generation !== this.snapshot.generation) return;
      this.resultGeneration = generation;
      this.update({ response, state: response.results.length ? 'ready' : 'empty' });
    } catch {
      if (abort.signal.aborted || generation !== this.snapshot.generation) return;
      this.update({ state: 'error', response: null, selectedId: null });
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
