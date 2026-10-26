// GitHub-only source stdout transport proof. No service, SDK, singleton, windows,
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
const replyFailures = [
  ['spawn', 'spawn_error'], ['admission', 'refused'], ['child_exit', 'nonzero'], ['child_exit', 'signal'],
  ['deadline', 'timeout'], ['reply', 'missing'], ['reply', 'truncated'], ['reply', 'duplicate'],
  ['reply', 'binding_refused'], ['reply', 'decode_refused'], ['reply', 'length_refused'],
  ['reply', 'utf8_refused'], ['reply', 'json_refused'], ['reply', 'write_error'],
];
function summary(ok, status, details = null, pinned = false) {
  return { scope: 'source_stdout_roundtrip', ok, status, electron_pinned: pinned,
    helper_stage: details?.stage ?? null, helper_category: details?.category ?? null };
}
function finish(result, code = result.ok ? 0 : 1) {
  if (finished) return;
  finished = true;
  clearTimeout(watchdog);
  if (childMode) { app ? app.exit(code) : process.exit(code); return; }
  if (process.versions.electron) {
    const index = replyFailures.findIndex(([stage, category]) => stage === result.helper_stage && category === result.helper_category);
    code = result.ok ? EXIT_PASS : index >= 0 ? EXIT_HELPER + index : result.status === 'deadline' ? EXIT_DEADLINE : EXIT_RUNTIME;
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
      if (args.length !== 4 || args[0] !== '--stdout-session' || args[2] !== '--stdout-challenge'
        || !publicUuid(args[1]) || !publicUuid(args[3])) throw new Error();
      const context = { session_id: args[1], challenge: args[3], role: 'second-instance' };
      watchdog = setTimeout(failed, 15_000);
      void helpers.writeHelperReply(context, { kind: 'second_instance', owner_starts: 0, broker_starts: 0, profile_invocations: 0 })
        .then(() => finish(summary(true, 'passed', null, true)), failed);
    } else {
      const marker = { source_commit: 'a'.repeat(40), role: 'second-instance', session_id: randomUUID(), challenge: randomUUID() };
      const child = spawn(process.execPath, [__filename, '--stdout-child', '--stdout-session', marker.session_id,
        '--stdout-challenge', marker.challenge], {
        windowsHide: true, env: frozenServiceEnvironment(), stdio: ['pipe', 'pipe', 'pipe'],
      });
      owned.add(child);
      child.stderr.resume();
      const reply = helpers.receiveHelper(child, marker);
      child.stdin.on('error', () => {});
      child.stdin.end();
      void reply.then(value => {
        const ok = value.kind === 'second_instance' && value.owner_starts === 0 && value.broker_starts === 0 && value.profile_invocations === 0;
        finish(summary(ok, ok ? 'passed' : 'roundtrip_failed', null, true));
      }, error => finish(summary(false, 'roundtrip_failed', helpers.helperFailureDetails(error), true)));
      watchdog = setTimeout(() => finish(summary(false, 'deadline', null, true)), 25_000);
    }
  }
} catch { failed(); }
