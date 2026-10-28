import { describe, expect, it } from 'vitest';
import { SearchController } from '../src/searchController';
import type { SearchResponse, SearchService } from '../src/bridge/types';

const reply = (id: string): SearchResponse => ({ results: [{ result_id: id, source_id: 'source', name: 'Ведомость.xlsx', relative_path: id, full_path: `C:\\Проект\\${id}\\Ведомость.xlsx`, size: 42, modified_ns: 0, stale: false, project_id: null, matched_fields: ['name'], score: null, match_kind: 'lexical' }], query_interpretation: { original_query: id, normalized_query: id, project_id: null, expanded_terms: [] }, applied_aliases: [], ambiguities: [], catalog_generation: 1, warnings: [] });

describe('current query authority', () => {
  it('invalidates selection immediately when B replaces A, even if A replies late', async () => {
    const pending: Array<(value: SearchResponse) => void> = [];
    const signals: AbortSignal[] = [];
    const service: SearchService = { search: (_request, signal) => { signals.push(signal); return new Promise(resolve => pending.push(resolve)); } };
    const search = new SearchController(service);
    search.edit('A'); const a = search.submit();
    search.edit('B'); const b = search.submit();
    expect(signals[0].aborted).toBe(true);
    pending[1](reply('B')); await b;
    search.select('B');
    pending[0](reply('A')); await a;
    expect(search.getSnapshot().response?.results[0].result_id).toBe('B');
    expect(search.revealCandidate()?.result_id).toBe('B');
    search.edit('C');
    expect(search.revealCandidate()).toBeNull();
    search.select('B');
    expect(search.revealCandidate()).toBeNull();
  });
  it('keeps the typed query after failure and retries the same current query', async () => {
    let fail = true;
    const search = new SearchController({ search: async () => { if (fail) throw new Error('disconnected'); return reply('B'); } });
    search.edit('B'); await search.submit();
    expect(search.getSnapshot()).toMatchObject({ query: 'B', state: 'error', selectedId: null });
    fail = false; await search.submit();
    expect(search.getSnapshot().state).toBe('ready');
  });
});
