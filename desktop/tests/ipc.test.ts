import { test } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { RendererBoundary, operationBudget, type ServicePort } from '../src/ipc.js';

// Synthetic service port proves policy only. No broker/owner/PG proof is implied.
function fixture(owner = true, timers?: Array<() => void>) {
  const frame = { url: 'ssearch://ui/' };
  const session = {};
  const contents = { mainFrame: frame, session, isDestroyed: () => false };
  const event = { sender: contents, senderFrame: frame };
  const calls: unknown[] = [];
  const port: ServicePort = {
    authority: { generation: randomUUID(), owner, capabilities: new Set([
      'settings_validate', 'settings_apply', 'start_catalog', 'cancel_job',
    ]) },
    async request(method, args, id) {
      calls.push([method, args]);
      return { schema_version: 1, request_id: id, data: { value: args }, error: null };
    },
  };
  const boundary = new RendererBoundary(contents as never, session as never, port,
    timers ? callback => { timers.push(callback); return () => {}; } : undefined);
  const context = boundary.handshake(event as never);
  const request = (method: string, args: unknown, control: object = {}) => ({
    ...context, request_id: randomUUID(), method, arguments: args, ...control,
  });
  return { boundary, event, contents, frame, port, calls, request };
}

test('validated scope and owner revision reach the service unchanged', async () => {
  const f = fixture();
  for (const source_ids of [null, []]) {
    const response = await f.boundary.invoke(f.event as never, f.request('search', {
      query: ' чертёж ', source_ids, mode: 'literal', limit: 100, project_id: null,
    }));
    assert.equal(response.error, null);
  }
  const response = await f.boundary.invoke(f.event as never, f.request('settings_apply', {
    candidate: { ui: { theme: 'dark' } }, expected_revision: 4,
  }));
  assert.equal(response.error, null);
  assert.deepEqual(f.calls, [
    ['search', { query: ' чертёж ', source_ids: null, mode: 'literal', limit: 100, project_id: null }],
    ['search', { query: ' чертёж ', source_ids: [], mode: 'literal', limit: 100, project_id: null }],
    ['settings_apply', { candidate: { ui: { theme: 'dark' } }, expected_revision: 4 }],
  ]);
});

test('Unicode query bound counts code points and UTF-8 request size stays bounded', async () => {
  const f = fixture();
  assert.equal((await f.boundary.invoke(f.event as never, f.request('search', {
    query: '🪐'.repeat(512), source_ids: [],
  }))).error, null);
  assert.equal((await f.boundary.invoke(f.event as never, f.request('search', {
    query: '🪐'.repeat(513),
  }))).error?.code, 'invalid_request');
  assert.equal((await f.boundary.invoke(f.event as never, f.request('settings_apply', {
    candidate: { value: '🪐'.repeat(17_000) }, expected_revision: 1,
  }))).error?.code, 'request_too_large');
});

test('forged identity, foreign sender/frame/origin and invalid input stop before service', async () => {
  const f = fixture();
  for (const [method, args] of [
    ['shell', {}], ['status', { token: 'private-value' }],
    ['search', { query: 'x', mode: 'semantic' }], ['search', { query: '  ' }],
    ['search', { query: 'x', limit: true }], ['fetch', { result_id: '1' }],
    ['fetch', { result_id: 'A9B00000-0000-0000-0000-000000000000' }],
    ['search', { query: 'x', source_ids: Array(65).fill(randomUUID()) }],
    ['settings_apply', { candidate: {}, expected_revision: true }],
    ['settings_apply', { candidate: { x: 'x'.repeat(65_536) }, expected_revision: 1 }],
  ] as const) {
    assert.notEqual((await f.boundary.invoke(f.event as never, f.request(method, args))).error, null);
  }
  assert.equal((await f.boundary.invoke(f.event as never,
    f.request('status', {}, { session_id: randomUUID() }))).error?.code, 'access_denied');
  for (const event of [
    { ...f.event, sender: { ...f.contents } },
    { ...f.event, senderFrame: { ...f.frame } },
  ]) assert.equal((await f.boundary.invoke(event as never, f.request('status', {}))).error?.code, 'access_denied');
  f.frame.url = 'ssearch://foreign/';
  assert.equal((await f.boundary.invoke(f.event as never, f.request('status', {}))).error?.code, 'access_denied');
  assert.deepEqual(f.calls, []);
});

test('default port is unavailable and every mutation needs trusted owner authority', async () => {
  const f = fixture(false);
  for (const [method, args] of [
    ['settings_validate', { candidate: {}, expected_revision: 1 }],
    ['settings_apply', { candidate: {}, expected_revision: 1 }],
    ['start_catalog', { source_ids: null }], ['cancel_job', { job_id: randomUUID() }],
  ] as const) assert.equal((await f.boundary.invoke(f.event as never,
    f.request(method, args))).error?.code, 'access_denied');
  assert.deepEqual(f.calls, []);
  f.port.authority = null;
  assert.equal((await f.boundary.invoke(f.event as never, f.request('status', {}))).error?.code, 'service_unavailable');
});

test('64 pending limit, cancellation and rollover reject stale replies', async () => {
  const f = fixture();
  const replies: Array<() => void> = [];
  f.port.request = (_method, _args, id) => new Promise(resolve => replies.push(() => resolve({
    schema_version: 1, request_id: id, data: { ok: true }, error: null,
  })));
  const first = f.request('status', {});
  const pending = [f.boundary.invoke(f.event as never, first)];
  for (let i = 1; i < 64; i++) pending.push(f.boundary.invoke(f.event as never, f.request('status', {})));
  assert.equal((await f.boundary.invoke(f.event as never, f.request('status', {}))).error?.code, 'too_many_requests');
  await f.boundary.invoke(f.event as never, { ...first, cancel: true });
  assert.equal((await pending[0]).error?.code, 'request_cancelled');
  f.boundary.rollover();
  assert.equal((await pending[1]).error?.code, 'stale_session');
  replies.forEach(reply => reply());
  assert.equal((await pending[0]).error?.code, 'request_cancelled');
  assert.equal(operationBudget('settings_apply') >= 30_000, true);
});

test('only validated reads can explicitly retry unavailable MAIN readiness', async () => {
  const f = fixture();
  const authority = f.port.authority;
  f.port.authority = null;
  let attempts = 0;
  f.port.ensureReadConnection = async () => {
    attempts++;
    if (attempts === 1) throw new Error('private startup failure');
    f.port.authority = authority;
  };
  assert.equal((await f.boundary.invoke(f.event as never,
    f.request('settings_apply', { candidate: {}, expected_revision: 1 }))).error?.code, 'access_denied');
  assert.equal((await f.boundary.invoke(f.event as never,
    f.request('status', { owner: true }))).error?.code, 'invalid_request');
  assert.equal((await f.boundary.invoke({ ...f.event, sender: {} } as never,
    f.request('status', {}))).error?.code, 'access_denied');
  assert.equal(attempts, 0);
  const failed = await f.boundary.invoke(f.event as never, f.request('status', {}));
  assert.equal(failed.error?.code, 'service_unavailable');
  assert.equal(JSON.stringify(failed).includes('private startup failure'), false);
  assert.deepEqual(f.calls, []);
  assert.equal((await f.boundary.invoke(f.event as never, f.request('status', {}))).error, null);
  assert.equal(attempts, 2);
  assert.deepEqual(f.calls, [['status', {}]]);
});

test('read readiness waits stay bounded and cannot dispatch after timeout or renderer rollover', async () => {
  const timers: Array<() => void> = [];
  const f = fixture(true, timers);
  const authority = f.port.authority;
  f.port.authority = null;
  let ready!: () => void;
  const waiting = new Promise<void>(resolve => { ready = resolve; });
  f.port.ensureReadConnection = () => waiting;
  const pending = Array.from({ length: 64 }, () => f.boundary.invoke(f.event as never, f.request('status', {})));
  assert.equal((await f.boundary.invoke(f.event as never, f.request('status', {}))).error?.code, 'too_many_requests');
  timers[0]();
  assert.equal((await pending[0]).error?.code, 'request_timeout');
  f.boundary.rollover();
  assert.equal((await pending[1]).error?.code, 'stale_session');
  f.port.authority = authority;
  ready();
  await Promise.all(pending);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(f.calls, []);
});

test('timeouts have unknown mutation outcome and late results cannot overwrite a new request', async () => {
  const timers: Array<() => void> = [];
  const f = fixture(true, timers);
  const releases: Array<() => void> = [];
  f.port.request = (_method, _args, id) => new Promise(resolve => releases.push(() => resolve({
    schema_version: 1, request_id: id, data: { revision: 2 }, error: null,
  })));
  const call = f.request('settings_apply', { candidate: {}, expected_revision: 1 });
  const pending = f.boundary.invoke(f.event as never, call);
  timers[0]();
  const response = await pending;
  assert.equal(response.error?.code, 'request_timeout');
  assert.deepEqual(response.error?.details, { outcome: 'unknown' });
  const newer = f.boundary.invoke(f.event as never, call);
  releases[0]();
  timers[1]();
  assert.equal((await newer).error?.code, 'request_timeout');
  f.boundary.close();
});

test('response correlation/size/error validation fails closed and does not echo exceptions', async () => {
  const f = fixture();
  f.port.request = async () => { throw new Error('token=private-value'); };
  const failure = await f.boundary.invoke(f.event as never, f.request('status', {}));
  assert.equal(failure.error?.code, 'service_unavailable');
  assert.equal(JSON.stringify(failure).includes('private-value'), false);
  f.port.request = async () => ({ schema_version: 1, request_id: randomUUID(), data: {}, error: null });
  assert.equal((await f.boundary.invoke(f.event as never, f.request('status', {}))).error?.code, 'invalid_service_response');
  f.port.request = async (_method, _args, id) => ({
    schema_version: 1, request_id: id, data: { text: 'x'.repeat(100_000) }, error: null,
  });
  assert.equal((await f.boundary.invoke(f.event as never, f.request('status', {}))).error, null);
  f.port.request = async (_method, _args, id) => ({
    schema_version: 1, request_id: id, data: { text: 'x'.repeat(2_097_152) }, error: null,
  });
  assert.equal((await f.boundary.invoke(f.event as never, f.request('status', {}))).error?.code, 'invalid_service_response');
});
