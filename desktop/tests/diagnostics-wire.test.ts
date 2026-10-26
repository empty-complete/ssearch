import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
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
