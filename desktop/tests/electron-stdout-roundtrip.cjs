// GitHub-only private helper pipe proof. No service, SDK, singleton, windows,
// frozen package or native acceptance. Raw Electron streams never reach job logs.
const childMode = !!process.versions.electron && process.argv.includes('--stdout-child');
let app;
let finished = false;
let watchdog;
let helpers;
const owned = new Set();
// Only the Node outer driver prints. A verified parent success has a dedicated
// nonzero code, so premature/default Electron exit 0 can never be mistaken for PASS.
const EXIT_PASS = 90;
const EXIT_RUNTIME = 91;
const EXIT_DEADLINE = 92;
const EXIT_HELPER = 100;
const EXIT_NO_WRITE = 89;
const EXIT_NATIVE_FAILURE = 200;
// Windows-only finite domain: outcome index plus nine closed predicate bits.
const EXIT_OBSERVATION = 1000;
const OBSERVATION_WIDTH = 512;
const replyFailures = [
  ['spawn', 'spawn_error'], ['admission', 'refused'], ['child_exit', 'nonzero'], ['child_exit', 'signal'],
  ['deadline', 'timeout'], ['reply', 'missing'], ['reply', 'truncated'], ['reply', 'duplicate'],
  ['reply', 'binding_refused'], ['reply', 'decode_refused'], ['reply', 'length_refused'],
  ['reply', 'utf8_refused'], ['reply', 'json_refused'], ['reply', 'write_error'],
];
const OUTCOME_WIDTH = (replyFailures.length + 2) * OBSERVATION_WIDTH;
const descriptorTypes = ['unavailable', 'fifo', 'regular_file', 'character', 'socket', 'other'];
const NATIVE_FAILURE_WIDTH = 16 * descriptorTypes.length ** 2;
// Harness-only refusal domain: guard/inspect/FIFO/writer-called bits plus a
// closed virtual/physical descriptor-type pair. No native Stats are exported.
const NATIVE_COMPLETED = NATIVE_FAILURE_WIDTH + 1;
function descriptorType(stat) {
  return stat.isFIFO() ? 1 : stat.isFile() ? 2 : stat.isCharacterDevice() ? 3 : stat.isSocket() ? 4 : 5;
}
function summary(ok, status, details = null, pinned = false, memory = null) {
  return { scope: 'source_private_helper_roundtrip', ok, status, electron_pinned: pinned,
    helper_stage: details?.stage ?? null, helper_category: details?.category ?? null,
    memory_predicates: memory };
}
function finish(result, code = result.ok ? 0 : 1) {
  if (finished) return;
  finished = true;
  clearTimeout(watchdog);
  if (childMode) { app ? app.exit(code) : process.exit(code); return; }
  if (process.versions.electron) {
    const index = replyFailures.findIndex(([stage, category]) => stage === result.helper_stage && category === result.helper_category);
    code = result.memory_predicates
      ? EXIT_OBSERVATION + result.memory_predicates.native * OUTCOME_WIDTH
        + result.memory_predicates.outcome * OBSERVATION_WIDTH + result.memory_predicates.mask
      : result.ok ? EXIT_PASS : index >= 0 ? EXIT_HELPER + index : result.status === 'deadline' ? EXIT_DEADLINE : EXIT_RUNTIME;
  }
  const exit = () => app ? app.exit(code) : process.versions.electron ? process.exit(code) : (process.exitCode = code);
  const drained = [...owned].map(child => new Promise(resolve => {
    if (child.exitCode !== null || child.signalCode !== null) { resolve(true); return; }
    const timer = setTimeout(() => resolve(false), 3_000);
    child.once('close', () => { clearTimeout(timer); resolve(true); });
    try { child.kill(); } catch { clearTimeout(timer); resolve(false); }
  }));
  void Promise.all(drained).then(values => {
    if (values.some(value => !value)) { result = summary(false, 'runtime_failure', null, result.electron_pinned); code = 1; }
    if (process.versions.electron) { exit(); return; } // Ignore the suspect parent stdout entirely.
    const timer = setTimeout(() => app ? app.exit(1) : process.exit(1), 2_000);
    process.stdout.write(JSON.stringify(result) + '\n', error => { clearTimeout(timer); if (error) code = 1; exit(); });
  }).catch(() => app ? app.exit(1) : process.exit(1));
}
function failed() { finish(summary(false, 'runtime_failure')); }
process.on('uncaughtException', failed);
process.on('unhandledRejection', failed);

try {
  const { spawn } = require('node:child_process');
  const { randomUUID } = require('node:crypto');
  const { publicUuid } = require('../dist/validation.js');
  const { encodeBrokerFrame } = require('../dist/brokerFrames.js');
  const { writeNativeHelperPipe } = require('../dist/diagnosticsHelperPipe.js');
  const { frozenServiceEnvironment } = require('../dist/packagedRuntime.js');
  helpers = require('../dist/diagnosticsHelpers.js');
  if (!process.versions.electron) {
    // Fixed exit domain: stdout/stderr of the Electron parent are discarded.
    const child = spawn(require('electron'), [__filename, '--stdout-parent'], {
      windowsHide: true, env: frozenServiceEnvironment(), stdio: ['ignore', 'pipe', 'pipe'],
    });
    owned.add(child);
    child.stderr.resume();
    child.stdout.resume();
    child.once('error', failed);
    child.once('close', (code, signal) => {
      if (signal) { failed(); return; }
      const observation = code - EXIT_OBSERVATION;
      if (observation >= 0 && observation < (NATIVE_COMPLETED + 1) * OUTCOME_WIDTH) {
        const native = Math.floor(observation / OUTCOME_WIDTH);
        const outcome = Math.floor((observation % OUTCOME_WIDTH) / OBSERVATION_WIDTH);
        const mask = observation % OBSERVATION_WIDTH;
        const nativeMask = native === NATIVE_COMPLETED ? 15 : native - 1;
        const guard = native > 0 ? !!(nativeMask & 1) : null;
        const inspected = native > 0 ? !!(nativeMask & 2) : null;
        const fifo = native > 0 ? !!(nativeMask & 4) : null;
        const accepted = outcome === replyFailures.length;
        const memory = { receiver_accepted: accepted, child_exited_zero: !!(mask & 1),
          writer_callback_completed: accepted && (mask & 1) ? true : null, bytes_equal_expected: !!(mask & 2),
          stdout_noise_seen: !!(mask & 4), capture_complete: !!(mask & 8),
          no_write_completed: !!(mask & 16), no_write_private_empty: !!(mask & 32),
          private_junk_refused: !!(mask & 64), capture_overflow: !!(mask & 128), missing_private_pipe_refused: !!(mask & 256),
          native_guard_passed: guard, fd3_inspect_available: inspected, fd3_is_fifo: fifo,
          production_writer_called: native > 0 ? !!(nativeMask & 8) : null,
          fd3_type: native > 0 && native !== NATIVE_COMPLETED
            ? descriptorTypes[Math.floor(nativeMask / 16) % descriptorTypes.length] : null,
          fd3_physical_type: native > 0 && native !== NATIVE_COMPLETED
            ? descriptorTypes[Math.floor(nativeMask / (16 * descriptorTypes.length))] : null,
          writer_native_category: native === 0 ? 'unmeasured' : native === NATIVE_COMPLETED ? null
            : !guard ? 'guard_refused' : !inspected ? 'inspect_unavailable' : !fifo ? 'not_fifo' : 'write_refused' };
        const ok = accepted && memory.child_exited_zero && memory.bytes_equal_expected && memory.capture_complete
          && memory.stdout_noise_seen && memory.no_write_completed && memory.no_write_private_empty
          && memory.private_junk_refused && memory.missing_private_pipe_refused && !memory.capture_overflow;
        const pair = replyFailures[outcome];
        finish(summary(ok, ok ? 'passed' : 'roundtrip_failed', pair ? { stage: pair[0], category: pair[1] } : null, true, memory));
        return;
      }
      if (code === EXIT_PASS) { finish(summary(true, 'passed', null, true)); return; }
      const pair = replyFailures[code - EXIT_HELPER];
      if (pair) { finish(summary(false, 'roundtrip_failed', { stage: pair[0], category: pair[1] }, true)); return; }
      finish(summary(false, code === EXIT_DEADLINE ? 'deadline' : 'runtime_failure', null, code === EXIT_DEADLINE));
    });
    watchdog = setTimeout(() => finish(summary(false, 'deadline')), 40_000);
  } else {
    app = require('electron').app;
    app.enableSandbox();
    app.on('window-all-closed', () => {});
    if (process.platform !== 'win32' || process.versions.electron !== '45.0.0' || process.env.GITHUB_ACTIONS !== 'true') {
      failed();
    } else if (childMode) {
      // Synthetic fixture correlation is not a credential. Avoid introducing a
      // Node stdin decoder absent from the production helper's native SDK path.
      const args = process.argv.slice(process.argv.indexOf('--stdout-child') + 1);
      const mode = args[5];
      if (args.length !== 6 || args[0] !== '--stdout-session' || args[2] !== '--stdout-challenge'
        || args[4] !== '--stdout-write' || !['yes', 'no', 'junk'].includes(mode)
        || !publicUuid(args[1]) || !publicUuid(args[3])) throw new Error();
      const context = { session_id: args[1], challenge: args[3], role: 'second-instance' };
      watchdog = setTimeout(failed, 15_000);
      // Explicit unrelated output must never enter the private decoder. Unknown
      // native stdout remains drained too; no prefix inspection or recovery.
      const noise = new Promise((resolve, reject) => process.stdout.write('SYNTHETIC_UNRELATED_STDOUT\n',
        error => error ? reject(new Error()) : resolve()));
      void noise.then(async () => {
        if (mode === 'no') { finish(summary(true, 'passed', null, true), EXIT_NO_WRITE); return; }
        let nativeMask = process.platform === 'win32' && !!process.versions.electron && process.type === 'browser' ? 1 : 0;
        if (nativeMask & 1) {
          try {
            const stat = require('node:fs').fstatSync(3);
            nativeMask |= 2;
            if (stat.isFIFO()) nativeMask |= 4;
            nativeMask += descriptorType(stat) * 16;
          } catch { /* Closed inspect-unavailable observation, no raw native data. */ }
          try {
            // Compare Electron's ASAR-aware fs mapping with physical CRT fd3.
            // This is read-only and does not select/replace production IO.
            nativeMask += descriptorType(require('original-fs').fstatSync(3)) * 16 * descriptorTypes.length;
          } catch { /* Physical descriptor unavailable; no values or errors leave this child. */ }
        }
        nativeMask |= 8;
        try {
          // The read-only observation never bypasses or substitutes this writer.
          if (mode === 'junk') await new Promise((resolve, reject) => writeNativeHelperPipe(Buffer.alloc(4),
            error => error ? reject(new Error()) : resolve()));
          else await helpers.writeHelperReply(context,
            { kind: 'second_instance', owner_starts: 0, broker_starts: 0, profile_invocations: 0 });
        } catch {
          finish(summary(false, 'roundtrip_failed', null, true), EXIT_NATIVE_FAILURE + nativeMask);
          return;
        }
        finish(summary(true, 'passed', null, true));
      }).catch(failed);
    } else {
      const marker = { source_commit: 'a'.repeat(40), role: 'second-instance', session_id: randomUUID(), challenge: randomUUID() };
      const peer = (mode, privatePipe = true) => {
        const args = [__filename, '--stdout-child', '--stdout-session', marker.session_id, '--stdout-challenge', marker.challenge,
          '--stdout-write', mode];
        const child = spawn(process.execPath, args, {
          windowsHide: true, env: helpers.helperEnvironment(),
          stdio: privatePipe ? ['pipe', 'pipe', 'pipe', 'pipe'] : ['pipe', 'pipe', 'pipe'],
        });
        owned.add(child);
        const chunks = [];
        let size = 0;
        let stdoutSize = 0;
        let overflow = false;
        // Tee the unchanged chunks BEFORE the strict receiver. Never strip,
        // resync or feed the memory comparisons back into decoding/acceptance.
        child.stdio[3]?.on('data', chunk => {
          if (size + chunk.length > 65_536) { overflow = true; try { child.kill(); } catch {} return; }
          chunks.push(Buffer.from(chunk)); size += chunk.length;
        });
        child.stdout.on('data', chunk => {
          stdoutSize += chunk.length;
          if (stdoutSize > 65_536) { overflow = true; try { child.kill(); } catch {} }
        });
        child.stderr.resume();
        const closed = new Promise(resolve => child.once('close', (code, signal) => resolve({ code, signal })));
        child.stdin.on('error', () => {});
        return { child, closed, bytes: () => Buffer.concat(chunks), stdoutNonempty: () => stdoutSize > 0, overflow: () => overflow };
      };
      watchdog = setTimeout(() => finish(summary(false, 'deadline', null, true)), 25_000);
      void (async () => {
        const writing = peer('yes');
        const expected = encodeBrokerFrame({ schema_version: 1, session_id: marker.session_id, challenge: marker.challenge,
          role: marker.role, helper_pid: writing.child.pid, kind: 'second_instance', owner_starts: 0, broker_starts: 0, profile_invocations: 0 });
        const reply = helpers.receiveHelper(writing.child, marker);
        writing.child.stdin.end();
        const result = await reply.then(value => ({ accepted: value.kind === 'second_instance' && value.owner_starts === 0
          && value.broker_starts === 0 && value.profile_invocations === 0, details: null }),
        error => ({ accepted: false, details: helpers.helperFailureDetails(error) }));
        const writerEnd = await writing.closed;
        const noWrite = peer('no');
        noWrite.child.once('error', () => {});
        noWrite.child.stdin.end();
        const controlEnd = await noWrite.closed;
        const junk = peer('junk');
        const junkResult = helpers.receiveHelper(junk.child, marker).then(() => false,
          error => { const details = helpers.helperFailureDetails(error); return details?.stage === 'reply' && details.category === 'length_refused'; });
        junk.child.stdin.end();
        const junkRefused = await junkResult;
        await junk.closed;
        const missing = peer('yes', false);
        missing.child.once('error', () => {});
        missing.child.stdin.end();
        const missingEnd = await missing.closed;
        const received = writing.bytes();
        const childZero = writerEnd.code === 0 && writerEnd.signal === null;
        let mask = childZero ? 1 : 0;
        if (received.equals(expected)) mask |= 2;
        if (writing.stdoutNonempty()) mask |= 4;
        if (childZero && !writing.overflow()) mask |= 8; // A decoder-killed capture is not complete evidence.
        if (controlEnd.code === EXIT_NO_WRITE && controlEnd.signal === null) mask |= 16;
        if (noWrite.bytes().length === 0) mask |= 32;
        if (junkRefused) mask |= 64;
        if (writing.overflow() || noWrite.overflow() || junk.overflow() || missing.overflow()) mask |= 128;
        if (missingEnd.signal === null && missingEnd.code >= EXIT_NATIVE_FAILURE
          && missingEnd.code < EXIT_NATIVE_FAILURE + NATIVE_FAILURE_WIDTH) mask |= 256;
        const index = replyFailures.findIndex(([stage, category]) => stage === result.details?.stage && category === result.details?.category);
        const outcome = result.accepted ? replyFailures.length : index >= 0 ? index : replyFailures.length + 1;
        const native = childZero ? NATIVE_COMPLETED : writerEnd.signal === null
          && writerEnd.code >= EXIT_NATIVE_FAILURE && writerEnd.code < EXIT_NATIVE_FAILURE + NATIVE_FAILURE_WIDTH
          ? writerEnd.code - EXIT_NATIVE_FAILURE + 1 : 0;
        finish(summary(false, 'roundtrip_failed', result.details, true, { outcome, mask, native }));
      })().catch(failed);
    }
  }
} catch { failed(); }
