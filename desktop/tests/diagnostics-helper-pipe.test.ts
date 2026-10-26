import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { encodeBrokerFrame } from '../src/brokerFrames.js';
import { HELPER_PROTOCOL_FD, writeHelperPipe, type HelperPipeIO, type HelperPipeClock } from '../src/diagnosticsHelperPipe.js';
import { helperEnvironment, helperFailureDetails, receiveHelper, type HelperReplyChild } from '../src/diagnosticsHelpers.js';
import { frozenServiceEnvironment } from '../src/packagedRuntime.js';

const context = { source_commit: 'a'.repeat(40), role: 'second-instance' as const,
  session_id: '00000000-0000-0000-0000-000000000001', challenge: '00000000-0000-0000-0000-000000000002' };
const frame = encodeBrokerFrame({ schema_version: 1, session_id: context.session_id, challenge: context.challenge,
  role: context.role, helper_pid: 1234, kind: 'second_instance', owner_starts: 0, broker_starts: 0, profile_invocations: 0 });

// All descriptors below are synthetic adapters. Never inspect/write this test runner's fd3.
function pipeFixture(kind: 'fifo' | 'file' | 'absent' = 'fifo') {
  const writes: Array<{ fd: number; bytes: Buffer; offset: number; length: number }> = [];
  const inspected: number[] = [];
  const received: Buffer[] = [];
  const io: HelperPipeIO = {
    inspect(fd) { inspected.push(fd); if (kind === 'absent') throw new Error('PRIVATE_INSPECT_SENTINEL'); return { isFIFO: () => kind === 'fifo' }; },
    write(fd, bytes, offset, length, callback) {
      writes.push({ fd, bytes, offset, length });
      const size = Math.min(length, 3);
      received.push(bytes.subarray(offset, offset + size));
      queueMicrotask(() => callback(null, size));
    },
  };
  return { io, writes, inspected, received };
}

test('fixed private FIFO writes every byte once across partial callbacks, exclusively on fd3', async () => {
  const f = pipeFixture();
  assert.equal(HELPER_PROTOCOL_FD, 3);
  const error = await new Promise<Error | null | undefined>(resolve => writeHelperPipe(frame, resolve, f.io));
  assert.ok(error == null);
  assert.ok(Buffer.concat(f.received).equals(frame));
  assert.ok(f.writes.length > 1);
  assert.ok(f.writes.every(write => write.fd === 3 && write.bytes === frame));
  assert.deepEqual(f.inspected, [3]);
  assert.equal(f.writes[0].offset, 0);
  for (let index = 1; index < f.writes.length; index++) assert.equal(f.writes[index].offset, f.writes[index - 1].offset + 3);
});

test('absent or regular-file fd3 refuses before any write and never falls back to stdout', async () => {
  for (const kind of ['file', 'absent'] as const) {
    const f = pipeFixture(kind);
    const error = await new Promise<Error | null | undefined>(resolve => writeHelperPipe(frame, resolve, f.io));
    assert.ok(error?.message === 'ui_proof_helper_write_failed');
    assert.equal(f.writes.length, 0);
    assert.equal(JSON.stringify(error).includes('PRIVATE_INSPECT_SENTINEL'), false);
  }
});

test('zero, invalid partial counts and write errors fail once without retrying a frame', async () => {
  for (const count of [0, -1, NaN, frame.length + 1, 1.5]) {
    let writes = 0;
    let completions = 0;
    const io: HelperPipeIO = { inspect: () => ({ isFIFO: () => true }), write(_fd, _bytes, _offset, _length, callback) {
      writes++; callback(null, count);
    } };
    const error = await new Promise<Error | null | undefined>(resolve => writeHelperPipe(frame,
      error => { completions++; resolve(error); }, io));
    assert.ok(error?.message === 'ui_proof_helper_write_failed');
    assert.equal(writes, 1);
    assert.equal(completions, 1);
  }
  let writes = 0;
  const error = await new Promise<Error | null | undefined>(resolve => writeHelperPipe(frame, resolve, {
    inspect: () => ({ isFIFO: () => true }), write() { writes++; throw new Error('PRIVATE_WRITE_SENTINEL'); },
  }));
  assert.ok(error?.message === 'ui_proof_helper_write_failed');
  assert.equal(writes, 1);
  assert.equal(JSON.stringify(error).includes('PRIVATE_WRITE_SENTINEL'), false);
});

test('two-second deadline cancels pending partial IO so late callbacks cannot start another write', async () => {
  let expire = () => {};
  let late: (error: Error | null, written: number) => void = () => {};
  let writes = 0;
  const clock: HelperPipeClock = (callback, milliseconds) => { assert.equal(milliseconds, 2_000); expire = callback; return () => {}; };
  const outcome = new Promise<Error | null | undefined>(resolve => writeHelperPipe(frame, resolve, {
    inspect: () => ({ isFIFO: () => true }), write(_fd, _bytes, _offset, _length, callback) { writes++; late = callback; },
  }, clock));
  assert.equal(writes, 1);
  expire();
  const error = await outcome;
  assert.ok(error?.message === 'ui_proof_helper_write_failed');
  late(null, 3);
  assert.equal(writes, 1);
});

function receiver(privatePipe: boolean) {
  const events = new EventEmitter();
  const stdout = new PassThrough();
  stdout.resume();
  const protocol = new PassThrough();
  const child: HelperReplyChild = { pid: 1234, stdout, stdio: [null, stdout, new PassThrough(), privatePipe ? protocol : null],
    once: events.once.bind(events), kill: () => true };
  const outcome = receiveHelper(child, context, () => () => {}).then(value => ({ value, error: null }),
    error => ({ value: null, error: error as unknown }));
  return { stdout, protocol, events, outcome };
}

test('valid private protocol is accepted while unrelated stdout bytes remain outside the decoder', async () => {
  const f = receiver(true);
  f.stdout.write('SYNTHETIC_UNRELATED_STDOUT');
  f.protocol.write(frame);
  f.events.emit('close', 0, null);
  const result = await f.outcome;
  assert.ok(result.error === null);
  assert.ok(result.value !== null);
});

test('no private pipe never falls back to a valid stdout reply', async () => {
  const f = receiver(false);
  f.stdout.write(frame);
  f.events.emit('close', 0, null);
  assert.ok((await f.outcome).error !== null);
});

test('private junk, partial EOF and missing reply remain fail-closed even with valid stdout', async () => {
  for (const category of ['length_refused', 'truncated', 'missing'] as const) {
    const f = receiver(true);
    f.stdout.write(frame);
    if (category === 'length_refused') f.protocol.write(Buffer.alloc(4));
    if (category === 'truncated') f.protocol.write(frame.subarray(0, 8));
    f.events.emit('close', 0, null);
    const result = await f.outcome;
    assert.ok(result.value === null);
    assert.deepEqual(helperFailureDetails(result.error), { stage: 'reply', category });
  }
});

test('only helper environment reserves fd3 from Node IPC/cluster bootstrap claims', () => {
  const keys = ['NODE_CHANNEL_FD', 'NODE_CHANNEL_SERIALIZATION_MODE', 'NODE_UNIQUE_ID'];
  const previous = keys.map(key => process.env[key]);
  try {
    keys.forEach(key => { process.env[key] = '99'; });
    const helper = helperEnvironment();
    const service = frozenServiceEnvironment();
    for (const key of keys) {
      assert.equal(helper[key], undefined);
      assert.equal(service[key], '99');
      assert.equal(process.env[key], '99');
    }
  } finally {
    keys.forEach((key, index) => { if (previous[index] === undefined) delete process.env[key]; else process.env[key] = previous[index]; });
  }
});
