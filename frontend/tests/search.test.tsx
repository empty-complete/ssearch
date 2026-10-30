import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SearchApp } from '../src/SearchApp';
import { SearchController } from '../src/searchController';
import type {
  ResultMetadata, SearchRequest, SearchResponse, SearchService,
} from '../src/bridge/types';

// Only static text assets are substituted. The real page, controls and search
// state remain under test without widening Vite's filesystem permissions.
vi.mock('../../LICENSE.txt?raw', () => ({ default: 'Static license text fixture.' }));
vi.mock('../THIRD_PARTY_NOTICES.txt?raw', () => ({ default: 'Static notices text fixture.' }));

const sourceId = '11111111-1111-4111-8111-111111111111';
const motorsProjectId = '22222222-2222-4222-8222-222222222222';
const materialsProjectId = '33333333-3333-4333-8333-333333333333';
const firstResultId = '44444444-4444-4444-8444-444444444444';
const secondResultId = '55555555-5555-4555-8555-555555555555';

function result(result_id: string, folder: string): ResultMetadata {
  return {
    result_id,
    source_id: sourceId,
    name: 'Ведомость.xlsx',
    relative_path: `Проект\\${folder}\\СтадияП\\Ведомость.xlsx`,
    full_path: `C:\\Учебный каталог\\Проект\\${folder}\\СтадияП\\Ведомость.xlsx`,
    size: 42,
    modified_ns: 0,
    stale: false,
    project_id: motorsProjectId,
    matched_fields: ['name', 'path'],
    score: null,
    match_kind: 'lexical',
  };
}

function response(query: string, results: ResultMetadata[] = []): SearchResponse {
  return {
    results,
    query_interpretation: {
      original_query: query,
      normalized_query: query.toLowerCase(),
      project_id: null,
      expanded_terms: [],
    },
    applied_aliases: [],
    ambiguities: [],
    catalog_generation: 1,
    warnings: [],
  };
}

beforeEach(() => sessionStorage.clear());
afterEach(() => {
  cleanup();
  sessionStorage.clear();
});

describe('search page source scope', () => {
  it.each([
    { label: 'all authorized sources', scope: null },
    { label: 'no selected sources', scope: [] },
  ] satisfies Array<{ label: string; scope: string[] | null }>)
  ('preserves the request scope for $label', async ({ scope }) => {
    const requests: SearchRequest[] = [];
    const service: SearchService = {
      async search(request) {
        requests.push(request);
        return response(request.query);
      },
    };
    // Exercise the existing page without a missing-component or type-error RED.
    // The baseline ignores this additional scope; P5 must honor it end to end.
    const props = { service, scope };
    render(<SearchApp {...props} />);
    const user = userEvent.setup();
    await user.type(screen.getByRole('searchbox'), 'спецификация');
    await user.keyboard('{Enter}');

    await waitFor(() => expect(requests.at(-1)?.query).toBe('спецификация'));
    expect(requests.at(-1)?.source_ids).toEqual(scope);
  });
});

it('offers full project choices and refines the project without rewriting or remounting the query', async () => {
  const requests: SearchRequest[] = [];
  const ambiguities: SearchResponse['ambiguities'] = [{
    term: 'ПМ',
    options: [
      { project_id: motorsProjectId, canonical_name: 'Пермские моторы', source_ids: [sourceId] },
      { project_id: materialsProjectId, canonical_name: 'Поставка материалов', source_ids: [sourceId] },
    ],
  }];
  const service: SearchService = {
    async search(request) {
      requests.push(request);
      return { ...response(request.query), ambiguities };
    },
  };
  render(<SearchApp service={service} />);
  const user = userEvent.setup();
  const queryInput = screen.getByRole('searchbox');
  await user.type(queryInput, 'ПМ спецификация');
  await user.keyboard('{Enter}');

  const motorsChoice = await screen.findByRole('button', { name: /Пермские моторы/ });
  expect(await screen.findByRole('button', { name: /Поставка материалов/ })).toBeVisible();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await user.click(motorsChoice);

  await waitFor(() => expect(requests.at(-1)).toMatchObject({
    query: 'ПМ спецификация',
    project_id: '22222222-2222-4222-8222-222222222222',
  }));
  expect(screen.getByRole('searchbox')).toBe(queryInput);
  expect(queryInput).toHaveValue('ПМ спецификация');
});

it('retains the selected UUID after a reordered same-query refresh but never reveals while pending', async () => {
  const first = result(firstResultId, 'АР');
  const second = result(secondResultId, 'КМ');
  let completeRefresh!: (reply: SearchResponse) => void;
  let requests = 0;
  const controller = new SearchController({
    search: request => {
      requests += 1;
      return requests === 1
        ? Promise.resolve(response(request.query, [first, second]))
        : new Promise(resolve => { completeRefresh = resolve; });
    },
  });
  try {
    controller.edit('ведомость');
    await controller.submit();
    controller.select(firstResultId);
    expect(controller.revealCandidate()?.result_id).toBe(firstResultId);

    const refreshing = controller.submit();
    expect(controller.revealCandidate()).toBeNull();
    completeRefresh(response('ведомость', [second, first]));
    await refreshing;

    expect(controller.getSnapshot().selectedId).toBe('44444444-4444-4444-8444-444444444444');
    expect(controller.revealCandidate()?.full_path)
      .toBe('C:\\Учебный каталог\\Проект\\АР\\СтадияП\\Ведомость.xlsx');
  } finally {
    controller.dispose();
  }
});
