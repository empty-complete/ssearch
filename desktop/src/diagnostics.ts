import { app, BrowserWindow, ipcMain, session, type IpcMainInvokeEvent } from 'electron';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { createServer, type Server } from 'node:http';
import type { ChildProcess } from 'node:child_process';
import { PackagedRuntime } from './packagedRuntime.js';
import { ServiceLifecycle } from './serviceLifecycle.js';
import { acquireDesktopSingleton } from './desktopSingleton.js';
import { installWindowLifecycle } from './windowLifecycle.js';
import { createWindow } from './windowHost.js';
import { registerDesktopIpc, type ServicePort } from './ipc.js';
import { inspectUiProof, reserveUiProof } from './diagnosticsReport.js';
import { canonicalDirectory, uiProofBudgets, proofChecksPassed, type UiProofRequest } from './diagnosticsPolicy.js';
import { authenticateUiHelper, startUiHelper, writeHelperReply } from './diagnosticsHelpers.js';
import { BrokerStdinAudit, type BrokerFrameCounts } from './diagnosticsWire.js';
import { classifyProofError, recordProofFailure, type ProofFailureCategory, type ProofFailurePhase } from './diagnosticsOutcome.js';
import type { ProfileIdentity } from './runtime.js';
import type { JsonObject, UiMethod } from './contracts.js';
import { plainObject, publicUuid, validResponse } from './validation.js';

export { requiredUiChecks, uiProofBudgets } from './diagnosticsPolicy.js';
const alive = (child: ChildProcess) => child.exitCode === null && child.signalCode === null;

class MeasuredRuntime extends PackagedRuntime {
  owners: ChildProcess[] = [];
  brokers: ReturnType<PackagedRuntime['spawnBroker']>[] = [];
  profiles = 0;
  readonly wire = new BrokerStdinAudit();
  override profileIdentitySync(profile?: string) { this.profiles++; return super.profileIdentitySync(profile); }
  override spawnOwner(profile: ProfileIdentity) { const child = super.spawnOwner(profile); this.owners.push(child); return child; }
  override spawnBroker(profile: ProfileIdentity) {
    const child = super.spawnBroker(profile);
    this.brokers.push(child);
    this.wire.observe(child.stdin);
    return child;
  }
}

/** Packaged-only hidden proof entry. Never routes through normal bootstrap/tray/default profile. */
export async function runUiProof(request: UiProofRequest): Promise<number> {
  let prepared: ReturnType<typeof reserveUiProof>;
  let runtime: MeasuredRuntime;
  let helper: ReturnType<typeof authenticateUiHelper> | null = null;
  try {
    const inspected = inspectUiProof(request, { packaged: app.isPackaged, executable: process.execPath,
      resources: process.resourcesPath, appPath: app.getAppPath(), appVersion: app.getVersion(), versions: process.versions });
    if (request.helper) helper = authenticateUiHelper({ ...request, profilePath: inspected.paths.profile,
      reportPath: inspected.paths.report }, inspected.root, inspected.report.source_commit, app.getVersion());
    runtime = new MeasuredRuntime({ installationRoot: inspected.root, expectedServiceVersion: app.getVersion() });
    prepared = reserveUiProof(inspected);
  } catch (error) {
    return error instanceof Error && ['ui_proof_arguments_invalid', 'ui_proof_paths_invalid',
      'ui_proof_helper_context_invalid', 'ui_proof_packaged_required'].includes(error.message) ? 2 : 1;
  }
  const { report, writer, paths, assetRoot } = prepared;
  report.measured_counts.read_only_profile_invocations = helper ? 1 : 0;
  const services: ServiceLifecycle[] = [];
  const helpers: ChildProcess[] = [];
  const seen = new Set<number>();
  const ownedProfiles = new Map<string, string>();
  const controller = new AbortController();
  let quitting = false;
  let activations = 0;
  let deadline: ReturnType<typeof setTimeout> | undefined;
  const network: { server?: Server } = {};
  let failure = 'pending';
  let caseCompleted = false;
  let helperCompleted = false;
  let cleanupFailed = false;
  let fatal = false;
  const mutationProof: { value?: { id: string; before: BrokerFrameCounts } } = {};
  let profile: ProfileIdentity | null = null;
  let bridge: ReturnType<typeof registerDesktopIpc>;
  const pause = (milliseconds: number) => new Promise(resolve => setTimeout(resolve, milliseconds));
  const guard = () => { if (controller.signal.aborted) throw new Error('ui_proof_cancelled'); };
  const update = (phase: ProofFailurePhase = 'bootstrap') => {
    report.measured_counts.owner_starts = runtime.owners.length;
    report.measured_counts.broker_starts = runtime.brokers.length;
    report.measured_counts.live_retained_owner_children = runtime.owners.filter(alive).length;
    report.measured_counts.read_only_profile_invocations = runtime.profiles + (helper ? 1 : 0)
      + helperProfileInvocations;
    report.measured_counts.verified_authority_pids = [...seen].sort((a, b) => a - b);
    try { report.measured_counts.broker_stdin_frame_counts = runtime.wire.snapshot(); }
    catch {
      fatal = true;
      recordProofFailure(report, 'ui_proof_wire_audit_incomplete', { phase, category: 'wire_audit',
        wireCategory: runtime.wire.failureCategory ?? 'unclassified' });
      controller.abort();
    }
    writer.write(phase);
  };
  let helperProfileInvocations = 0;
  const until = async (predicate: () => boolean | Promise<boolean>, budget = 60_000) => {
    const stop = Date.now() + budget;
    while (Date.now() < stop) { guard(); if (await predicate()) return; await pause(50); }
    throw new Error('ui_proof_case_deadline');
  };
  const measured = async (name: string, action: () => Promise<void> | void) => {
    guard();
    const check = report.checks.find(item => item.name === name);
    if (!check) throw new Error('ui_proof_check_unknown');
    failure = name;
    update('audit_before_action');
    try {
      guard(); // An audit refusal must abort before the action (including window creation).
      await action();
      guard();
      check.status = 'pass';
      update('audit_after_action');
    } catch (error) {
      check.status = 'fail';
      recordProofFailure(report, name, { phase: 'action', category: classifyProofError(error) });
      update('audit_after_action');
      throw new Error('ui_proof_check_failed');
    }
  };
  const connection = async (service: ServiceLifecycle) => {
    guard();
    const value = await service.ensureService();
    guard();
    report.measured_counts.service_method_calls.status++;
    const health = await service.request('status', {}, randomUUID(), controller.signal);
    assert.equal(health.error, null); assert.ok(plainObject(health.data));
    assert.equal(health.data.app_version, app.getVersion()); assert.equal(health.data.service_version, app.getVersion());
    assert.equal(typeof health.data.service_version, 'string');
    seen.add(value.connection.process.pid);
    report.owner = { pid: value.connection.process.pid, created: value.connection.process.created,
      generation: value.connection.generation, installation_id: value.connection.installationId };
    report.versions.service = health.data.service_version as string;
    if (value.mode === 'owned') ownedProfiles.set(service.profile.profile, value.connection.generation);
    return value;
  };
  const start = () => { guard(); const value = new ServiceLifecycle(runtime, profile!); services.push(value); return value; };
  const call = async (service: ServiceLifecycle, method: UiMethod, args: JsonObject = {}, id: string = randomUUID()) => {
    guard(); report.measured_counts.service_method_calls[method]++;
    return service.request(method, args, id, controller.signal);
  };
  const data = async (service: ServiceLifecycle, method: UiMethod, args: JsonObject = {}) => {
    const reply = await call(service, method, args);
    guard(); assert.equal(reply.error, null); assert.ok(plainObject(reply.data)); return reply.data as JsonObject;
  };
  const observedPort = (port: ServicePort): ServicePort => ({
    get authority() { return port.authority; },
    ...(port.ensureReadConnection ? { ensureReadConnection: () => port.ensureReadConnection!() } : {}),
    request(method, args, id, signal) {
      report.measured_counts.service_method_calls[method]++;
      return port.request(method, args, id, signal);
    },
  });
  const windowFor = async (mode: 'application' | 'bootstrapper', port?: ServicePort, assets = assetRoot) => {
    guard();
    const window = createWindow({ mode, assetRoot: assets, bridge, servicePort: port ? observedPort(port) : undefined, show: false });
    await once(window.webContents, 'did-finish-load');
    guard(); assert.equal(window.isVisible(), false); assert.equal(window.webContents.getURL(), 'ssearch://ui/');
    await until(() => window.webContents.executeJavaScript("!!document.querySelector('input[type=search]')"));
    return window;
  };
  const expose = async (window: BrowserWindow) => {
    const value = await window.webContents.executeJavaScript(`({node:[typeof require,typeof process,typeof Buffer,typeof ipcRenderer],
      keys:Object.keys(window.ssearch).sort(), serialized:JSON.stringify(window.ssearch)})`);
    assert.deepEqual(value.node, ['undefined', 'undefined', 'undefined', 'undefined']);
    assert.deepEqual(value.keys, ['cancelJob', 'fetch', 'search', 'settingsApply', 'settingsGet', 'settingsValidate', 'startCatalog', 'status']);
    assert.equal(value.serialized, '{}');
  };
  const edit = async (window: BrowserWindow, text: string, submit = true) => {
    guard();
    await window.webContents.executeJavaScript(`{const input=document.querySelector('input[type=search]');
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(text)});
      input.dispatchEvent(new Event('input',{bubbles:true}));}`);
    if (submit) await window.webContents.executeJavaScript(`document.querySelector('input[type=search]').dispatchEvent(
      new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true,cancelable:true}))`);
  };
  app.on('window-all-closed', () => {});
  let interrupt!: (error: Error) => void;
  const interrupted = new Promise<never>((_resolve, reject) => { interrupt = reject; });
  // A helper can finish without app.whenReady; still observe its bounded failure path.
  void interrupted.catch(() => {});
  const bounded = <T>(operation: Promise<T>) => Promise.race([operation, interrupted]);
  const emergency = (error: unknown) => {
    fatal = true;
    failure = 'unhandled_runtime_failure';
    recordProofFailure(report, 'unhandled_runtime_failure', { phase: 'unhandled', category: classifyProofError(error) });
    controller.abort();
    interrupt(new Error('ui_proof_runtime_failed'));
  };
  process.on('uncaughtException', emergency);
  process.on('unhandledRejection', emergency);
  deadline = setTimeout(() => {
    recordProofFailure(report, 'ui_proof_deadline', { phase: 'deadline', category: 'deadline' });
    controller.abort();
    interrupt(new Error('ui_proof_deadline'));
  }, helper ? 180_000 : uiProofBudgets[request.scenario]);
  try {
    const appData = join(helper?.parentRoot ?? paths.profile, 'shell-appdata');
    for (const directory of [appData, ...['shell-userdata', 'shell-session', 'crash-dumps', 'logs', 'temp'].map(name => join(paths.profile, name))]) {
      try { mkdirSync(directory, { mode: 0o700 }); } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error; }
      canonicalDirectory(directory);
    }
    app.setPath('appData', appData);
    app.setPath('userData', join(paths.profile, 'shell-userdata'));
    app.setPath('sessionData', join(paths.profile, 'shell-session'));
    app.setPath('crashDumps', join(paths.profile, 'crash-dumps'));
    app.setPath('temp', join(paths.profile, 'temp'));
    app.setAppLogsPath(join(paths.profile, 'logs'));
    process.env.TEMP = process.env.TMP = join(paths.profile, 'temp');
    if (request.scenario !== 'load') profile = runtime.profileIdentitySync(paths.profile);
    const singletonProfile = helper?.role === 'second-instance' ? helper.profile : profile;
    const primary = singletonProfile ? acquireDesktopSingleton(app, singletonProfile.key, () => { activations++; }) : true;
    if (helper?.role === 'second-instance') {
      assert.equal(primary, false);
      report.checks = [{ name: 'native_parent_epoch_verified', status: 'pass' },
        { name: 'same_singleton_refused_without_service_start', status: 'pass' }];
      assert.equal(runtime.owners.length + runtime.brokers.length, 0);
      await bounded(writeHelperReply(helper, { kind: 'second_instance', owner_starts: 0, broker_starts: 0,
        profile_invocations: runtime.profiles + 1 }));
      helperCompleted = true;
    } else if (helper?.role === 'orphan') {
      assert.equal(primary, true);
      await bounded(app.whenReady());
      const owned = start();
      const ready = await connection(owned);
      assert.equal(ready.mode, 'owned'); assert.equal(ready.connection.owner, true);
      report.checks = [{ name: 'native_parent_epoch_verified', status: 'pass' }, { name: 'fresh_owned_service_verified', status: 'pass' }];
      await bounded(writeHelperReply(helper, { kind: 'orphan_ready', owner_starts: runtime.owners.length,
        broker_starts: runtime.brokers.length, profile_invocations: runtime.profiles + 1,
        generation: ready.connection.generation, owner_pid: ready.connection.process.pid }));
      helperCompleted = true;
    } else {
      assert.equal(primary, true);
      update();
      await bounded(app.whenReady());
      bridge = registerDesktopIpc(ipcMain);
      const execute = async () => {
        if (request.scenario === 'load') {
          let view!: BrowserWindow;
          await measured('installed_bootstrap_loaded', async () => { view = await windowFor('bootstrapper'); });
          await measured('sandboxed_named_bridge', () => expose(view));
          await measured('unavailable_without_service', async () => {
            const reply = await view.webContents.executeJavaScript('window.ssearch.status({}, {request_id:crypto.randomUUID()})');
            assert.equal(reply.error.code, 'service_unavailable');
          });
          await measured('no_service_start', () => {
            assert.equal(runtime.profiles + runtime.owners.length + runtime.brokers.length, 0);
            assert.equal(existsSync(join(paths.profile, 'engine')), false);
          });
        } else if (request.scenario === 'empty-profile') await emptyProfile();
        else if (request.scenario === 'security') await security();
        else await lifecycle();
      };
      await bounded(execute());
      caseCompleted = true;
    }
  } catch (error) {
    recordProofFailure(report, failure === 'pending' ? 'ui_proof_runtime_failed' : failure,
      { phase: failure === 'pending' ? 'bootstrap' : 'action', category: classifyProofError(error) });
  }
  finally {
    if (deadline) clearTimeout(deadline);
    // An orphan helper's only successful action is abrupt exit; leave its retained owner for the native lease.
    if (!(helper?.role === 'orphan' && helperCompleted && !fatal)) {
      controller.abort(); quitting = true;
      for (const view of BrowserWindow.getAllWindows()) view.destroy();
      const cleanup = report.checks.find(item => item.name === 'clean_shutdown');
      let cleanupStage: ProofFailureCategory = 'runtime_failure';
      try {
        let cleanupDeadline: ReturnType<typeof setTimeout> | undefined;
        try {
          const stopHelpers = helpers.map(async child => {
            if (!alive(child)) return;
            const closed = once(child, 'close');
            child.kill(); // Only our retained QA helper; its owner retains the native parent lease.
            await closed;
          });
          const stopped = Promise.allSettled([...services.map(value => value.releaseService()), ...stopHelpers])
            .then(results => { assert.ok(results.every(result => result.status === 'fulfilled')); });
          await Promise.race([stopped,
            new Promise<never>((_resolve, reject) => {
              cleanupDeadline = setTimeout(() => reject(new Error('ui_proof_cleanup_deadline')), 55_000);
            })]);
        } finally { if (cleanupDeadline) clearTimeout(cleanupDeadline); }
        cleanupStage = 'retained_child';
        assert.equal(runtime.owners.filter(alive).length, 0);
        assert.equal(runtime.brokers.filter(alive).length, 0);
        cleanupStage = 'wire_audit';
        runtime.wire.snapshot();
        if (mutationProof.value) runtime.wire.requireSingleMutation('settings_apply', mutationProof.value.id, mutationProof.value.before);
        if (request.scenario === 'empty-profile') {
          cleanupStage = 'assertion';
          assert.equal(report.measured_counts.service_method_calls.start_catalog, 0);
          assert.equal(runtime.wire.snapshot().start_catalog, 0);
        }
        cleanupStage = 'owner_state';
        for (const [ownedProfile, generation] of ownedProfiles) {
          assert.equal(existsSync(join(ownedProfile, 'engine/runtime/endpoint.json')), false);
          const state = JSON.parse(readFileSync(join(ownedProfile, 'engine/runtime/service-state.json'), 'utf8'));
          assert.equal(state.state, 'stopped');
          assert.equal(state.generation, generation);
        }
        if (cleanup) cleanup.status = 'pass';
      } catch (error) {
        cleanupFailed = true;
        if (cleanup) cleanup.status = 'fail';
        const classified = classifyProofError(error);
        const category = classified === 'deadline' ? classified : cleanupStage;
        const wireCategory = runtime.wire.failureCategory;
        recordProofFailure(report, 'ui_proof_cleanup_failed', { phase: 'cleanup', category,
          cleanupCategory: category, ...(wireCategory ? { wireCategory } : {}) });
      }
    }
    if (!helper) {
      report.ok = !report.failure_diagnostics && proofChecksPassed(request.scenario, report.checks, caseCompleted && !fatal);
      if (report.ok) report.reason_code = 'passed';
    } else {
      report.ok = !report.failure_diagnostics && helperCompleted && !fatal && !cleanupFailed && report.checks.every(check => check.status === 'pass');
      if (report.ok) report.reason_code = 'passed';
    }
    const probeServer = network.server;
    if (probeServer) {
      probeServer.closeAllConnections();
      await new Promise<void>(resolve => probeServer.close(() => resolve()));
    }
    ipcMain.removeHandler('ssearch-proof:context'); ipcMain.removeHandler('ssearch-proof:result');
    process.removeListener('uncaughtException', emergency); process.removeListener('unhandledRejection', emergency);
    try { update('final_report'); }
    finally { writer.close(); }
  }
  return report.ok ? 0 : 1;

  async function emptyProfile() {
    let service = start();
    let view!: BrowserWindow;
    let revision = 0;
    let generation = '';
    const rendererData = async (method: 'status' | 'settingsGet' | 'settingsValidate' | 'settingsApply', args: JsonObject = {}) => {
      guard();
      const id = randomUUID();
      const reply: unknown = await view.webContents.executeJavaScript(`window.ssearch[${JSON.stringify(method)}](
        ${JSON.stringify(args)}, {request_id:${JSON.stringify(id)}})`);
      guard();
      assert.ok(validResponse(reply, id));
      assert.equal(reply.error, null);
      assert.ok(plainObject(reply.data));
      return reply.data as JsonObject;
    };
    const empty = async () => {
      const settings = await rendererData('settingsGet');
      assert.ok(plainObject(settings.settings));
      assert.deepEqual(settings.settings.sources, []);
      const health = await rendererData('status');
      assert.equal(health.app_version, app.getVersion()); assert.equal(health.service_version, app.getVersion());
      assert.ok(plainObject(health.components)); assert.deepEqual(health.components.jobs, []);
      assert.deepEqual(health.components.sources, []);
      assert.equal(report.measured_counts.service_method_calls.start_catalog, 0);
      assert.equal(runtime.wire.snapshot().start_catalog, 0);
      return settings;
    };
    await measured('installed_renderer_and_named_ipc', async () => {
      const ready = await connection(service); generation = ready.connection.generation;
      assert.equal(ready.connection.owner, true);
      view = await windowFor('application', service);
      await expose(view);
      const health = await rendererData('status');
      assert.equal(health.service_version, app.getVersion());
    });
    await measured('fresh_sources_empty_and_jobs_zero', async () => { await empty(); });
    await measured('real_settings_revision_apply', async () => {
      const before = await empty();
      assert.ok(Number.isInteger(before.revision) && (before.revision as number) >= 1);
      const candidate = structuredClone(before.settings) as JsonObject;
      assert.ok(plainObject(candidate.ui)); candidate.ui.theme = 'dark';
      assert.equal((await rendererData('settingsValidate', { candidate, expected_revision: before.revision! })).valid, true);
      const applied = await rendererData('settingsApply', { candidate, expected_revision: before.revision! });
      revision = (before.revision as number) + 1;
      assert.equal(applied.revision, revision); assert.equal((await empty()).revision, revision);
    });
    await measured('restart_preserves_empty_sources_and_revision', async () => {
      const previousId = view.webContents.id;
      const previousSession = view.webContents.session;
      view.destroy();
      await service.releaseService(); assert.equal(runtime.owners.filter(alive).length, 0);
      service = start(); const next = await connection(service);
      assert.notEqual(next.connection.generation, generation);
      view = await windowFor('application', service);
      assert.notEqual(view.webContents.id, previousId);
      assert.notEqual(view.webContents.session, previousSession);
      await expose(view);
      assert.equal((await empty()).revision, revision);
    });
  }
  async function security() {
    const service = start(); await connection(service);
    let product!: BrowserWindow;
    await measured('installed_renderer_loaded', async () => { product = await windowFor('application', service); });
    await measured('sandboxed_named_bridge', () => expose(product));
    const fixture = join(paths.profile, 'security-fixture'); mkdirSync(fixture, { mode: 0o700 });
    writeFileSync(join(fixture, 'index.html'), '<!doctype html><script>globalThis.inlineRan=true</script><script src="probe.js"></script><body>negative policy fixture</body>');
    writeFileSync(join(fixture, 'LICENSE.txt'), 'negative download fixture');
    writeFileSync(join(fixture, 'probe.js'), `globalThis.probeReady=true;globalThis.violations=[];
      addEventListener('securitypolicyviolation',event=>violations.push(event.effectiveDirective));
      try{eval('globalThis.evalRan=true')}catch{}
      globalThis.deniedLoads=async url=>{for(const [tag,attribute,path] of [['script','src','script.js'],['img','src','image.png'],['iframe','src','frame']]){
        const node=document.createElement(tag);node[attribute]=url+'/'+path;document.body.append(node)}try{await fetch(url+'/fetch')}catch{}};`);
    guard();
    const negative = createWindow({ mode: 'application', assetRoot: fixture, bridge, show: false });
    await once(negative.webContents, 'did-finish-load');
    let requests = 0;
    const server = createServer((_request, response) => { requests++; response.end('negative loopback probe'); });
    network.server = server;
    server.listen(0, '127.0.0.1'); await once(server, 'listening');
    const address = server.address(); assert.ok(address && typeof address === 'object');
    const remote = `http://127.0.0.1:${address.port}`; // In-memory negative probe only; no external asset host or writes.
    await measured('real_csp_and_interception', async () => {
      assert.equal(await negative.webContents.executeJavaScript('globalThis.probeReady'), true);
      assert.equal(await negative.webContents.executeJavaScript('globalThis.inlineRan===undefined&&globalThis.evalRan===undefined'), true);
      await negative.webContents.executeJavaScript(`deniedLoads(${JSON.stringify(remote)})`);
      await until(async () => {
        const value = await negative.webContents.executeJavaScript('globalThis.violations');
        return ['script-src-elem', 'img-src', 'frame-src', 'connect-src'].every(directive => value.includes(directive));
      });
      await assert.rejects(negative.webContents.session.fetch(`${remote}/interception`));
      for (const url of ['ssearch://foreign/', 'ssearch://ui/index.html?query=1', 'ssearch://ui/missing.js', 'file:///C:/blocked']) {
        await assert.rejects(negative.webContents.session.fetch(url));
      }
      assert.equal(requests, 0);
    });
    await measured('navigation_windows_permissions_downloads_denied', async () => {
      const before = negative.webContents.getURL();
      await negative.webContents.executeJavaScript(`location.href=${JSON.stringify(remote)}`); await pause(150);
      assert.equal(negative.webContents.getURL(), before);
      const windows = BrowserWindow.getAllWindows().length;
      await negative.webContents.executeJavaScript(`window.open(${JSON.stringify(remote)})`); await pause(150);
      assert.equal(BrowserWindow.getAllWindows().length, windows);
      assert.equal(await negative.webContents.executeJavaScript('Notification.requestPermission()'), 'denied');
      let prevented = false;
      negative.webContents.session.on('will-download', event => { prevented = event.defaultPrevented; });
      negative.webContents.downloadURL('ssearch://ui/LICENSE.txt'); await until(() => prevented);
      assert.equal(requests, 0);
    });
    await measured('real_sender_frame_origin_denials', async () => {
      const isolated = session.fromPartition(`ssearch-proof-adversarial-${randomUUID()}`);
      isolated.protocol.handle('ssearch', incoming => ['ssearch://ui/', 'ssearch://foreign/'].includes(incoming.url)
        ? new Response('<!doctype html><body>adversarial sender fixture</body>', { headers: { 'Content-Type': 'text/html' } })
        : new Response('', { status: 403 }));
      let calls = 0;
      const realPort: ServicePort = { get authority() { return service.authority; },
        request(method, args, id, signal) { calls++; return service.request(method, args, id, signal); } };
      const options = { show: false, webPreferences: { session: isolated, sandbox: true, contextIsolation: true,
        nodeIntegration: false, nodeIntegrationInSubFrames: true, offscreen: true,
        preload: join(__dirname, 'uiProbePreload.js') } };
      const authorized = new BrowserWindow(options);
      const boundary = bridge.bind(authorized.webContents, isolated, observedPort(realPort));
      let savedContext: ReturnType<typeof boundary.handshake> = null;
      const results: Array<{ event: IpcMainInvokeEvent; result: any }> = [];
      ipcMain.handle('ssearch-proof:context', event => {
        if (event.sender === authorized.webContents && event.senderFrame === authorized.webContents.mainFrame
          && event.senderFrame?.url === 'ssearch://ui/') savedContext = boundary.handshake(event);
        return savedContext;
      });
      ipcMain.handle('ssearch-proof:result', (event, result: unknown) => { results.push({ event, result }); });
      await authorized.loadURL('ssearch://ui/');
      await until(() => results.some(item => item.event.sender === authorized.webContents
        && item.event.senderFrame === authorized.webContents.mainFrame));
      const valid = results.find(item => item.event.senderFrame === authorized.webContents.mainFrame)!.result;
      assert.equal(valid.fixtureFailure, undefined); assert.equal(valid.valid.error, null);
      assert.equal(valid.valid.data.service_version, app.getVersion());
      assert.equal(valid.forged.error.code, 'access_denied'); assert.equal(valid.oversized.error.code, 'request_too_large');
      assert.equal(valid.wrongChannel.error.code, 'access_denied'); assert.equal(valid.unknownDenied, true); assert.equal(calls, 1);
      await authorized.webContents.executeJavaScript(`{const frame=document.createElement('iframe');frame.srcdoc='<p>frame</p>';document.body.append(frame)}`);
      await until(() => results.some(item => item.event.sender === authorized.webContents && item.event.senderFrame !== authorized.webContents.mainFrame));
      const child = results.find(item => item.event.sender === authorized.webContents && item.event.senderFrame !== authorized.webContents.mainFrame)!;
      assert.equal(child.result.valid.error.code, 'access_denied');
      const foreign = new BrowserWindow(options); await foreign.loadURL('ssearch://ui/');
      await until(() => results.some(item => item.event.sender === foreign.webContents));
      assert.equal(results.find(item => item.event.sender === foreign.webContents)!.result.valid.error.code, 'access_denied');
      results.length = 0; await authorized.loadURL('ssearch://foreign/');
      await until(() => results.some(item => item.event.sender === authorized.webContents));
      assert.equal(results[0].result.valid.error.code, 'access_denied'); assert.equal(calls, 1);
    });
  }
  async function lifecycle() {
    const service = start();
    let first!: Awaited<ReturnType<ServiceLifecycle['ensureService']>>;
    let view!: BrowserWindow;
    let dispatched = 0;
    const replies: Array<{ query: unknown; error: string | null; original: unknown }> = [];
    const port: ServicePort = {
      get authority() { return service.authority; },
      ensureReadConnection: () => service.ensureReadConnection(),
      request(method, args, id, signal) {
        if (method === 'search') dispatched++;
        return service.request(method, args, id, signal).then(reply => {
          if (method === 'search') replies.push({ query: args.query, error: reply.error?.code ?? null,
            original: plainObject(reply.data) && plainObject(reply.data.query_interpretation)
              ? reply.data.query_interpretation.original_query : null });
          return reply;
        });
      },
    };
    await measured('two_views_one_verified_owner', async () => {
      const [a, b] = await Promise.all([connection(service), connection(service)]); first = a;
      assert.equal(a.connection, b.connection); assert.equal(a.mode, 'owned'); assert.equal(a.connection.owner, true);
      view = await windowFor('application', port); installWindowLifecycle(view, () => quitting);
      const second = await windowFor('application', service);
      assert.equal((await connection(service)).connection.process.pid, first.connection.process.pid);
      assert.equal(runtime.owners.filter(alive).length, 1); assert.equal(runtime.brokers.filter(alive).length, 1);
      second.destroy();
    });
    await measured('second_instance_without_service_start', async () => {
      const helper = startUiHelper(paths.profile, report.source_commit, 'second-instance', profile!); helpers.push(helper.child);
      const result = await helper.reply;
      assert.equal(result.kind, 'second_instance'); assert.equal(result.owner_starts, 0); assert.equal(result.broker_starts, 0);
      assert.ok(Number.isInteger(result.profile_invocations) && (result.profile_invocations as number) >= 1);
      helperProfileInvocations += result.profile_invocations as number;
      await until(() => activations === 1);
      assert.equal(runtime.owners.filter(alive).length, 1);
    });
    await measured('attached_release_safe', async () => {
      const attached = start(); const ready = await connection(attached);
      assert.equal(ready.mode, 'attached'); assert.equal(ready.connection.owner, true);
      assert.equal(ready.connection.generation, first.connection.generation);
      await attached.releaseService(); assert.equal((await call(service, 'status')).error, null);
      assert.equal(runtime.owners.filter(alive).length, 1);
    });
    await measured('renderer_crash_query_and_stale_response', async () => {
      const broker = runtime.brokers.find(alive); assert.ok(broker); broker.stdout.pause();
      await edit(view, 'pending before renderer crash'); await until(() => dispatched === 1);
      const loaded = once(view.webContents, 'did-finish-load'); view.webContents.forcefullyCrashRenderer(); await loaded;
      await until(() => view.webContents.executeJavaScript("document.querySelector('input[type=search]')?.value==='pending before renderer crash'"));
      await edit(view, 'new query after renderer recovery'); await until(() => dispatched === 2); broker.stdout.resume();
      await until(() => replies.some(reply => reply.query === 'new query after renderer recovery' && reply.error === null
        && reply.original === 'new query after renderer recovery'));
      assert.ok(['request_cancelled', 'stale_session'].includes(replies.find(reply => reply.query === 'pending before renderer crash')?.error ?? ''));
      const reloaded = once(view.webContents, 'did-finish-load'); view.webContents.reload(); await reloaded;
      await until(() => view.webContents.executeJavaScript("document.querySelector('input[type=search]')?.value==='new query after renderer recovery'"));
      assert.equal(dispatched, 2); assert.equal(runtime.owners.filter(alive).length, 1);
    });
    await measured('lost_mutation_reply_no_retry', async () => {
      const before = await data(service, 'settings_get');
      const candidate = structuredClone(before.settings) as JsonObject;
      assert.ok(plainObject(candidate.ui)); candidate.ui.theme = 'dark';
      const broker = runtime.brokers.find(alive); assert.ok(broker); broker.stdout.pause();
      const operation = { id: randomUUID(), before: runtime.wire.snapshot() };
      mutationProof.value = operation;
      const pending = call(service, 'settings_apply', { candidate, expected_revision: before.revision! }, operation.id);
      broker.stdout.destroy(); broker.kill();
      assert.equal((await pending).error?.details.outcome, 'unknown');
      await until(() => service.authority !== null); await connection(service);
      const after = await data(service, 'settings_get');
      assert.ok(after.revision === before.revision || after.revision === (before.revision as number) + 1);
      runtime.wire.requireSingleMutation('settings_apply', operation.id, operation.before);
      assert.equal(runtime.owners.filter(alive).length, 1);
    });
    await measured('idle_owner_crash_recovery', async () => {
      const prior = await connection(service);
      const owned = runtime.owners.find(child => child.pid === prior.connection.process.pid); assert.ok(owned);
      owned.kill(); await until(() => !alive(owned));
      await until(() => service.authority !== null && service.authority.generation !== prior.connection.generation, 120_000);
      const next = await connection(service); assert.notEqual(next.connection.generation, prior.connection.generation);
      assert.equal(runtime.owners.filter(alive).length, 1);
    });
    await measured('close_is_not_quit', async () => {
      view.close(); assert.equal(view.isDestroyed(), false); assert.equal(view.isVisible(), false);
      assert.equal((await call(service, 'status')).error, null);
    });
    await measured('parent_orphan_graceful_stop_and_restart', async () => {
      const orphan = startUiHelper(paths.profile, report.source_commit, 'orphan', profile!); helpers.push(orphan.child);
      const ready = await orphan.reply; assert.equal(ready.kind, 'orphan_ready');
      assert.ok(publicUuid(ready.generation)); assert.ok(Number.isInteger(ready.owner_pid) && ready.owner_pid !== orphan.child.pid);
      assert.equal(ready.owner_starts, 1); assert.ok(Number.isInteger(ready.broker_starts) && (ready.broker_starts as number) >= 1);
      assert.ok(Number.isInteger(ready.profile_invocations) && (ready.profile_invocations as number) >= 1);
      helperProfileInvocations += ready.profile_invocations as number; seen.add(ready.owner_pid as number);
      const endpoint = join(orphan.helperRoot, 'engine/runtime/endpoint.json');
      await until(() => !existsSync(endpoint), 60_000);
      const state = JSON.parse(readFileSync(join(orphan.helperRoot, 'engine/runtime/service-state.json'), 'utf8'));
      assert.equal(state.state, 'stopped'); assert.equal(state.generation, ready.generation);
      const saved = profile; profile = runtime.profileIdentitySync(orphan.helperRoot);
      try {
        const restarted = start(); const next = await connection(restarted);
        assert.notEqual(next.connection.generation, ready.generation); assert.equal(next.connection.owner, true);
        await restarted.releaseService();
      } finally { profile = saved; }
      await connection(service);
    });
  }
}
