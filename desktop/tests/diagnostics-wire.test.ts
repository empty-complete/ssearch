import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PassThrough, Writable } from 'node:stream';
import { once } from 'node:events';
import { encodeBrokerFrame } from '../src/brokerFrames.js';
import { BrokerStdinAudit } from '../src/diagnosticsWire.js';

const requestId = '00000000-0000-0000-0000-000000000001';
function mutation(id = requestId) {
  return encodeBrokerFrame({ session_id: '00000000-0000-0000-0000-000000000002',
    generation: '00000000-0000-0000-0000-000000000003', envelope: {
      schema_version: 1, request_id: id, data: { method: 'settings_apply',
        arguments: { candidate: { ui: { theme: 'dark' } }, expected_revision: 1 } }, error: null,
    } });
}

function readRequest(index: number) {
  return encodeBrokerFrame({ session_id: '00000000-0000-0000-0000-000000000002',
    generation: '00000000-0000-0000-0000-000000000003', envelope: {
      schema_version: 1, request_id: `00000000-0000-0000-0000-${String(index).padStart(12, '0')}`,
      data: { method: 'status', arguments: {} }, error: null,
    } });
}

async function completedBroker(audit: BrokerStdinAudit, bytes?: Buffer): Promise<PassThrough> {
  const broker = new PassThrough();
  broker.resume();
  audit.observe(broker);
  if (bytes) broker.write(bytes);
  const finished = once(broker, 'finish');
  broker.end();
  await finished;
  return broker;
}

test('broker wire observation preserves bytes and counts a fragmented dispatch once', () => {
  const audit = new BrokerStdinAudit();
  const pipe = new PassThrough();
  const submitted: Buffer[] = [];
  pipe.on('data', bytes => submitted.push(Buffer.from(bytes)));
  audit.observe(pipe);
  const before = audit.snapshot();
  const bytes = mutation();
  pipe.write(bytes.subarray(0, 3));
  pipe.write(bytes.subarray(3));
  assert.deepEqual(Buffer.concat(submitted), bytes);
  assert.equal(audit.snapshot().settings_apply, 1);
  assert.doesNotThrow(() => audit.requireSingleMutation('settings_apply', requestId, before));
  pipe.end();
});

test('wire replay across reconnected brokers fails even when request revision cannot advance twice', () => {
  for (const replayId of [requestId, '00000000-0000-0000-0000-000000000004']) {
    const audit = new BrokerStdinAudit();
    const before = audit.snapshot();
    for (const id of [requestId, replayId]) {
      const broker = new PassThrough();
      broker.resume();
      audit.observe(broker);
      broker.write(encodeBrokerFrame({ schema_version: 1, session_id: 'bootstrap', challenge: 'bootstrap' }));
      broker.write(mutation(id));
      broker.end();
    }
    assert.equal(audit.snapshot().settings_apply, 2);
    assert.throws(() => audit.requireSingleMutation('settings_apply', requestId, before),
      { message: 'ui_proof_mutation_dispatch_not_once' });
  }
});

test('incomplete or unsupported stdin telemetry cannot establish single-dispatch proof', () => {
  for (const kind of ['partial', 'end_payload']) {
    const audit = new BrokerStdinAudit();
    const broker = new PassThrough();
    broker.resume();
    audit.observe(broker);
    if (kind === 'partial') broker.write(mutation().subarray(0, 8));
    else broker.end(mutation());
    assert.throws(() => audit.snapshot(), { message: 'ui_proof_wire_audit_incomplete' });
    broker.destroy();
  }
});

test('audit refusal exposes fixed categories without raw frames or child output', () => {
  const cases = ['duplicate_stream', 'end_payload_empty', 'end_payload_nonempty', 'unsupported_chunk',
    'frame_decode', 'frame_shape', 'truncated_frame', 'partial_snapshot'] as const;
  for (const category of cases) {
    const audit = new BrokerStdinAudit();
    const broker = new PassThrough();
    broker.resume();
    audit.observe(broker);
    assert.equal(audit.failureCategory, null);
    if (category === 'duplicate_stream') audit.observe(broker);
    else if (category === 'end_payload_empty') broker.end('');
    else if (category === 'end_payload_nonempty') broker.end(mutation());
    else if (category === 'unsupported_chunk') broker.write('unsupported synthetic input');
    else if (category === 'frame_decode') broker.write(Buffer.alloc(4));
    else if (category === 'frame_shape') broker.write(encodeBrokerFrame({ unexpected: true }));
    else {
      broker.write(mutation().subarray(0, 8));
      if (category === 'truncated_frame') broker.emit('close');
    }
    assert.equal(audit.failureCategory, category);
    assert.throws(() => audit.snapshot(), { message: 'ui_proof_wire_audit_incomplete' });
    broker.destroy();
  }
});

test('more than 64 sequential completed brokers preserve aggregate counts without exhausting active slots', async () => {
  const audit = new BrokerStdinAudit();
  for (let index = 1; index <= 96; index++) await completedBroker(audit, readRequest(index));
  assert.equal(audit.failureCategory, null);
  assert.equal(audit.snapshot().status, 96);
  assert.equal(audit.snapshot().settings_apply, 0);
});

test('the limit remains 64 concurrently active broker streams', () => {
  const audit = new BrokerStdinAudit();
  const brokers = Array.from({ length: 65 }, () => {
    const broker = new PassThrough();
    broker.resume();
    return broker;
  });
  try {
    for (const broker of brokers.slice(0, 64)) audit.observe(broker);
    assert.equal(audit.failureCategory, null);
    assert.equal(audit.snapshot().status, 0);
    audit.observe(brokers[64]);
    assert.equal(audit.failureCategory, 'stream_limit');
    assert.throws(() => audit.snapshot(), { message: 'ui_proof_wire_audit_incomplete' });
  } finally { brokers.forEach(broker => broker.destroy()); }
});

test('partial close remains a sticky refusal after decoder retirement', async () => {
  const audit = new BrokerStdinAudit();
  const broken = new PassThrough();
  broken.resume();
  audit.observe(broken);
  broken.write(mutation().subarray(0, 8));
  assert.equal(audit.failureCategory, 'partial_snapshot');
  const closed = once(broken, 'close');
  broken.destroy();
  await closed;
  assert.equal(audit.failureCategory, 'truncated_frame');
  await completedBroker(audit, readRequest(1));
  assert.equal(audit.failureCategory, 'truncated_frame');
  assert.throws(() => audit.snapshot(), { message: 'ui_proof_wire_audit_incomplete' });
});

test('mutation replay is still caught across retired decoders, with the same or a new request UUID', async () => {
  for (const replayId of [requestId, '00000000-0000-0000-0000-000000000004']) {
    const audit = new BrokerStdinAudit();
    const before = audit.snapshot();
    await completedBroker(audit, mutation());
    for (let index = 1; index <= 72; index++) await completedBroker(audit, readRequest(index));
    assert.doesNotThrow(() => audit.requireSingleMutation('settings_apply', requestId, before));
    await completedBroker(audit, mutation(replayId));
    assert.equal(audit.snapshot().settings_apply, 2);
    assert.throws(() => audit.requireSingleMutation('settings_apply', requestId, before),
      { message: 'ui_proof_mutation_dispatch_not_once' });
  }
});

test('duplicate finish and close events cannot free another active broker or corrupt its counts', async () => {
  const audit = new BrokerStdinAudit();
  const retired = await completedBroker(audit, readRequest(1));
  const live = Array.from({ length: 64 }, () => new PassThrough());
  const extra = new PassThrough();
  try {
    for (const [index, broker] of live.entries()) {
      broker.resume();
      audit.observe(broker);
      broker.write(readRequest(index + 2));
    }
    assert.equal(audit.snapshot().status, 65);
    retired.emit('finish');
    retired.emit('close');
    retired.emit('close');
    assert.equal(audit.snapshot().status, 65);
    audit.observe(extra);
    assert.equal(audit.failureCategory, 'stream_limit');
  } finally {
    live.forEach(broker => broker.destroy());
    extra.destroy();
  }
});

test('observing the same retired stream is still a duplicate refusal', async () => {
  const audit = new BrokerStdinAudit();
  const retired = await completedBroker(audit);
  audit.observe(retired);
  assert.equal(audit.failureCategory, 'duplicate_stream');
  assert.throws(() => audit.snapshot(), { message: 'ui_proof_wire_audit_incomplete' });
});

test('a retired stream cannot resume unobserved writes', async () => {
  const audit = new BrokerStdinAudit();
  const retired = await completedBroker(audit);
  retired.on('error', () => {});
  await new Promise<void>(resolve => {
    assert.equal(retired.write(mutation(), error => {
      assert.ok(error);
      resolve();
    }), false);
  });
  assert.equal(audit.failureCategory, 'frame_shape');
  assert.throws(() => audit.snapshot(), { message: 'ui_proof_wire_audit_incomplete' });
});

test('aggregate request-ID bound survives retirement instead of resetting per broker', async () => {
  const audit = new BrokerStdinAudit();
  for (let batch = 0; batch < 16; batch++) {
    const broker = new PassThrough();
    broker.resume();
    audit.observe(broker);
    for (let index = 1; index <= 64; index++) broker.write(readRequest(batch * 64 + index));
    const finished = once(broker, 'finish');
    broker.end();
    await finished;
  }
  assert.equal(audit.snapshot().status, 1024);
  await completedBroker(audit, readRequest(1025));
  assert.equal(audit.failureCategory, 'request_limit');
  assert.throws(() => audit.snapshot(), { message: 'ui_proof_wire_audit_incomplete' });
});

test('observation forwards original this, bytes, callback identity and backpressure unchanged', async () => {
  const audit = new BrokerStdinAudit();
  const bytes = mutation();
  let release!: () => void;
  const forwarded: { receiver?: Writable; arguments?: unknown[]; chunk?: unknown } = {};
  const broker = new Writable({ highWaterMark: 1, write(chunk, _encoding, callback) {
    assert.equal(this, broker);
    forwarded.chunk = chunk;
    release = callback;
  } });
  const original = broker.write;
  broker.write = function(this: Writable, ...arguments_: unknown[]) {
    forwarded.receiver = this;
    forwarded.arguments = arguments_;
    return Reflect.apply(original, this, arguments_);
  } as typeof broker.write;
  audit.observe(broker);
  let callback!: (error?: Error | null) => void;
  const acknowledged = new Promise<void>(resolve => {
    callback = error => { assert.ifError(error); resolve(); };
  });
  assert.equal(broker.write(bytes, callback), false);
  assert.equal(forwarded.receiver, broker);
  assert.equal(forwarded.arguments?.[0], bytes);
  assert.equal(forwarded.arguments?.[1], callback);
  assert.equal(forwarded.chunk, bytes);
  assert.equal(audit.snapshot().settings_apply, 1);
  release();
  await acknowledged;
  const finished = once(broker, 'finish');
  broker.end();
  await finished;
  assert.equal(audit.failureCategory, null);
});
