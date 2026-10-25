import type { ResultMetadata, SearchService, WireEnvelope, SearchResponse } from '../bridge/types';

const row = (result_id: string, folder: string): ResultMetadata => ({
  result_id,
  source_id: '16af7473-ab3c-4d5a-9f30-e028763629c4',
  name: 'Ведомость.xlsx',
  relative_path: `Северный завод\\${folder}\\СтадияП\\Ведомость.xlsx`,
  full_path: `C:\\Учебные документы\\Северный завод\\${folder}\\СтадияП\\Ведомость.xlsx`,
  size: 42800,
  modified_ns: 1792868400000000000,
  stale: folder === 'КМ',
  project_id: null,
  matched_fields: ['name', 'path'],
  score: null,
  match_kind: 'lexical',
});

export const mockService: SearchService = {
  async search(request, signal) {
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(resolve, request.query === 'медленно' ? 1500 : 180);
      signal.addEventListener('abort', () => {
        clearTimeout(timer);
        reject(new DOMException('Aborted', 'AbortError'));
      }, { once: true });
    });
    if (request.query === 'ошибка') throw new Error('service_unavailable');

    const envelope: WireEnvelope<SearchResponse> = {
      schema_version: 1,
      request_id: crypto.randomUUID(),
      error: null,
      data: {
        results: request.query === 'пусто' ? [] : [
          row('947e61b4-5200-4f7e-8bbc-ced7be362d7b', 'АР'),
          row('c8b0b9c4-d68a-4c20-a9da-031df6728856', 'КМ'),
        ],
        query_interpretation: {
          original_query: request.query,
          normalized_query: request.query.toLowerCase(),
          project_id: null,
          expanded_terms: [],
        },
        applied_aliases: [],
        ambiguities: [],
        catalog_generation: 1,
        warnings: request.query === 'частично' ? ['source_unavailable'] : [],
      },
    };
    return envelope.data;
  },
};
