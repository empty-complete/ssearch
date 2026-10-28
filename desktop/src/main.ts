import { app, BrowserWindow, ipcMain, Tray, Menu } from 'electron';
import { dirname, join, resolve } from 'node:path';
import { realpathSync } from 'node:fs';
import { createWindow } from './windowHost.js';
import { registerAssetScheme } from './assetProtocol.js';
import { registerDesktopIpc } from './ipc.js';
import { SourceRuntime } from './sourceRuntime.js';
import { PackagedRuntime } from './packagedRuntime.js';
import type { ServiceRuntime } from './runtime.js';
import { ServiceLifecycle } from './serviceLifecycle.js';
import { installWindowLifecycle } from './windowLifecycle.js';
import { acquireDesktopSingleton } from './desktopSingleton.js';
import { parseUiProofArguments, type UiProofRequest } from './diagnosticsPolicy.js';
import { runUiProof } from './diagnostics.js';

app.enableSandbox();
registerAssetScheme();
let proof: UiProofRequest | null = null;
let argumentsOkay = true;
try { proof = parseUiProofArguments(process.argv.slice(1)); }
catch { argumentsOkay = false; app.exit(2); }
if (argumentsOkay) {
  if (proof) void runUiProof(proof).then(code => app.exit(code)).catch(() => {
    console.error('ui_proof_bootstrap_failed');
    app.exit(1);
  });
  else startApplication();
}

function startApplication(): void {
  let service: ServiceLifecycle | null = null;
  let quitting = false;
  let released = false;
  let tray: Tray | null = null;
  let bootstrapOkay = true;
  let profileKey = '';
  const repository = resolve(__dirname, '../..');
  try {
    const runtime: ServiceRuntime = app.isPackaged
      ? new PackagedRuntime({ installationRoot: dirname(realpathSync(process.execPath)),
        expectedServiceVersion: app.getVersion() })
      : new SourceRuntime({ repositoryRoot: repository,
        python: join(repository, '.venv/Scripts/python.exe'), modelDirectory: join(repository, 'models') });
    const selector = process.argv.indexOf('--data-dir');
    if (selector >= 0 && (!process.argv[selector + 1] || process.argv[selector + 1].startsWith('--'))) {
      throw new Error('profile_selector_required');
    }
    const profile = runtime.profileIdentitySync(selector >= 0 ? process.argv[selector + 1] : undefined);
    profileKey = profile.key;
    service = new ServiceLifecycle(runtime, profile);
  } catch {
    bootstrapOkay = false;
    console.error('service_bootstrap_unavailable');
    app.exit(1);
  }
  const activate = () => {
    const window = BrowserWindow.getAllWindows()[0];
    if (window) { if (window.isMinimized()) window.restore(); window.show(); window.focus(); }
  };
  const primary = bootstrapOkay && acquireDesktopSingleton(app, profileKey, activate);
  if (bootstrapOkay && !primary) app.exit(0);
  if (primary) void app.whenReady().then(() => {
    const bridge = registerDesktopIpc(ipcMain);
    const open = () => createWindow({
      mode: 'application', assetRoot: app.isPackaged ? join(app.getAppPath(), 'renderer') : join(repository, 'frontend/dist'),
      bridge, servicePort: service ?? undefined,
    });
    const window = open();
    installWindowLifecycle(window, () => quitting);
    window.on('session-end', () => { void service?.releaseService(); });
    tray = new Tray(app.isPackaged ? join(app.getAppPath(), 'assets/ssearch.ico')
      : join(repository, 'desktop/assets/ssearch.ico'));
    tray.setToolTip('ssearch — Simple Search');
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: 'Показать ssearch', click: activate },
      { label: 'Выход', click: () => app.quit() },
    ]));
    tray.on('double-click', activate);
    void service?.ensureService().catch(() => console.error('service_unavailable'));
    app.on('activate', () => {
      activate();
    });
  });
  app.on('before-quit', event => {
    quitting = true;
    if (released) return;
    event.preventDefault();
    void (async () => {
      let code = 0;
      try { await service?.releaseService(); } catch { code = 1; }
      released = true;
      tray?.destroy();
      app.exit(code);
    })();
  });
  process.on('SIGTERM', () => app.quit());
  process.on('SIGINT', () => app.quit());
}
