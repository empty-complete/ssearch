import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { mkdtempSync, mkdirSync, writeFileSync, realpathSync, existsSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { encodeBrokerFrame } from '../src/brokerFrames.js';
import { authenticateUiHelper, receiveHelper, helperFailureDetails, type HelperReplyChild,
  type HelperReplyContext, type HelperClock, type HelperFailureDetails } from '../src/diagnosticsHelpers.js';
import { recordProofFailure, type ProofOutcome } from '../src/diagnosticsOutcome.js';

const marker: HelperReplyContext = { source_commit: 'a'.repeat(40), role: 'second-instance',
  session_id: '00000000-0000-0000-0000-000000000001', challenge: '00000000-0000-0000-0000-000000000002' };
const sentinel = 'PRIVATE_HELPER_SENTINEL_C:\\private-profile_credentials';

// Stream/clock policy fixture only. No executable, singleton, native identity or owner is run.
function fixture() {
  const events = new EventEmitter();
  const stdout = new PassThrough();
  const timing = { fire: () => {}, milliseconds: 0, cancelled: false, kills: 0 };
  const clock: HelperClock = (callback, milliseconds) => {
    timing.fire = callback;
    timing.milliseconds = milliseconds;
    return () => { timing.cancelled = true; };
  };
  const child: HelperReplyChild = { pid: 1234, stdout, once: events.once.bind(events),
    kill() { timing.kills++; return true; } };
  const reply = receiveHelper(child, marker, clock);
  // Handle rejection immediately; test actions may synchronously emit a refusal.
  const outcome = reply.then(value => ({ value, error: null }), error => ({ value: null, error: error as unknown }));
  return { events, stdout, timing, outcome };
}

function positive() {
  return { schema_version: 1, kind: 'second_instance', session_id: marker.session_id, challenge: marker.challenge,
    role: marker.role, helper_pid: 1234, owner_starts: 0, broker_starts: 0, profile_invocations: 2 };
}
function negative() {
  return { schema_version: 1, kind: 'helper_failure', session_id: marker.session_id, challenge: marker.challenge,
    role: marker.role, source_commit: marker.source_commit, helper_pid: 1234,
    stage: 'native_profile', category: 'identity_refused' };
}
async function refused(f: ReturnType<typeof fixture>, expected: HelperFailureDetails) {
  const outcome = await f.outcome;
  assert.equal(outcome.value, null);
  assert.ok(outcome.error);
  const details = helperFailureDetails(outcome.error);
  assert.deepEqual(details, expected);
  assert.equal(JSON.stringify(details).includes(sentinel), false);
  return details;
}

test('positive correlated helper reply still requires its retained child to exit zero', async () => {
  const f = fixture();
  f.stdout.write(encodeBrokerFrame(positive()));
  f.events.emit('close', 0, null);
  const outcome = await f.outcome;
  assert.equal(outcome.error, null);
  assert.deepEqual(outcome.value, positive());
  assert.equal(f.timing.milliseconds, 180_000);
  assert.equal(f.timing.cancelled, true);
});

test('admission refusal creates no helper QA profile/report and projects no private marker content', () => {
  const root = realpathSync(mkdtempSync(join(tmpdir(), 'ssearch helper policy ')));
  try {
    mkdirSync(join(root, 'helpers'));
    const id = '00000000-0000-0000-0000-000000000003';
    const profilePath = join(root, 'helpers', id);
    writeFileSync(join(root, 'helpers', `${id}.context.json`), JSON.stringify({ private: sentinel }));
    let error: unknown;
    try { authenticateUiHelper({ scenario: 'lifecycle', helper: true, helperRole: 'second-instance',
      profilePath, reportPath: join(profilePath, 'helper.json') }, root, marker.source_commit, '0.2.0'); }
    catch (failure) { error = failure; }
    assert.ok(error);
    assert.deepEqual(helperFailureDetails(error), { stage: 'admission', category: 'refused' });
    assert.equal(existsSync(profilePath), false);
    assert.equal(JSON.stringify(helperFailureDetails(error)).includes(sentinel), false);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('spawn, child nonzero/signal, admission exit and unchanged deadline have distinct closed outcomes', async () => {
  for (const category of ['spawn_error', 'nonzero', 'signal', 'refused', 'timeout'] as const) {
    const f = fixture();
    if (category === 'spawn_error') f.events.emit('error', new Error(sentinel));
    else if (category === 'nonzero') f.events.emit('close', 1, null);
    else if (category === 'signal') f.events.emit('close', null, 'SIGTERM');
    else if (category === 'refused') f.events.emit('close', 2, null);
    else { assert.equal(f.timing.milliseconds, 180_000); f.timing.fire(); }
    const stage = category === 'spawn_error' ? 'spawn' : category === 'timeout' ? 'deadline'
      : category === 'refused' ? 'admission' : 'child_exit';
    await refused(f, { stage, category });
    if (category === 'timeout') assert.equal(f.timing.kills, 1);
  }
});

test('missing, malformed and truncated replies remain failures with safe distinct outcomes', async () => {
  for (const category of ['missing', 'decode_refused', 'truncated'] as const) {
    const f = fixture();
    if (category === 'decode_refused') f.stdout.write(Buffer.alloc(4));
    if (category === 'truncated') f.stdout.write(encodeBrokerFrame(positive()).subarray(0, 8));
    f.events.emit('close', category === 'truncated' ? 1 : 0, null);
    await refused(f, { stage: 'reply', category });
  }
});

test('native negative handoff remains failure even with zero exit and validates every correlation binding', async () => {
  const acceptedNegative = fixture();
  acceptedNegative.stdout.write(encodeBrokerFrame(negative()));
  acceptedNegative.events.emit('close', 0, null);
  await refused(acceptedNegative, { stage: 'native_profile', category: 'identity_refused' });
  for (const field of ['source_commit', 'session_id', 'challenge', 'role', 'helper_pid'] as const) {
    const f = fixture();
    f.stdout.write(encodeBrokerFrame({ ...negative(), [field]: sentinel }));
    f.events.emit('close', 1, null);
    await refused(f, { stage: 'reply', category: 'binding_refused' });
  }
  for (const altered of [{ ...negative(), stage: 'reply' }, { ...negative(), category: 'write_error' },
    { ...negative(), arbitrary: sentinel }]) {
    const f = fixture();
    f.stdout.write(encodeBrokerFrame(altered));
    f.events.emit('close', 0, null);
    await refused(f, { stage: 'reply', category: 'binding_refused' });
  }
});

test('duplicate positive/negative frames in any order and trailing partial data cannot establish success', async () => {
  for (const frames of [[positive(), negative()], [negative(), positive()], [positive(), positive()]]) {
    const f = fixture();
    f.stdout.write(Buffer.concat(frames.map(frame => encodeBrokerFrame(frame))));
    f.events.emit('close', 0, null);
    const outcome = await f.outcome;
    assert.equal(outcome.value, null);
    assert.ok(outcome.error); // Even a prior negative frame must never become successful completion.
    const details = helperFailureDetails(outcome.error);
    assert.ok(details);
    assert.ok(details.category === 'duplicate' || (details.stage === 'native_profile' && details.category === 'identity_refused'));
  }
  const f = fixture();
  f.stdout.write(Buffer.concat([encodeBrokerFrame(positive()), encodeBrokerFrame(negative()).subarray(0, 8)]));
  f.events.emit('close', 0, null);
  await refused(f, { stage: 'reply', category: 'truncated' });
});

test('first reply binding refusal survives later child error, nonzero close and deadline', async () => {
  const f = fixture();
  f.stdout.write(encodeBrokerFrame({ ...positive(), challenge: sentinel }));
  f.events.emit('error', new Error(sentinel));
  f.events.emit('close', 1, null);
  f.timing.fire();
  await refused(f, { stage: 'reply', category: 'binding_refused' });
});

test('helper failure projection augments an existing first failure without replacing it', () => {
  const report: ProofOutcome = { ok: false, reason_code: 'pending' };
  recordProofFailure(report, 'installed_renderer_loaded', { phase: 'action', category: 'renderer_load' });
  recordProofFailure(report, 'second_instance_without_service_start', { phase: 'action', category: 'runtime_failure',
    helperStage: 'native_profile', helperCategory: 'identity_refused' });
  assert.equal(report.reason_code, 'installed_renderer_loaded');
  assert.equal(report.failure_diagnostics?.primary_category, 'renderer_load');
  assert.equal(report.failure_diagnostics?.helper_stage, 'native_profile');
  assert.equal(report.failure_diagnostics?.helper_category, 'identity_refused');
  assert.equal(report.ok, false);
});
