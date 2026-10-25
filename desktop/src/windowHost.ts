import { BrowserWindow, session, screen } from 'electron';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { permitsAsset } from './windowPolicy.js';

interface WindowOptions {
  mode: 'application' | 'bootstrapper';
  assetRoot: string;
}

const contentSecurityPolicy = [
  "default-src 'self'", "script-src 'self'", "style-src 'self' 'unsafe-inline'",
  "img-src 'self'", "font-src 'self'", "connect-src 'none'", "object-src 'none'",
  "frame-src 'none'", "base-uri 'none'", "form-action 'none'",
].join('; ');

export function createWindow({ mode, assetRoot }: WindowOptions): BrowserWindow {
  const isolated = session.fromPartition(`ssearch-preview-${randomUUID()}`);
  isolated.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  isolated.setPermissionCheckHandler(() => false);
  isolated.on('will-download', event => event.preventDefault());
  isolated.webRequest.onBeforeRequest((details, callback) => callback({
    cancel: !permitsAsset(details.url, assetRoot) || details.resourceType === 'subFrame',
  }));
  isolated.webRequest.onHeadersReceived((details, callback) => callback({
    responseHeaders: {
      ...details.responseHeaders,
      'Content-Security-Policy': [contentSecurityPolicy],
    },
  }));

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
    },
  });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.webContents.on('will-frame-navigate', event => event.preventDefault());
  window.webContents.on('will-attach-webview', event => event.preventDefault());
  window.once('ready-to-show', () => window.show());
  void window.loadFile(join(assetRoot, 'index.html')).catch(() => window.destroy());
  return window;
}
