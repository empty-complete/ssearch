import { app, BrowserWindow, ipcMain } from 'electron';
import { join } from 'node:path';
import { createWindow } from './windowHost.js';
import { registerAssetScheme } from './assetProtocol.js';
import { registerDesktopIpc } from './ipc.js';

// Candidate shell only: no service/model/database ownership or update lifecycle.
app.enableSandbox();
registerAssetScheme();
void app.whenReady().then(() => {
  const bridge = registerDesktopIpc(ipcMain);
  const open = () => createWindow({
    mode: 'application', assetRoot: join(app.getAppPath(), 'renderer'), bridge,
  });
  open();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) open();
  });
});
app.on('window-all-closed', () => app.quit());
