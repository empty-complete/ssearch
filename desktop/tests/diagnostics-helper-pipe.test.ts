import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { constants } from 'node:fs';
import { encodeBrokerFrame } from '../src/brokerFrames.js';
import { HELPER_PROTOCOL_FD, helperDescriptorIsPipe, writeHelperPipe, writeNativeHelperPipe, type HelperPipeIO, type HelperPipeClock } from '../src/diagnosticsHelperPipe.js';
import { helperEnvironment, helperFailureDetails, receiveHelper, type HelperReplyChild } from '../src/diagnosticsHelpers.js';
import { frozenServiceEnvironment } from '../src/packagedRuntime.js';

const context = { source_commit: 'a'.repeat(40), role: 'second-instance' as const,
  session_id: '00000000-0000-0000-0000-000000000001', challenge: '00000000-0000-0000-0000-000000000002' };
const frame = encodeBrokerFrame({ schema_version: 1, session_id: context.session_id, challenge: context.challenge,
  role: context.role, helper_pid: 1234, kind: 'second_instance', owner_starts: 0, broker_starts: 0, profile_invocations: 0 });

test('Windows native pipe mode remains a pipe when Node Stats.isFIFO always returns false', () => {
  // Pinned libuv Windows fstat uses CRT _S_IFIFO (0x1000). Node intentionally
  // hides isFIFO on Windows and may omit fs.constants.S_IFIFO entirely.
  for (const mode of [0x1000, 0x1000 | 0o600]) {
    assert.ok(helperDescriptorIsPipe({ mode, isFIFO: () => false }));
  }
});

test('native pipe classification refuses files, devices, socket, absent and invalid mode without Stats fallback', () => {
  for (const mode of [constants.S_IFREG, constants.S_IFDIR, constants.S_IFCHR, 0xc000,
    0, undefined, null, NaN, Infinity, -1, 0x1000 + 0.5, 2 ** 32 + 0x1000, '4096', 0x1000 | constants.S_IFREG]) {
    assert.ok(!helperDescriptorIsPipe({ mode, isFIFO: () => true }));
  }
  assert.ok(!helperDescriptorIsPipe({ isFIFO: () => true }));
});

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

test('explicit cancellation and callback errors fail once without resuming late partial IO', async () => {
  let late: (error: Error | null, written: number) => void = () => {};
  let writes = 0;
  let completions = 0;
  let failed = false;
  const cancel = writeHelperPipe(frame, error => { completions++; failed = !!error; }, {
    inspect: () => ({ isFIFO: () => true }),
    write(_fd, _bytes, _offset, _length, callback) { writes++; late = callback; },
  });
  cancel();
  cancel();
  late(null, 3);
  await Promise.resolve();
  assert.ok(failed && completions === 1 && writes === 1);
  const error = await new Promise<Error | null | undefined>(resolve => writeHelperPipe(frame, resolve, {
    inspect: () => ({ isFIFO: () => true }),
    write(_fd, _bytes, _offset, _length, callback) { callback(new Error('PRIVATE_CALLBACK_SENTINEL'), 0); },
  }));
  assert.ok(error?.message === 'ui_proof_helper_write_failed');
  assert.ok(!JSON.stringify(error).includes('PRIVATE_CALLBACK_SENTINEL'));
});

test('default native writer refuses Node test process before accessing any descriptor', async () => {
  const error = await new Promise<Error | null | undefined>(resolve => writeNativeHelperPipe(frame, resolve));
  assert.ok(error?.message === 'ui_proof_helper_write_failed');
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
  for (const keys of [['NODE_CHANNEL_FD', 'NODE_CHANNEL_SERIALIZATION_MODE', 'NODE_UNIQUE_ID'],
    ['node_channel_fd', 'Node_Channel_Serialization_Mode', 'NoDe_UnIqUe_Id']]) {
    const previous = keys.map(key => process.env[key]);
    try {
      keys.forEach(key => { process.env[key] = '99'; });
      const helper = helperEnvironment();
      const service = frozenServiceEnvironment();
      assert.ok(Object.keys(helper).every(key => !keys.some(reserved => reserved.toUpperCase() === key.toUpperCase())));
      for (const key of Object.keys(process.env).filter(key => keys.some(reserved => reserved.toUpperCase() === key.toUpperCase()))) {
        assert.ok(service[key] === '99');
        assert.ok(process.env[key] === '99');
      }
    } finally {
      keys.forEach((key, index) => { if (previous[index] === undefined) delete process.env[key]; else process.env[key] = previous[index]; });
    }
  }
});
