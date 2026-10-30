import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { SearchApp } from '../src/SearchApp';
import { SearchController } from '../src/searchController';
import { SearchPage } from '../src/search/SearchPage';
import type {
  ResultMetadata, SearchRequest, SearchResponse, SearchService,
} from '../src/bridge/types';

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

it('resets an enabled project choice after its search fails while retaining the query and source scope', async () => {
  const requests: SearchRequest[] = [];
  const service: SearchService = {
    async search(request) {
      requests.push(request);
      if (request.project_id) throw new Error('service_unavailable');
      const reply = response(request.query);
      reply.ambiguities = [{ term: 'ПМ', options: [
        { project_id: motorsProjectId, canonical_name: 'Пермские моторы', source_ids: [sourceId] },
      ] }];
      return reply;
    },
  };
  render(<SearchApp service={service} scope={[sourceId]} />);
  const user = userEvent.setup();
  const queryInput = screen.getByRole('searchbox');
  await user.type(queryInput, 'ПМ спецификация');
  await user.keyboard('{Enter}');
  await user.click(await screen.findByRole('button', { name: 'Пермские моторы', exact: true }));
  await screen.findByRole('alert');
  const reset = screen.getByRole('button', { name: 'Все проекты', exact: true });
  expect(reset).toBeEnabled();
  await user.click(reset);

  await waitFor(() => expect(requests.at(-1)).toMatchObject({
    query: 'ПМ спецификация',
    source_ids: ['11111111-1111-4111-8111-111111111111'],
    project_id: null,
  }));
  expect(screen.getByRole('searchbox')).toBe(queryInput);
  expect(queryInput).toHaveValue('ПМ спецификация');
  expect(screen.queryByText('Проект: Пермские моторы', { exact: true })).not.toBeInTheDocument();
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

it('selects the first result with ArrowDown from the query without intercepting composition or text navigation', async () => {
  const service: SearchService = {
    async search(request) {
      return response(request.query, [result(firstResultId, 'АР'), result(secondResultId, 'КМ')]);
    },
  };
  render(<SearchApp service={service} />);
  const user = userEvent.setup();
  const input = screen.getByRole<HTMLInputElement>('searchbox');
  await user.type(input, 'ведомость');
  await user.keyboard('{Enter}');
  const results = await screen.findByRole('listbox', { name: 'Найденные документы' });
  const options = within(results).getAllByRole('option');
  expect(input).toHaveFocus();

  fireEvent.compositionStart(input);
  fireEvent.keyDown(input, { key: 'ArrowDown', isComposing: true, keyCode: 229 });
  expect(input).toHaveFocus();
  expect(within(results).queryAllByRole('option', { selected: true })).toHaveLength(0);
  fireEvent.compositionEnd(input);

  input.setSelectionRange(4, 4);
  await user.keyboard('{ArrowLeft}');
  expect(input.selectionStart).toBe(3);
  await user.keyboard('{ArrowRight}');
  expect(input.selectionStart).toBe(4);
  await user.keyboard('{Home}');
  expect(input.selectionStart).toBe(0);
  await user.keyboard('{End}');
  expect(input.selectionStart).toBe(9);
  expect(input).toHaveFocus();
  expect(within(results).queryAllByRole('option', { selected: true })).toHaveLength(0);

  await user.keyboard('{ArrowDown}');
  expect(within(results).getByRole('option', { selected: true })).toBe(options[0]);
  expect(options[0]).toHaveAccessibleName(/Ведомость.xlsx/);
  expect(input).toHaveValue('ведомость');
});

it('keeps the default Explorer action disabled and explains its unavailability after selecting a result', async () => {
  const service: SearchService = {
    async search(request) {
      return response(request.query, [result(firstResultId, 'АР')]);
    },
  };
  render(<SearchApp service={service} />);
  const user = userEvent.setup();
  await user.type(screen.getByRole('searchbox'), 'ведомость');
  await user.keyboard('{Enter}');
  const results = await screen.findByRole('listbox', { name: 'Найденные документы' });
  await user.click(within(results).getByRole('option'));

  expect(screen.getByRole('button', { name: 'Показать в Проводнике' })).toBeDisabled();
  expect(screen.getByText(/Показ в Проводнике.*(?:недоступен|будет доступен)/)).toBeVisible();
});

it('does not accept the first A reply after editing A to B and back to A', async () => {
  const pending: Array<(reply: SearchResponse) => void> = [];
  const controller = new SearchController({
    search: () => new Promise(resolve => pending.push(resolve)),
  });
  try {
    controller.edit('A'); const firstA = controller.submit();
    controller.edit('B'); const b = controller.submit();
    controller.edit('A'); const currentA = controller.submit();
    pending[2](response('A', [result(firstResultId, 'Текущая выдача')])); await currentA;
    controller.select(firstResultId);
    pending[0](response('A', [result(secondResultId, 'Старая выдача')])); await firstA;
    pending[1](response('B', [result(secondResultId, 'Другой запрос')])); await b;
    expect(controller.revealCandidate()?.full_path)
      .toBe('C:\\Учебный каталог\\Проект\\Текущая выдача\\СтадияП\\Ведомость.xlsx');
  } finally { controller.dispose(); }
});

it('invalidates a pending project choice when the source scope changes to an empty list', async () => {
  const pending: Array<{ request: SearchRequest; resolve: (reply: SearchResponse) => void }> = [];
  const controller = new SearchController({
    search: request => new Promise(resolve => pending.push({ request, resolve })),
  }, [sourceId]);
  try {
    controller.edit('ПМ');
    const initial = controller.submit();
    const ambiguous = response('ПМ');
    ambiguous.ambiguities = [{ term: 'ПМ', options: [
      { project_id: motorsProjectId, canonical_name: 'Пермские моторы', source_ids: [sourceId] },
    ] }];
    pending[0].resolve(ambiguous); await initial;
    const project = controller.chooseProject(motorsProjectId);
    controller.setScope([]);
    expect(pending[2].request).toMatchObject({ query: 'ПМ', source_ids: [], project_id: null });
    pending[2].resolve(response('ПМ')); await Promise.resolve();
    pending[1].resolve(response('ПМ', [result(firstResultId, 'АР')])); await project;
    expect(controller.getSnapshot().state).toBe('empty');
    expect(controller.getSnapshot().response?.results).toEqual([]);
    expect(controller.revealCandidate(firstResultId)).toBeNull();
  } finally { controller.dispose(); }
});

it('clears a selected UUID that disappears from the refreshed catalogue', async () => {
  let refresh = false;
  const controller = new SearchController({ async search(request) {
    return response(request.query, refresh ? [result(secondResultId, 'КМ')] : [result(firstResultId, 'АР')]);
  } });
  try {
    controller.edit('ведомость'); await controller.submit(); controller.select(firstResultId);
    refresh = true; await controller.submit();
    expect(controller.getSnapshot().selectedId).toBeNull();
    expect(controller.revealCandidate(firstResultId)).toBeNull();
  } finally { controller.dispose(); }
});

it('guards row composition and pending-query Enter even with an injected reveal port', async () => {
  let finishB!: (reply: SearchResponse) => void;
  const revealed: string[] = [];
  const service: SearchService = { async search(request) {
    return request.query === 'B' ? new Promise<SearchResponse>(resolve => { finishB = resolve; })
      : response(request.query, [result(firstResultId, 'АР')]);
  } };
  render(<SearchPage client={service} scope={null} mode="application" onReveal={async id => {
    revealed.push(id); return { status: 'revealed' };
  }} />);
  const user = userEvent.setup();
  const input = screen.getByRole('searchbox');
  await user.type(input, 'A'); await user.keyboard('{Enter}');
  await screen.findByRole('listbox', { name: 'Найденные документы' });
  await user.keyboard('{ArrowDown}');
  const row = screen.getByRole('option', { selected: true });
  fireEvent.compositionStart(row);
  fireEvent.keyDown(row, { key: 'Enter', isComposing: true, keyCode: 229 });
  fireEvent.keyUp(row, { key: 'Enter', isComposing: true, keyCode: 229 });
  fireEvent.compositionEnd(row);
  expect(revealed).toEqual([]);
  await user.click(input); await user.clear(input); await user.type(input, 'B');
  await user.keyboard('{Enter}');
  expect(revealed).toEqual([]);
  await act(async () => { finishB(response('B', [result(secondResultId, 'КМ')])); });
  await user.keyboard('{ArrowDown}{Enter}');
  expect(revealed).toEqual(['55555555-5555-4555-8555-555555555555']);
});

it('renders filename markup as safe literal text while highlighting matched terms', async () => {
  const file = { ...result(firstResultId, 'АР'), name: '<img src=x onerror=alert(1)>.xlsx' };
  const service: SearchService = { async search(request) { return response(request.query, [file]); } };
  const { container } = render(<SearchPage client={service} scope={null} mode="onboarding" />);
  const user = userEvent.setup();
  await user.type(screen.getByRole('searchbox'), 'img'); await user.keyboard('{Enter}');
  expect(await screen.findByRole('option')).toHaveTextContent('<img src=x onerror=alert(1)>.xlsx');
  expect(container.querySelector('img,script')).toBeNull();
  expect(container.querySelector('mark')).toHaveTextContent('img');
  expect(sessionStorage.length).toBe(0);
});

it('reconnects with the retained query and scope after a service failure', async () => {
  let offline = true;
  const requests: SearchRequest[] = [];
  const service: SearchService = { async search(request) {
    requests.push(request);
    if (offline) throw new Error('service_unavailable');
    return response(request.query, [result(firstResultId, 'АР')]);
  } };
  render(<SearchPage client={service} scope={[sourceId]} mode="widget" />);
  const user = userEvent.setup();
  const input = screen.getByRole('searchbox');
  await user.type(input, 'ведомость'); await user.keyboard('{Enter}');
  await screen.findByRole('alert');
  offline = false;
  await user.click(screen.getByRole('button', { name: 'Повторить соединение' }));
  await screen.findByRole('option');
  expect(input).toHaveValue('ведомость');
  expect(requests.at(-1)).toMatchObject({ query: 'ведомость', source_ids: [sourceId] });
});
