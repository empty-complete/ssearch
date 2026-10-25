import { test } from 'node:test';
import assert from 'node:assert/strict';
import { BrokerFrameDecoder, encodeBrokerFrame, MAX_RESPONSE_FRAME } from '../src/brokerFrames.js';
import { spawn } from 'node:child_process';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { ServiceBridgeClient } from '../src/serviceClient.js';

function wire(json: string): Buffer {
  const bytes = Buffer.from(json, 'utf8');
  const header = Buffer.alloc(4);
  header.writeUInt32BE(bytes.length);
  return Buffer.concat([header, bytes]);
}

test('private broker decoder preserves fragmented UTF-8 frames and multiple replies', () => {
  const decoder = new BrokerFrameDecoder();
  const first = wire('{"schema_version":1,"value":"чертёж 🪐"}');
  assert.deepEqual(decoder.push(first.subarray(0, 2)), []);
  assert.equal(decoder.partial, true);
  assert.deepEqual(decoder.push(first.subarray(2, 6)), []);
  assert.deepEqual(decoder.push(Buffer.concat([first.subarray(6), wire('{"ok":true}')])) , [
    { schema_version: 1, value: 'чертёж 🪐' }, { ok: true },
  ]);
  assert.equal(decoder.partial, false);
  decoder.end();
});

test('broker framing refuses oversized, empty, invalid UTF-8/JSON and truncated frames', () => {
  for (const length of [0, MAX_RESPONSE_FRAME + 1]) {
    const header = Buffer.alloc(4);
    header.writeUInt32BE(length);
    assert.throws(() => new BrokerFrameDecoder().push(header));
  }
  assert.throws(() => new BrokerFrameDecoder().push(wire('{broken')));
  assert.throws(() => new BrokerFrameDecoder().push(Buffer.from([0, 0, 0, 1, 0xff])));
  for (const prefix of [Buffer.from([0]), wire('{"ok":true}').subarray(0, 7)]) {
    const decoder = new BrokerFrameDecoder();
    decoder.push(prefix);
    assert.throws(() => decoder.end());
  }
});

test('requests use exact big-endian byte lengths and keep the 64 KiB plus overhead bound', () => {
  const encoded = encodeBrokerFrame({ value: 'чертёж' });
  assert.deepEqual(encoded, wire('{"value":"чертёж"}'));
  assert.throws(() => encodeBrokerFrame({ value: 'x'.repeat(65_536 + 4096) }));
  const response = encodeBrokerFrame({ value: 'x'.repeat(100_000) }, MAX_RESPONSE_FRAME);
  assert.equal(response.readUInt32BE(0), response.length - 4);
  assert.deepEqual(new BrokerFrameDecoder().push(response), [{ value: 'x'.repeat(100_000) }]);
});

test('correlated transport failures retain uncertainty without retrying a mutation or changing P2 codes', async () => {
  for (const code of ['service_unavailable', 'request_timeout', 'service_identity_mismatch',
    'authentication_required', 'access_denied', 'revision_conflict', 'invalid_request']) {
    const profile = { profile: __dirname, key: 'b'.repeat(64), serviceVersion: '0.2.0',
      pythonBase: process.execPath, pythonVenv: process.execPath };
    const client = new ServiceBridgeClient({ spawnBroker: () => spawn(process.execPath,
      [join(__dirname, 'fixtures/correlated-error-broker.cjs'), profile.profile, profile.key, code],
      { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] }) });
    try {
      await client.connect(profile);
      const reply = await client.request('settings_apply', { candidate: {}, expected_revision: 1 },
        randomUUID(), new AbortController().signal);
      assert.equal(reply.error?.code, code);
      assert.equal(reply.error?.details.transport, 'fixture');
      assert.equal(reply.error?.details.outcome,
        ['access_denied', 'revision_conflict', 'invalid_request'].includes(code) ? undefined : 'unknown');
      if (client.authority) {
        const count = await client.request('status', {}, randomUUID(), new AbortController().signal);
        assert.deepEqual(count.data, { calls: 2 });
      }
    } finally { await client.close(); }
  }
});
