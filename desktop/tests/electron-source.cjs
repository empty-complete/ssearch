// Real source P2 proof under the admitted standard-user CI job. No visible windows.
const assert = require('node:assert/strict');
const { once } = require('node:events');
const { spawn, execFileSync } = require('node:child_process');
const { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } = require('node:fs');
const { join, resolve } = require('node:path');
const { tmpdir } = require('node:os');
const { app, BrowserWindow, ipcMain } = require('electron');
const { SourceRuntime } = require('../dist/sourceRuntime.js');
const { ServiceLifecycle } = require('../dist/serviceLifecycle.js');
const { registerAssetScheme } = require('../dist/assetProtocol.js');
const { registerDesktopIpc } = require('../dist/ipc.js');
const { createWindow } = require('../dist/windowHost.js');
const { installWindowLifecycle } = require('../dist/windowLifecycle.js');
const { acquireDesktopSingleton } = require('../dist/desktopSingleton.js');

const root = resolve(__dirname, '../..');
const artifacts = join(root, 'desktop/artifacts');
mkdirSync(artifacts, { recursive: true });
const sourceCommit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim();
const secondary = process.argv.includes('--secondary');
const orphan = process.argv.includes('--orphan');
const parameter = process.argv.indexOf('--profile');
const temporary = parameter < 0 ? mkdtempSync(join(tmpdir(), 'ssearch-electron-live-')) : null;
const profilePath = parameter < 0 ? join(temporary, 'profile Кириллица') : process.argv[parameter + 1];
const completed = [];
let current = 'admission';
const receipt = (ok, reason = 'pending') => {
  if (secondary || orphan) return;
  writeFileSync(join(artifacts, 'electron-source.json'), JSON.stringify({
    source_commit: sourceCommit, ok, completed_cases: completed,
    failed_case: ok ? null : current, reason_code: reason,
    runtime_versions: process.versions, proof_scope: 'real_source_hidden_offscreen',
  }, null, 2));
};
receipt(false);
assert.equal(process.env.GITHUB_ACTIONS, 'true');
assert.equal(process.env.BUILD_MODE, 'source');
assert.equal(sourceCommit, process.env.SOURCE_SHA);
assert.equal(process.versions.electron, '45.0.0');
app.enableSandbox();
registerAssetScheme();
class TrackingRuntime extends SourceRuntime {
  brokers = [];
  owners = [];
  spawnBroker(identity) { const child = super.spawnBroker(identity); this.brokers.push(child); return child; }
  spawnOwner(identity) { const child = super.spawnOwner(identity); this.owners.push(child); return child; }
}
const alive = child => child.exitCode === null && child.signalCode === null;
const runtime = new TrackingRuntime({ repositoryRoot: root,
  python: join(root, '.venv/Scripts/python.exe'), modelDirectory: join(profilePath, 'missing-model') });
const profile = runtime.profileIdentitySync(profilePath);
let activations = 0;
const primary = acquireDesktopSingleton(app, profile.key, () => { activations++; });
if (!primary) {
  assert.equal(secondary, true);
  assert.equal(runtime.owners.length, 0);
  assert.equal(runtime.brokers.length, 0);
  writeFileSync(join(artifacts, 'secondary-source.json'), JSON.stringify({ ok: true, source_commit: sourceCommit,
    owner_starts: 0, broker_starts: 0 }));
  app.exit(0);
} else {
  const service = new ServiceLifecycle(runtime, profile);
  let quitting = false;
  const deadline = setTimeout(() => { receipt(false, 'source_case_deadline'); app.exit(1); }, 800_000);
  const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  async function until(check, budget = 120_000) {
    const stop = Date.now() + budget;
    while (Date.now() < stop) { if (await check()) return; await pause(50); }
    throw new Error('source_case_deadline');
  }
  const runChild = args => new Promise((accept, reject) => {
    const child = spawn(process.execPath, [__filename, ...args], {
      cwd: root, env: process.env, windowsHide: true, stdio: ['ignore', 'ignore', 'ignore'],
    });
    const timer = setTimeout(() => { child.kill(); reject(new Error('owned_test_child_deadline')); }, 160_000);
    child.on('error', () => { clearTimeout(timer); reject(new Error('owned_test_child_failed')); });
    child.on('close', code => { clearTimeout(timer); code === 0 ? accept() : reject(new Error('owned_test_child_failed')); });
  });
  const edit = (window, text, submit) => window.webContents.executeJavaScript(`{
    const input=document.querySelector('input[type="search"]');
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(text)});
    input.dispatchEvent(new Event('input',{bubbles:true}));
  }`).then(async () => {
    if (submit) await window.webContents.executeJavaScript(`document.querySelector('input[type="search"]').dispatchEvent(
      new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true,cancelable:true}))`);
  });
  void app.whenReady().then(async () => {
    let window;
    try {
      current = 'source_owner_and_two_views'; receipt(false);
      const [first, again] = await Promise.all([service.ensureService(), service.ensureService()]);
      assert.equal(first.connection, again.connection);
      assert.equal(first.mode, 'owned');
      assert.equal(first.connection.owner, true);
      if (orphan) {
        writeFileSync(join(artifacts, 'orphan-source.json'), JSON.stringify({ ok: true, source_commit: sourceCommit,
          generation: first.connection.generation }));
        app.exit(0); // Deliberately omit release: the private inherited control pipe must reach EOF.
        return;
      }
      const ownerPid = first.connection.process.pid;
      const bridge = registerDesktopIpc(ipcMain);
      const operations = [];
      let dispatched = 0;
      const port = {
        get authority() { return service.authority; },
        request(method, args, id, signal) {
          if (method === 'search') dispatched++;
          return service.request(method, args, id, signal).then(reply => {
            if (method === 'search') operations.push({ query: args.query, error: reply.error?.code ?? null,
              response_query: reply.data?.query_interpretation?.original_query });
            return reply;
          });
        },
      };
      window = createWindow({ mode: 'application', assetRoot: join(root, 'frontend/dist'), bridge, servicePort: port, show: false });
      installWindowLifecycle(window, () => quitting);
      await once(window.webContents, 'did-finish-load');
      await until(() => window.webContents.executeJavaScript('!!document.querySelector(\'input[type="search"]\')'));
      const second = createWindow({ mode: 'application', assetRoot: join(root, 'frontend/dist'), bridge, servicePort: service, show: false });
      await once(second.webContents, 'did-finish-load');
      assert.equal((await service.ensureService()).connection.process.pid, ownerPid);
      assert.equal(runtime.owners.filter(alive).length, 1);
      assert.equal(runtime.brokers.filter(alive).length, 1);
      second.destroy();
      completed.push(current);

      current = 'actual_second_instance_activation_without_service_start'; receipt(false);
      await runChild(['--profile', profile.profile, '--secondary']);
      await until(() => activations === 1);
      const other = JSON.parse(readFileSync(join(artifacts, 'secondary-source.json')));
      assert.equal(other.source_commit, sourceCommit);
      assert.equal(other.owner_starts, 0);
      assert.equal(other.broker_starts, 0);
      assert.equal(runtime.owners.filter(alive).length, 1);
      assert.equal((await service.ensureService()).connection.process.pid, ownerPid);
      completed.push(current);

      current = 'actual_renderer_crash_reload_stale_reply_and_query_recovery'; receipt(false);
      const broker = runtime.brokers.filter(alive)[0];
      broker.stdout.pause();
      await edit(window, 'pending before renderer crash', true);
      await until(() => dispatched === 1);
      const recovered = once(window.webContents, 'did-finish-load');
      window.webContents.forcefullyCrashRenderer();
      await recovered;
      await until(() => window.webContents.executeJavaScript(
        'document.querySelector(\'input[type="search"]\')?.value===\'pending before renderer crash\''));
      await edit(window, 'new query after renderer recovery', true);
      await until(() => dispatched === 2);
      broker.stdout.resume();
      await until(() => operations.some(operation => operation.query === 'new query after renderer recovery'
        && operation.error === null && operation.response_query === 'new query after renderer recovery'));
      const old = operations.find(operation => operation.query === 'pending before renderer crash');
      assert.ok(['request_cancelled', 'stale_session'].includes(old.error));
      const reload = once(window.webContents, 'did-finish-load');
      window.webContents.reload();
      await reload;
      await until(() => window.webContents.executeJavaScript(
        'document.querySelector(\'input[type="search"]\')?.value===\'new query after renderer recovery\''));
      assert.equal(dispatched, 2); // Recovery restores input, never repeats a request.
      assert.equal(service.authority.generation, first.connection.generation);
      assert.equal(runtime.owners.filter(alive).length, 1);
      completed.push(current);

      current = 'close_to_tray_is_not_service_quit'; receipt(false);
      window.close();
      assert.equal(window.isDestroyed(), false);
      assert.equal(window.isVisible(), false);
      assert.equal((await service.ensureService()).connection.process.pid, ownerPid);
      completed.push(current);

      current = 'actual_parent_orphan_eof_and_next_start'; receipt(false);
      const orphanProfile = join(temporary, 'orphan profile');
      await runChild(['--profile', orphanProfile, '--orphan']);
      const endpoint = join(orphanProfile, 'engine/runtime/endpoint.json');
      await until(() => !existsSync(endpoint), 60_000);
      const state = JSON.parse(readFileSync(join(orphanProfile, 'engine/runtime/service-state.json')));
      assert.equal(state.state, 'stopped');
      const oldGeneration = JSON.parse(readFileSync(join(artifacts, 'orphan-source.json'))).generation;
      const restartIdentity = runtime.profileIdentitySync(orphanProfile);
      const restarted = new ServiceLifecycle(runtime, restartIdentity);
      try {
        const next = await restarted.ensureService();
        assert.notEqual(next.connection.generation, oldGeneration);
        assert.equal(next.connection.owner, true);
      } finally { await restarted.releaseService(); }
      completed.push(current);

      current = 'explicit_source_quit'; receipt(false);
      quitting = true;
      window.destroy();
      await service.releaseService();
      assert.equal(runtime.owners.filter(alive).length, 0);
      completed.push(current);
      receipt(true, 'passed');
      clearTimeout(deadline);
      if (temporary) rmSync(temporary, { recursive: true, force: true });
      app.exit(0);
    } catch {
      receipt(false, 'source_case_failed');
      quitting = true;
      for (const view of BrowserWindow.getAllWindows()) view.destroy();
      try { await service.releaseService(); } catch { /* Leave P2 recovery authoritative. */ }
      clearTimeout(deadline);
      app.exit(1);
    }
  });
}
