import { BrowserWindow, session, screen } from 'electron';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { installAssetHandler } from './assetProtocol.js';
import type { registerDesktopIpc, ServicePort } from './ipc.js';

interface WindowOptions {
  mode: 'application' | 'bootstrapper';
  assetRoot: string;
  bridge: ReturnType<typeof registerDesktopIpc>;
  servicePort?: ServicePort;
  show?: boolean;
}

export function createWindow({ mode, assetRoot, bridge, servicePort, show = true }: WindowOptions): BrowserWindow {
  const isolated = session.fromPartition(`ssearch-preview-${randomUUID()}`);
  installAssetHandler(isolated, assetRoot);

  const area = screen.getPrimaryDisplay().workAreaSize;
  const window = new BrowserWindow({
    width: Math.min(840, area.width),
    height: Math.min(560, area.height),
    minWidth: Math.min(640, area.width),
    minHeight: Math.min(400, area.height),
    show: false,
    title: mode === 'bootstrapper' ? 'ssearch — подготовка' : 'ssearch — Simple Search',
    autoHideMenuBar: true,
    webPreferences: {
      session: isolated,
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      devTools: false,
      navigateOnDragDrop: false,
      spellcheck: false,
      preload: join(__dirname, 'preload.js'),
      offscreen: !show,
    },
  });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.webContents.on('will-frame-navigate', event => event.preventDefault());
  window.webContents.on('will-redirect', event => event.preventDefault());
  window.webContents.on('will-attach-webview', event => event.preventDefault());
  bridge.bind(window.webContents, isolated, servicePort);
  if (show) window.once('ready-to-show', () => window.show());
  void window.loadURL('ssearch://ui/').catch(() => window.destroy());
  return window;
}
