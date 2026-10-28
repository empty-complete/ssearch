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
let subphase = 'not_started';
let exceptionKind = null;
const predicates = {};
const classify = error => error instanceof assert.AssertionError ? 'assertion_error'
  : error instanceof SyntaxError ? 'json_syntax_error'
  : error?.code === 'ENOENT' ? 'missing_file'
  : error?.code === 'EACCES' || error?.code === 'EPERM' ? 'file_access_denied'
  : error instanceof Error && error.message === 'source_case_deadline' ? 'case_deadline'
  : error instanceof Error && error.message === 'owned_test_child_deadline' ? 'child_deadline'
  : error instanceof Error && error.message === 'owned_test_child_failed' ? 'child_failed'
  : error instanceof Error ? 'error' : 'non_error_exception';
const receipt = (ok, reason = 'pending') => {
  if (secondary || orphan) return;
  writeFileSync(join(artifacts, 'electron-source.json'), JSON.stringify({
    source_commit: sourceCommit, ok, completed_cases: completed,
    failed_case: ok ? null : current, reason_code: reason,
    subphase, safe_predicates: predicates, exception_kind: exceptionKind,
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
// A hidden runner must finish its awaited cleanup and choose its own exit code.
app.on('window-all-closed', () => {});
class TrackingRuntime extends SourceRuntime {
  brokers = [];
  owners = [];
  failSpawns = false;
  faultIdentity(identity) {
    // OS-level ENOENT injection only; successful recovery still uses the real P2 adapter/owner.
    return this.failSpawns ? { ...identity, pythonBase: join(artifacts, 'absent-source-python.exe') } : identity;
  }
  spawnBroker(identity) { const child = super.spawnBroker(this.faultIdentity(identity)); this.brokers.push(child); return child; }
  spawnOwner(identity) { const child = super.spawnOwner(this.faultIdentity(identity)); this.owners.push(child); return child; }
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
    child.on('error', () => {
      clearTimeout(timer);
      predicates.owned_test_child_spawned = false;
      reject(new Error('owned_test_child_failed'));
    });
    child.on('spawn', () => { predicates.owned_test_child_spawned = true; });
    child.on('close', code => {
      clearTimeout(timer);
      predicates.owned_test_child_exit_zero = code === 0;
      code === 0 ? accept(child.pid) : reject(new Error('owned_test_child_failed'));
    });
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
          generation: first.connection.generation, parent: profile.parent, owner_pid: first.connection.process.pid }));
        app.exit(0); // Deliberately omit release: the private inherited control pipe must reach EOF.
        return;
      }
      const ownerPid = first.connection.process.pid;
      const bridge = registerDesktopIpc(ipcMain);
      const operations = [];
      let dispatched = 0;
      const port = {
        get authority() { return service.authority; },
        ensureReadConnection() { return service.ensureReadConnection(); },
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

      current = 'actual_explicit_read_retry_after_initial_and_exhausted_spawn_failure'; receipt(false);
      const retryRuntime = new TrackingRuntime({ repositoryRoot: root,
        python: join(root, '.venv/Scripts/python.exe'), modelDirectory: join(profilePath, 'missing-model') });
      const retryService = new ServiceLifecycle(retryRuntime, profile);
      const retryReplies = [];
      const retryPort = {
        get authority() { return retryService.authority; },
        ensureReadConnection() { return retryService.ensureReadConnection(); },
        request(method, args, id, signal) {
          return retryService.request(method, args, id, signal).then(reply => {
            if (method === 'search' && !reply.error) retryReplies.push(reply.data.query_interpretation.original_query);
            return reply;
          });
        },
      };
      let retryWindow;
      try {
        retryRuntime.failSpawns = true;
        await assert.rejects(retryService.ensureService());
        assert.equal(retryService.authority, null);
        retryWindow = createWindow({ mode: 'application', assetRoot: join(root, 'frontend/dist'),
          bridge, servicePort: retryPort, show: false });
        await once(retryWindow.webContents, 'did-finish-load');
        const retryVisible = () => retryWindow.webContents.executeJavaScript(
          "[...document.querySelectorAll('button')].some(button=>button.textContent.includes('Повторить соединение'))");
        const clickRetry = () => retryWindow.webContents.executeJavaScript(
          "[...document.querySelectorAll('button')].find(button=>button.textContent.includes('Повторить соединение')).click()");
        await until(() => retryWindow.webContents.executeJavaScript('!!document.querySelector(\'input[type="search"]\')'));
        await edit(retryWindow, 'explicit retry after startup failure', true);
        await until(retryVisible);
        retryRuntime.failSpawns = false;
        await clickRetry();
        await until(() => retryService.authority !== null);
        await until(() => retryReplies.includes('explicit retry after startup failure'));
        await until(async () => !(await retryVisible()));
        assert.equal(retryService.authority.generation, first.connection.generation);
        assert.equal((await retryService.ensureService()).mode, 'attached');
        assert.equal(runtime.owners.filter(alive).length, 1);

        const successfulBroker = retryRuntime.brokers.filter(alive)[0];
        assert.ok(successfulBroker);
        const beforeFailures = retryRuntime.brokers.length;
        retryRuntime.failSpawns = true;
        successfulBroker.kill(); // Only our retained broker, never the shared attached owner.
        await until(() => retryRuntime.brokers.length >= beforeFailures + 6, 30_000);
        // Each failed opening tries before/after its failed owner child; three openings exhaust backoff.
        const exhaustedCount = retryRuntime.brokers.length;
        await pause(6000);
        assert.equal(retryRuntime.brokers.length, exhaustedCount);
        assert.equal(retryService.authority, null);
        assert.equal(runtime.owners.filter(alive).length, 1);
        retryRuntime.failSpawns = false;
        await edit(retryWindow, 'explicit retry after exhausted reconnect', true);
        await until(() => retryService.authority !== null);
        await until(() => retryReplies.includes('explicit retry after exhausted reconnect'));
        await until(async () => !(await retryVisible()));
        assert.equal(retryService.authority.generation, first.connection.generation);
        assert.equal(runtime.owners.filter(alive).length, 1);
      } finally {
        retryWindow?.destroy();
        await retryService.releaseService();
      }
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
      subphase = 'orphan_child_ensure_and_abrupt_exit'; receipt(false);
      const orphanParentPid = await runChild(['--profile', orphanProfile, '--orphan']);
      subphase = 'orphan_ready_receipt'; receipt(false);
      const orphanReady = JSON.parse(readFileSync(join(artifacts, 'orphan-source.json')));
      predicates.orphan_ready_current = orphanReady.ok === true && orphanReady.source_commit === sourceCommit;
      assert.equal(predicates.orphan_ready_current, true);
      predicates.orphan_main_pid_matches_spawn = orphanReady.parent.pid === orphanParentPid;
      predicates.orphan_main_executable_matches_spawn = orphanReady.parent.executable.toLowerCase() === process.execPath.toLowerCase();
      predicates.orphan_owner_distinct_from_main = Number.isInteger(orphanReady.owner_pid)
        && orphanReady.owner_pid > 0 && orphanReady.owner_pid !== orphanParentPid;
      assert.equal(predicates.orphan_main_pid_matches_spawn, true);
      assert.equal(predicates.orphan_main_executable_matches_spawn, true);
      assert.equal(predicates.orphan_owner_distinct_from_main, true);
      assert.match(orphanReady.generation, /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/);
      const oldGeneration = orphanReady.generation;
      const endpoint = join(orphanProfile, 'engine/runtime/endpoint.json');
      const stateFile = join(orphanProfile, 'engine/runtime/service-state.json');
      subphase = 'orphan_eof_endpoint_removed'; receipt(false);
      await until(() => {
        predicates.orphan_endpoint_absent = !existsSync(endpoint);
        predicates.orphan_state_present = existsSync(stateFile);
        try {
          const observed = JSON.parse(readFileSync(stateFile));
          predicates.orphan_state_readable = true;
          predicates.orphan_state_stopped = observed.state === 'stopped';
          predicates.orphan_state_same_generation = observed.generation === oldGeneration;
        } catch { predicates.orphan_state_readable = false; }
        return predicates.orphan_endpoint_absent;
      }, 60_000);
      subphase = 'orphan_durable_stopped_state'; receipt(false);
      predicates.orphan_state_present = existsSync(stateFile);
      const state = JSON.parse(readFileSync(stateFile));
      predicates.orphan_state_stopped = state.state === 'stopped';
      predicates.orphan_state_same_generation = state.generation === oldGeneration;
      assert.equal(predicates.orphan_state_stopped, true);
      assert.equal(predicates.orphan_state_same_generation, true);
      subphase = 'restart_profile_identity'; receipt(false);
      const restartIdentity = runtime.profileIdentitySync(orphanProfile);
      const restarted = new ServiceLifecycle(runtime, restartIdentity);
      let restartFailed = false;
      try {
        subphase = 'restart_verified_ensure'; receipt(false);
        const next = await restarted.ensureService();
        predicates.restart_generation_changed = next.connection.generation !== oldGeneration;
        predicates.restart_owner_verified = next.connection.owner === true;
        assert.equal(predicates.restart_generation_changed, true);
        assert.equal(predicates.restart_owner_verified, true);
      } catch (error) {
        restartFailed = true;
        throw error;
      } finally {
        if (!restartFailed) { subphase = 'restart_graceful_release'; receipt(false); }
        try {
          await restarted.releaseService();
          predicates.restart_release_clean = true;
        } catch (error) {
          predicates.restart_release_clean = false;
          if (!restartFailed) throw error;
        }
      }
      completed.push(current);

      current = 'explicit_source_quit'; receipt(false);
      quitting = true;
      window.destroy();
      subphase = 'primary_graceful_release'; receipt(false);
      await service.releaseService();
      predicates.all_retained_owners_exited = runtime.owners.filter(alive).length === 0;
      assert.equal(predicates.all_retained_owners_exited, true);
      completed.push(current);
      clearTimeout(deadline);
      subphase = 'temporary_fixture_cleanup'; receipt(false);
      if (temporary) rmSync(temporary, { recursive: true, force: true });
      subphase = 'complete'; receipt(true, 'passed');
      app.exit(0);
    } catch (error) {
      exceptionKind = classify(error);
      receipt(false, 'source_case_failed');
      quitting = true;
      for (const view of BrowserWindow.getAllWindows()) view.destroy();
      try {
        await service.releaseService();
        predicates.failure_cleanup_clean = true;
      } catch { predicates.failure_cleanup_clean = false; }
      receipt(false, 'source_case_failed');
      clearTimeout(deadline);
      app.exit(1);
    }
  });
}
