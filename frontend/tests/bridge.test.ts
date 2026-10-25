import { describe, expect, it } from 'vitest';
import { SearchClientError, createSearchClient } from '../src/bridge/client';
import type { CallControl, DesktopApi, SearchResponse, WireEnvelope } from '../src/bridge/types';

function closedApi(overrides: Partial<DesktopApi>): DesktopApi {
  const deny = async (_args: unknown, control: CallControl): Promise<WireEnvelope<never>> => ({
    schema_version: 1, request_id: control.request_id, data: null,
    error: { code: 'access_denied', message: 'access_denied', retryable: false, details: {} },
  });
  return {
    search: deny, fetch: deny, status: deny, settingsGet: deny,
    settingsValidate: deny, settingsApply: deny, startCatalog: deny, cancelJob: deny,
    ...overrides,
  };
}

const response: SearchResponse = {
  results: [], query_interpretation: {
    original_query: 'drawing', normalized_query: 'drawing', project_id: null, expanded_terms: [],
  }, applied_aliases: [], ambiguities: [], catalog_generation: 4, warnings: [],
};

describe('typed browser bridge', () => {
  it('preserves null versus empty scopes and wire error code/details', async () => {
    const requests: unknown[] = [];
    const api = {
      async search(args, control) {
        requests.push(args);
        return { schema_version: 1, request_id: control.request_id, data: response, error: null };
      },
      async settingsApply(_args, control) {
        return { schema_version: 1, request_id: control.request_id, data: null,
          error: { code: 'revision_conflict', message: 'revision_conflict', retryable: false,
            details: { current_revision: 5 } } };
      },
    } satisfies Pick<DesktopApi, 'search' | 'settingsApply'>;
    const client = createSearchClient(closedApi(api));
    for (const source_ids of [null, []]) expect(await client.search({
      query: 'drawing', source_ids, mode: 'literal',
    })).toEqual(response);
    expect(requests).toEqual([
      { query: 'drawing', source_ids: null, mode: 'literal' },
      { query: 'drawing', source_ids: [], mode: 'literal' },
    ]);
    await expect(client.settingsApply({ candidate: {}, expected_revision: 4 })).rejects.toMatchObject({
      code: 'revision_conflict', details: { current_revision: 5 },
    });
  });

  it('keeps AbortSignal local, cancels by bounded request ID and rejects a late success', async () => {
    const controls: unknown[] = [];
    const args: unknown[] = [];
    let release: (response: WireEnvelope<SearchResponse>) => void = () => {};
    let id = '';
    const api: Pick<DesktopApi, 'search'> = {
      search(request, control) {
        controls.push(control);
        args.push(request);
        id = control.request_id;
        if (control.cancel) return Promise.resolve({
          schema_version: 1, request_id: id, data: null,
          error: { code: 'request_cancelled', message: 'request_cancelled', retryable: false, details: {} },
        });
        return new Promise(resolve => { release = resolve; });
      },
    };
    const client = createSearchClient(closedApi(api));
    const abort = new AbortController();
    const pending = client.search({ query: 'drawing' }, abort.signal);
    abort.abort();
    await expect(pending).rejects.toMatchObject({ code: 'request_cancelled' });
    release({ schema_version: 1, request_id: id, data: response, error: null });
    expect(controls).toEqual([{ request_id: id }, { request_id: id, cancel: true }]);
    expect(args).toEqual([{ query: 'drawing' }, { query: 'drawing' }]);
  });

  it('rejects forged reply IDs and preserves unknown mutation outcome without retry', async () => {
    let calls = 0;
    const api: Pick<DesktopApi, 'settingsApply' | 'status'> = {
      async settingsApply(_args, control) {
        calls++;
        return { schema_version: 1, request_id: control.request_id, data: null,
          error: { code: 'request_timeout', message: 'request_timeout', retryable: false,
            details: { outcome: 'unknown' } } };
      },
      async status() {
        return { schema_version: 1, request_id: 'forged', data: null,
          error: { code: 'access_denied', message: 'access_denied', retryable: false, details: {} } };
      },
    };
    const client = createSearchClient(closedApi(api));
    await expect(client.settingsApply({ candidate: {}, expected_revision: 1 })).rejects.toMatchObject({
      code: 'request_timeout', details: { outcome: 'unknown' },
    });
    expect(calls).toBe(1);
    await expect(client.status()).rejects.toBeInstanceOf(SearchClientError);
    await expect(client.status()).rejects.toMatchObject({ code: 'invalid_service_response' });
  });

  it('keeps mutation outcome unknown for malformed reply correlation and error codes', async () => {
    for (const fault of ['correlation', 'error_code']) {
      let calls = 0;
      const client = createSearchClient(closedApi({
        async settingsApply(_args, control) {
          calls++;
          return {
            schema_version: 1, request_id: fault === 'correlation' ? 'forged' : control.request_id,
            data: null, error: {
              code: fault === 'error_code' ? 'BAD_ERROR' : 'access_denied',
              message: 'synthetic malformed response', retryable: false, details: {},
            },
          };
        },
      }));
      await expect(client.settingsApply({ candidate: {}, expected_revision: 1 })).rejects.toMatchObject({
        code: 'invalid_service_response', details: { outcome: 'unknown' },
      });
      expect(calls).toBe(1);
    }
  });
});
