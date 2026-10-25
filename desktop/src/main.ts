import { app, BrowserWindow, ipcMain, Tray, Menu } from 'electron';
import { join, resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { createWindow } from './windowHost.js';
import { registerAssetScheme } from './assetProtocol.js';
import { registerDesktopIpc } from './ipc.js';
import { SourceRuntime } from './sourceRuntime.js';
import { ServiceLifecycle } from './serviceLifecycle.js';
import { installWindowLifecycle } from './windowLifecycle.js';
import { acquireDesktopSingleton } from './desktopSingleton.js';

app.enableSandbox();
registerAssetScheme();
let service: ServiceLifecycle | null = null;
let quitting = false;
let released = false;
let tray: Tray | null = null;
let bootstrapOkay = true;
let profileKey = createHash('sha256').update('ssearch-packaged-candidate-unavailable').digest('hex');
const repository = resolve(__dirname, '../..');
if (!app.isPackaged) {
  try {
    const runtime = new SourceRuntime({ repositoryRoot: repository,
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
    console.error('source_service_bootstrap_unavailable');
    app.exit(1);
  }
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
  void service?.ensureService().catch(() => console.error('source_service_unavailable'));
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
