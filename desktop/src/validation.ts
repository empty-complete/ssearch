import type { JsonObject, UiMethod, WireEnvelope } from './contracts.js';

export const methods = [
  'search', 'fetch', 'status', 'settings_get', 'settings_validate',
  'settings_apply', 'start_catalog', 'cancel_job',
] as const;
export const mutations = new Set<UiMethod>([
  'settings_validate', 'settings_apply', 'start_catalog', 'cancel_job',
]);

export function publicUuid(value: unknown): value is string {
  return typeof value === 'string'
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
}

export function plainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
    && [Object.prototype, null].includes(Object.getPrototypeOf(value));
}

export function exactKeys(value: Record<string, unknown>, required: readonly string[], optional: readonly string[] = []): boolean {
  return required.every(key => Object.hasOwn(value, key))
    && Object.keys(value).every(key => required.includes(key) || optional.includes(key));
}

/** Bounded JSON inspection before serialization; no toJSON, getters, cycles or classes. */
export function jsonBytes(value: unknown, limit: number): number {
  let size = 0;
  const ancestors = new Set<object>();
  const work: Array<{ value: unknown; exit?: boolean }> = [{ value }];
  while (work.length) {
    const item = work.pop()!;
    const current = item.value;
    if (item.exit) {
      ancestors.delete(current as object);
      continue;
    }
    if (current === null || typeof current === 'boolean' || typeof current === 'string'
      || (typeof current === 'number' && Number.isFinite(current))) {
      size += Buffer.byteLength(JSON.stringify(current), 'utf8');
    } else {
      if (!Array.isArray(current) && !plainObject(current)) throw new Error('invalid_request');
      if (ancestors.has(current)) throw new Error('invalid_request');
      ancestors.add(current);
      work.push({ value: current, exit: true });
      const keys = Object.keys(current);
      size += 2 + Math.max(0, keys.length - 1);
      if (Array.isArray(current) && (keys.length !== current.length
        || keys.some((key, index) => key !== String(index)))) throw new Error('invalid_request');
      for (const key of keys) {
        const descriptor = Object.getOwnPropertyDescriptor(current, key)!;
        if (!Object.hasOwn(descriptor, 'value')) throw new Error('invalid_request');
        if (!Array.isArray(current)) size += Buffer.byteLength(JSON.stringify(key), 'utf8') + 1;
        work.push({ value: descriptor.value });
      }
    }
    if (size > limit) throw new Error('request_too_large');
  }
  return size;
}

const fields: Record<UiMethod, [string[], string[]]> = {
  search: [['query'], ['source_ids', 'limit', 'mode', 'project_id']],
  fetch: [['result_id'], ['source_ids']],
  status: [[], []], settings_get: [[], []],
  settings_validate: [['candidate', 'expected_revision'], []],
  settings_apply: [['candidate', 'expected_revision'], []],
  start_catalog: [[], ['source_ids']], cancel_job: [['job_id'], []],
};

export function validateArguments(method: unknown, value: unknown): asserts method is UiMethod {
  if (typeof method !== 'string' || !methods.includes(method as UiMethod)) throw new Error('route_not_found');
  if (!plainObject(value)) throw new Error('invalid_request');
  jsonBytes(value, 65_536);
  const [required, optional] = fields[method as UiMethod];
  if (!exactKeys(value, required, optional)) throw new Error('invalid_request');
  if (Object.hasOwn(value, 'source_ids') && value.source_ids !== null) {
    if (!Array.isArray(value.source_ids) || value.source_ids.length > 64
      || !value.source_ids.every(publicUuid)) throw new Error('invalid_request');
  }
  for (const name of ['result_id', 'job_id']) {
    if (Object.hasOwn(value, name) && !publicUuid(value[name])) throw new Error('invalid_request');
  }
  if (method === 'search') {
    // Python str.strip() whitespace, including U+0085 and excluding U+FEFF.
    const trimmed = typeof value.query === 'string' ? value.query.replace(
      /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu, '',
    ) : '';
    if (typeof value.query !== 'string' || Array.from(trimmed).length < 1
      || Array.from(trimmed).length > 512
      || (Object.hasOwn(value, 'limit') && (!Number.isInteger(value.limit)
        || (value.limit as number) < 1 || (value.limit as number) > 100))
      || (Object.hasOwn(value, 'mode') && !['literal', 'lexical', 'hybrid'].includes(value.mode as string))
      || (Object.hasOwn(value, 'project_id') && value.project_id !== null && !publicUuid(value.project_id))) {
      throw new Error('invalid_request');
    }
  }
  if (method === 'settings_validate' || method === 'settings_apply') {
    if (!plainObject(value.candidate) || !Number.isInteger(value.expected_revision)
      || (value.expected_revision as number) < 1) throw new Error('invalid_request');
  }
}

export function validResponse(value: unknown, id: string): value is WireEnvelope<unknown> {
  try {
    if (!plainObject(value) || !exactKeys(value, ['schema_version', 'request_id', 'data', 'error'])
      || value.schema_version !== 1 || value.request_id !== id
      || (value.data === null) === (value.error === null)) return false;
    // Reserve bounded envelope overhead without narrowing the service's 2 MiB DTO.
    jsonBytes(value, 2_097_152 + 4096);
    if (value.data !== null) return jsonBytes(value.data, 2_097_152) <= 2_097_152;
    const error = value.error;
    return plainObject(error) && exactKeys(error, ['code', 'message', 'retryable', 'details'])
      && typeof error.code === 'string' && /^[a-z][a-z0-9_]{0,95}$/.test(error.code)
      && typeof error.message === 'string' && typeof error.retryable === 'boolean'
      && plainObject(error.details);
  } catch {
    return false;
  }
}

export function failure(id: string, code: string, details: JsonObject = {}): WireEnvelope<never> {
  return {
    schema_version: 1, request_id: id, data: null,
    error: { code, message: code, retryable: code === 'service_unavailable', details },
  };
}
