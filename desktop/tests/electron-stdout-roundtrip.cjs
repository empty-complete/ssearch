// GitHub-only source stdout transport proof. No service, SDK, singleton, windows,
// frozen package or native acceptance. Raw Electron streams never reach job logs.
const childMode = process.argv.includes('--stdout-child');
let app;
let finished = false;
let watchdog;
let helpers;
const owned = new Set();
const statuses = ['passed', 'roundtrip_failed', 'runtime_failure', 'invalid_summary', 'deadline'];
function summary(ok, status, details = null, pinned = false) {
  return { scope: 'source_stdout_roundtrip', ok, status, electron_pinned: pinned,
    helper_stage: details?.stage ?? null, helper_category: details?.category ?? null };
}
function finish(result, code = result.ok ? 0 : 1) {
  if (finished) return;
  finished = true;
  clearTimeout(watchdog);
  if (childMode) { app ? app.exit(code) : process.exit(code); return; }
  const exit = () => app ? app.exit(code) : process.versions.electron ? process.exit(code) : (process.exitCode = code);
  const drained = [...owned].map(child => new Promise(resolve => {
    if (child.exitCode !== null || child.signalCode !== null) { resolve(true); return; }
    const timer = setTimeout(() => resolve(false), 3_000);
    child.once('close', () => { clearTimeout(timer); resolve(true); });
    try { child.kill(); } catch { clearTimeout(timer); resolve(false); }
  }));
  void Promise.all(drained).then(values => {
    if (values.some(value => !value)) { result = summary(false, 'runtime_failure', null, result.electron_pinned); code = 1; }
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
  const { BrokerFrameDecoder, encodeBrokerFrame } = require('../dist/brokerFrames.js');
  const { exactKeys, plainObject, publicUuid } = require('../dist/validation.js');
  const { frozenServiceEnvironment } = require('../dist/packagedRuntime.js');
  helpers = require('../dist/diagnosticsHelpers.js');
  if (!process.versions.electron) {
    // A Node outer driver validates/reconstructs the small closed summary and
    // suppresses all native Electron stderr and unexpected stdout contents.
    const child = spawn(require('electron'), [__filename, '--stdout-parent'], {
      windowsHide: true, env: frozenServiceEnvironment(), stdio: ['ignore', 'pipe', 'pipe'],
    });
    owned.add(child);
    child.stderr.resume();
    let bytes = Buffer.alloc(0);
    child.stdout.on('data', chunk => {
      if (bytes.length + chunk.length > 4096) { finish(summary(false, 'invalid_summary')); return; }
      bytes = Buffer.concat([bytes, chunk]);
    });
    child.once('error', failed);
    child.once('close', code => {
      try {
        const value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
        if (!plainObject(value) || !exactKeys(value, ['scope', 'ok', 'status', 'electron_pinned', 'helper_stage', 'helper_category'])
          || value.scope !== 'source_stdout_roundtrip' || typeof value.ok !== 'boolean' || typeof value.electron_pinned !== 'boolean'
          || !statuses.includes(value.status) || (value.helper_stage !== null && !helpers.helperStages.includes(value.helper_stage))
          || (value.helper_category !== null && !helpers.helperCategories.includes(value.helper_category))
          || (value.helper_stage === null) !== (value.helper_category === null)) throw new Error();
        const ok = code === 0 && value.ok && value.electron_pinned && value.status === 'passed'
          && value.helper_stage === null && value.helper_category === null;
        finish(summary(ok, ok ? 'passed' : value.status === 'passed' ? 'roundtrip_failed' : value.status,
          value.helper_stage === null ? null : { stage: value.helper_stage, category: value.helper_category }, value.electron_pinned), ok ? 0 : 1);
      } catch { finish(summary(false, 'invalid_summary')); }
    });
    watchdog = setTimeout(() => finish(summary(false, 'deadline')), 40_000);
  } else {
    app = require('electron').app;
    app.enableSandbox();
    app.on('window-all-closed', () => {});
    if (process.platform !== 'win32' || process.versions.electron !== '45.0.0' || process.env.GITHUB_ACTIONS !== 'true') {
      failed();
    } else if (childMode) {
      const decoder = new BrokerFrameDecoder(65_536);
      let context = null;
      process.stdin.on('data', chunk => {
        try {
          for (const frame of decoder.push(chunk)) {
            if (context || !plainObject(frame) || !exactKeys(frame, ['schema_version', 'session_id', 'challenge', 'role'])
              || frame.schema_version !== 1 || !publicUuid(frame.session_id) || !publicUuid(frame.challenge)
              || frame.role !== 'second-instance') throw new Error();
            context = frame;
          }
        } catch { failed(); }
      });
      process.stdin.once('end', () => {
        void (async () => {
          decoder.end();
          if (!context) throw new Error();
          await helpers.writeHelperReply(context, { kind: 'second_instance', owner_starts: 0, broker_starts: 0, profile_invocations: 0 });
          finish(summary(true, 'passed', null, true));
        })().catch(failed);
      });
      watchdog = setTimeout(failed, 15_000);
    } else {
      const marker = { source_commit: 'a'.repeat(40), role: 'second-instance', session_id: randomUUID(), challenge: randomUUID() };
      const child = spawn(process.execPath, [__filename, '--stdout-child'], {
        windowsHide: true, env: frozenServiceEnvironment(), stdio: ['pipe', 'pipe', 'pipe'],
      });
      owned.add(child);
      child.stderr.resume();
      const reply = helpers.receiveHelper(child, marker);
      child.stdin.on('error', () => {});
      child.stdin.end(encodeBrokerFrame({ schema_version: 1, session_id: marker.session_id, challenge: marker.challenge, role: marker.role }));
      void reply.then(value => {
        const ok = value.kind === 'second_instance' && value.owner_starts === 0 && value.broker_starts === 0 && value.profile_invocations === 0;
        finish(summary(ok, ok ? 'passed' : 'roundtrip_failed', null, true));
      }, error => finish(summary(false, 'roundtrip_failed', helpers.helperFailureDetails(error), true)));
      watchdog = setTimeout(() => finish(summary(false, 'deadline', null, true)), 25_000);
    }
  }
} catch { failed(); }
