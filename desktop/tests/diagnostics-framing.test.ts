import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { BrokerFrameDecoder, encodeBrokerFrame } from '../src/brokerFrames.js';
import { helperFailureDetails, receiveHelper, type HelperReplyChild, type HelperReplyContext } from '../src/diagnosticsHelpers.js';

function body(bytes: Buffer): Buffer {
  const header = Buffer.alloc(4);
  header.writeUInt32BE(bytes.length);
  return Buffer.concat([header, bytes]);
}

test('strict decoder distinguishes invalid length, UTF-8, JSON and truncation without exposing input', () => {
  for (const category of ['length', 'utf8', 'json', 'truncated'] as const) {
    const decoder = new BrokerFrameDecoder(64);
    assert.equal(decoder.failureCategory, null);
    if (category === 'length') assert.throws(() => decoder.push(Buffer.alloc(4)), { message: 'invalid_broker_frame' });
    else if (category === 'utf8') assert.throws(() => decoder.push(body(Buffer.from([0xff]))), TypeError);
    else if (category === 'json') assert.throws(() => decoder.push(body(Buffer.from('{private-fixture'))), SyntaxError);
    else {
      decoder.push(encodeBrokerFrame({ ok: true }).subarray(0, 7));
      assert.throws(() => decoder.end(), { message: 'truncated_broker_frame' });
    }
    assert.equal(decoder.failureCategory, category);
    assert.throws(() => decoder.end());
    assert.equal(decoder.failureCategory, category);
  }
  const oversized = Buffer.alloc(4);
  oversized.writeUInt32BE(65);
  const decoder = new BrokerFrameDecoder(64);
  assert.throws(() => decoder.push(oversized), { message: 'invalid_broker_frame' });
  assert.equal(decoder.failureCategory, 'length');
});

test('closed tagging rethrows the exact original JSON and UTF-8 errors', () => {
  const originalParse = JSON.parse;
  const originalDecode = TextDecoder.prototype.decode;
  for (const category of ['json', 'utf8'] as const) {
    const sentinel = category === 'json' ? new SyntaxError('synthetic JSON refusal') : new TypeError('synthetic UTF-8 refusal');
    const decoder = new BrokerFrameDecoder();
    try {
      if (category === 'json') JSON.parse = () => { throw sentinel; };
      else TextDecoder.prototype.decode = () => { throw sentinel; };
      assert.throws(() => decoder.push(body(Buffer.from('{}'))), error => error === sentinel);
      assert.equal(decoder.failureCategory, category);
    } finally {
      JSON.parse = originalParse;
      TextDecoder.prototype.decode = originalDecode;
    }
  }
});

test('helper receiver projects only closed decode outcomes and preserves the first refusal', async () => {
  const context: HelperReplyContext = { source_commit: 'a'.repeat(40), role: 'second-instance',
    session_id: '00000000-0000-0000-0000-000000000001', challenge: '00000000-0000-0000-0000-000000000002' };
  for (const [bytes, category] of [[Buffer.alloc(4), 'length_refused'], [body(Buffer.from([0xff])), 'utf8_refused'],
    [body(Buffer.from('{PRIVATE_SYNTHETIC_INPUT')), 'json_refused']] as const) {
    const events = new EventEmitter();
    const stdout = new PassThrough();
    const child: HelperReplyChild = { pid: 1234, stdout, stdio: [null, null, null, stdout],
      once: events.once.bind(events), kill: () => true };
    const outcome = receiveHelper(child, context, () => () => {}).then(() => null, error => error as unknown);
    stdout.write(bytes);
    events.emit('close', 1, null);
    const error = await outcome;
    assert.ok(error);
    const details = helperFailureDetails(error);
    assert.deepEqual(details, { stage: 'reply', category });
    assert.equal(JSON.stringify(details).includes('PRIVATE_SYNTHETIC_INPUT'), false);
    stdout.destroy();
  }
});
