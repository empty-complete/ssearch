import type { SearchClient, CallControl, DesktopApi, JsonObject, SearchService, WireEnvelope } from './types';

export class SearchClientError extends Error {
  constructor(readonly code: string, readonly details: JsonObject = {}, readonly retryable = false) {
    super(code);
    this.name = 'SearchClientError';
  }
}

/** AbortSignal never crosses contextBridge; cancellation uses the same named method. */
function call<A, T>(
  method: (args: A, control: CallControl) => Promise<WireEnvelope<T>>,
  args: A,
  signal?: AbortSignal,
  mutation = false,
): Promise<T> {
  if (signal?.aborted) return Promise.reject(new SearchClientError('request_cancelled'));
  const id = crypto.randomUUID();
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (complete: () => void) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener('abort', abort);
      complete();
    };
    const abort = () => {
      finish(() => reject(new SearchClientError('request_cancelled', mutation ? { outcome: 'unknown' } : {})));
      try {
        void method(args, { request_id: id, cancel: true }).catch(() => {});
      } catch { /* Cancellation never proves a running mutation was rolled back. */ }
    };
    signal?.addEventListener('abort', abort, { once: true });
    let pending: Promise<WireEnvelope<T>>;
    try {
      pending = method(args, { request_id: id });
    } catch {
      finish(() => reject(new SearchClientError('service_unavailable', mutation ? { outcome: 'unknown' } : {})));
      return;
    }
    void pending.then(reply => {
      if (settled) return;
      if (!reply || reply.schema_version !== 1 || reply.request_id !== id
        || Object.keys(reply).sort().join(',') !== 'data,error,request_id,schema_version'
        || (reply.data === null) === (reply.error === null)) {
        finish(() => reject(new SearchClientError('invalid_service_response', mutation ? { outcome: 'unknown' } : {})));
      } else if (reply.error) {
        const error = reply.error;
        if (typeof error.code !== 'string' || !/^[a-z][a-z0-9_]{0,95}$/.test(error.code)
          || typeof error.retryable !== 'boolean' || !error.details || typeof error.details !== 'object') {
          finish(() => reject(new SearchClientError('invalid_service_response', mutation ? { outcome: 'unknown' } : {})));
        } else {
          finish(() => reject(new SearchClientError(error.code, error.details, error.retryable)));
        }
      } else {
        finish(() => resolve(reply.data));
      }
    }).catch(() => finish(() => reject(new SearchClientError('service_unavailable',
      mutation ? { outcome: 'unknown' } : {}))));
  });
}

export function createSearchClient(api: DesktopApi): SearchClient {
  return {
    search: (args, signal) => call(api.search, args, signal),
    fetch: (args, signal) => call(api.fetch, args, signal),
    status: signal => call(api.status, {}, signal),
    settingsGet: signal => call(api.settingsGet, {}, signal),
    settingsValidate: (args, signal) => call(api.settingsValidate, args, signal, true),
    settingsApply: (args, signal) => call(api.settingsApply, args, signal, true),
    startCatalog: (args, signal) => call(api.startCatalog, args, signal, true),
    cancelJob: (args, signal) => call(api.cancelJob, args, signal, true),
  };
}

export const unavailableService: SearchService = {
  async search() { throw new SearchClientError('service_unavailable'); },
};
