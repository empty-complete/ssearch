// GitHub-only Electron source proof. All windows stay hidden/offscreen.
// Synthetic service ports do not establish broker, ownership or packaged gates.
const assert = require('node:assert/strict');
const { once } = require('node:events');
const { mkdtempSync, readFileSync, writeFileSync, mkdirSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join, resolve } = require('node:path');
const { randomUUID, createHash } = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { createServer } = require('node:http');
const { app, BrowserWindow, ipcMain, session } = require('electron');
const { registerAssetScheme } = require('../dist/assetProtocol.js');
const { createWindow } = require('../dist/windowHost.js');
const { registerDesktopIpc } = require('../dist/ipc.js');

app.enableSandbox();
registerAssetScheme();
const deadline = setTimeout(() => {
  console.error('Electron security proof timed out');
  app.exit(1);
}, 60_000);
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function waitFor(check) {
  for (let i = 0; i < 100; i++) {
    const result = await check();
    if (result) return result;
    await pause(25);
  }
  throw new Error('Expected Electron evidence did not arrive');
}

void app.whenReady().then(async () => {
  const root = mkdtempSync(join(tmpdir(), 'ssearch-electron-'));
  let server;
  try {
    assert.equal(process.versions.electron, '45.0.0');
    const bridge = registerDesktopIpc(ipcMain);
    const product = createWindow({
      mode: 'application', assetRoot: resolve(__dirname, '../../frontend/dist'), bridge, show: false,
    });
    await once(product.webContents, 'did-finish-load');
    assert.equal(product.isVisible(), false);
    assert.equal(product.webContents.getURL(), 'ssearch://ui/');
    await waitFor(() => product.webContents.executeJavaScript(`!!document.querySelector('input[type="search"]')`));
    await product.webContents.executeJavaScript(`{
      const input=document.querySelector('input[type="search"]');
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'source protocol probe');
      input.dispatchEvent(new Event('input',{bubbles:true}));
    }`);
    await waitFor(() => product.webContents.executeJavaScript(`
      Array.from(document.querySelectorAll('button')).some(button=>button.textContent==='Найти' && !button.disabled)
    `));
    await product.webContents.executeJavaScript(`document.querySelector('input[type="search"]').dispatchEvent(
      new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true,cancelable:true})
    )`);
    await waitFor(() => product.webContents.executeJavaScript(`
      document.querySelector('[role="alert"]')?.textContent.includes('Локальный поиск недоступен')
        && document.querySelector('input[type="search"]').value==='source protocol probe'
    `));
    const exposure = await product.webContents.executeJavaScript(`(async () => ({
      node: [typeof require, typeof process, typeof Buffer, typeof ipcRenderer],
      keys: Object.keys(window.ssearch).sort(), serialized: JSON.stringify(window.ssearch),
      reply: await window.ssearch.status({}, {request_id: crypto.randomUUID()})
    }))()`);
    assert.deepEqual(exposure.node, ['undefined', 'undefined', 'undefined', 'undefined']);
    assert.deepEqual(exposure.keys, [
      'cancelJob', 'fetch', 'search', 'settingsApply', 'settingsGet', 'settingsValidate', 'startCatalog', 'status',
    ]);
    assert.equal(exposure.serialized, '{}');
    assert.equal(exposure.reply.error.code, 'service_unavailable');

    // The policy is tested in Chromium, not merely by comparing its CSP text.
    const assets = join(root, 'assets');
    mkdirSync(assets);
    writeFileSync(join(assets, 'index.html'), `<!doctype html><html><head>
      <script>globalThis.inlineRan=true</script><script src="probe.js"></script>
      </head><body>synthetic source security fixture</body></html>`);
    writeFileSync(join(assets, 'probe.js'), `
      globalThis.probeReady=true;
      globalThis.violations=[];
      addEventListener('securitypolicyviolation',e=>violations.push(e.effectiveDirective));
      try { eval('globalThis.evalRan=true'); } catch {}
      globalThis.runDeniedLoads=async function(url) {
        const script=document.createElement('script'); script.src=url+'/script.js'; document.head.append(script);
        const image=document.createElement('img'); image.src=url+'/image.png'; document.body.append(image);
        const frame=document.createElement('iframe'); frame.src=url+'/frame'; document.body.append(frame);
        try { await fetch(url+'/fetch'); } catch {}
      };
    `);
    writeFileSync(join(assets, 'LICENSE.txt'), 'synthetic download fixture');
    const window = createWindow({ mode: 'application', assetRoot: assets, bridge, show: false });
    await once(window.webContents, 'did-finish-load');
    assert.equal(await window.webContents.executeJavaScript('globalThis.probeReady'), true);
    assert.equal(await window.webContents.executeJavaScript('globalThis.inlineRan===undefined && globalThis.evalRan===undefined'), true);
    let networkRequests = 0;
    server = createServer((_request, response) => {
      networkRequests++;
      response.end('must never be reached');
    });
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    const remote = `http://127.0.0.1:${server.address().port}`;
    await window.webContents.executeJavaScript(`runDeniedLoads(${JSON.stringify(remote)})`);
    await pause(100);
    const violations = await window.webContents.executeJavaScript('globalThis.violations');
    for (const directive of ['script-src-elem', 'img-src', 'frame-src', 'connect-src']) {
      assert.equal(violations.includes(directive), true, directive);
    }
    // MAIN uses the bound session to bypass renderer CSP; onBeforeRequest must still refuse it.
    await assert.rejects(window.webContents.session.fetch(`${remote}/interception-proof`));
    assert.equal(networkRequests, 0);
    const before = window.webContents.getURL();
    await window.webContents.executeJavaScript(`location.href=${JSON.stringify(remote)}`);
    await pause(100);
    assert.equal(window.webContents.getURL(), before);
    const count = BrowserWindow.getAllWindows().length;
    await window.webContents.executeJavaScript(`window.open(${JSON.stringify(remote)})`);
    await pause(100);
    assert.equal(BrowserWindow.getAllWindows().length, count);
    assert.equal(await window.webContents.executeJavaScript('Notification.requestPermission()'), 'denied');
    let downloadPrevented = false;
    window.webContents.session.on('will-download', event => { downloadPrevented = event.defaultPrevented; });
    window.webContents.downloadURL('ssearch://ui/LICENSE.txt');
    await waitFor(() => downloadPrevented);

    // Actual Electron IPC events from main frame, foreign window, subframe and wrong origin.
    // This adversarial fixture deliberately enables subframe preload only in its own session.
    const adversarial = session.fromPartition(`security-probe-${randomUUID()}`);
    adversarial.protocol.handle('ssearch', request => {
      if (!['ssearch://ui/', 'ssearch://foreign/'].includes(request.url)) return new Response('', { status: 403 });
      return new Response('<!doctype html><body>synthetic IPC sender fixture</body>', {
        headers: { 'Content-Type': 'text/html' },
      });
    });
    let calls = 0;
    const servicePort = {
      authority: { generation: randomUUID(), owner: true, capabilities: new Set(['settings_apply']) },
      async request(_method, _args, id) {
        calls++;
        return { schema_version: 1, request_id: id, data: { fixture: true }, error: null };
      },
    };
    const probeOptions = {
      show: false,
      webPreferences: {
        session: adversarial, sandbox: true, contextIsolation: true, nodeIntegration: false,
        nodeIntegrationInSubFrames: true, offscreen: true,
        preload: join(__dirname, 'electron-probe-preload.cjs'),
      },
    };
    const authorized = new BrowserWindow(probeOptions);
    const bound = bridge.bind(authorized.webContents, adversarial, servicePort);
    let savedContext;
    const results = [];
    ipcMain.handle('security:context', event => {
      if (event.sender === authorized.webContents && event.senderFrame === event.sender.mainFrame
        && event.senderFrame.url === 'ssearch://ui/') savedContext = bound.handshake(event);
      return savedContext;
    });
    ipcMain.handle('security:result', (event, result) => {
      results.push({ contents: event.sender, frame: event.senderFrame, result });
    });
    await authorized.loadURL('ssearch://ui/');
    const valid = await waitFor(() => results.find(item => item.contents === authorized.webContents
      && item.frame === authorized.webContents.mainFrame));
    assert.equal(valid.result.fixtureFailure, undefined);
    assert.equal(valid.result.valid.error, null);
    assert.equal(valid.result.forged.error.code, 'access_denied');
    assert.equal(valid.result.oversized.error.code, 'request_too_large');
    assert.equal(valid.result.wrongChannel.error.code, 'access_denied');
    assert.equal(valid.result.unknownDenied, true);
    assert.equal(calls, 1);
    await authorized.webContents.executeJavaScript(`{
      const iframe=document.createElement('iframe'); iframe.srcdoc='<p>subframe fixture</p>';
      document.body.append(iframe);
    }`);
    const child = await waitFor(() => results.find(item => item.contents === authorized.webContents
      && item.frame !== authorized.webContents.mainFrame));
    assert.equal(child.result.valid.error.code, 'access_denied');
    const foreign = new BrowserWindow(probeOptions);
    await foreign.loadURL('ssearch://ui/');
    const stranger = await waitFor(() => results.find(item => item.contents === foreign.webContents));
    assert.equal(stranger.result.valid.error.code, 'access_denied');
    results.length = 0;
    await authorized.loadURL('ssearch://foreign/');
    const origin = await waitFor(() => results.find(item => item.contents === authorized.webContents));
    assert.equal(origin.result.valid.error.code, 'access_denied');
    assert.equal(calls, 1);
    // Fault injection AFTER real IPC dispatch exercises the actual sandboxed preload catch.
    // A lost acknowledgement cannot prove a synthetic mutation was rolled back.
    let dispatchedMutations = 0;
    for (const [method, apiName, args] of [
      ['settings_validate', 'settingsValidate', { candidate: {}, expected_revision: 1 }],
      ['settings_apply', 'settingsApply', { candidate: {}, expected_revision: 1 }],
      ['start_catalog', 'startCatalog', { source_ids: [] }],
      ['cancel_job', 'cancelJob', { job_id: randomUUID() }],
    ]) {
      ipcMain.removeHandler(`ssearch:${method}`);
      ipcMain.handle(`ssearch:${method}`, () => {
        dispatchedMutations++;
        throw new Error('synthetic_disconnected_after_dispatch');
      });
      const failure = await product.webContents.executeJavaScript(`window.ssearch[${JSON.stringify(apiName)}](
        ${JSON.stringify(args)}, {request_id: crypto.randomUUID()})`);
      assert.equal(failure.error.code, 'service_unavailable');
      assert.deepEqual(failure.error.details, { outcome: 'unknown' });
      assert.equal(JSON.stringify(failure).includes('synthetic_disconnected'), false);
    }
    assert.equal(dispatchedMutations, 4);
    const evidenceRoot = resolve(__dirname, '../../frontend/test-results');
    mkdirSync(evidenceRoot, { recursive: true });
    writeFileSync(join(evidenceRoot, 'electron-security.json'), JSON.stringify({
      schema_version: 1,
      source_sha: execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
      runtime_versions: process.versions,
      electron_binary_sha256: createHash('sha256').update(readFileSync(process.execPath)).digest('hex'),
      surface: 'ssearch://ui/',
      proof: 'source_only_hidden_offscreen',
      synthetic_service_port: true,
      passed: [
        'sandboxed_self_contained_preload', 'eight_named_methods_no_node_or_secrets',
        'actual_react_search_and_unavailable_flow', 'default_unavailable',
        'inline_and_eval_scripts_denied', 'csp_remote_loads_denied',
        'bound_session_network_interception', 'navigation_and_new_windows_denied',
        'permission_and_download_denied', 'actual_main_frame_allowed',
        'forged_session_oversized_unknown_method_denied',
        'actual_subframe_foreign_window_wrong_origin_denied',
        'actual_preload_post_dispatch_rejection_preserves_unknown_mutation_outcome',
      ],
      open_gates: ['real_broker_source_security', 'owner_authority', 'packaged_frozen', 'native', 'win10'],
    }, null, 2));
    console.log('PASS Electron45: sandboxed preload, CSP execution, request interception, navigation/windows/permissions/downloads, actual sender/frame/origin refusal. Source proof only.');
  } finally {
    for (const window of BrowserWindow.getAllWindows()) window.destroy();
    server?.close();
    rmSync(root, { recursive: true, force: true });
  }
}).then(() => {
  clearTimeout(deadline);
  app.exit(0);
}).catch(error => {
  console.error(error);
  clearTimeout(deadline);
  app.exit(1);
});
