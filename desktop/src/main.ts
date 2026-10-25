import { app, BrowserWindow } from 'electron';
import { join } from 'node:path';
import { createWindow } from './windowHost.js';

// Candidate shell only: no service/model/database ownership or update lifecycle.
app.enableSandbox();
void app.whenReady().then(() => {
  createWindow({ mode: 'application', assetRoot: join(app.getAppPath(), 'renderer') });
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow({ mode: 'application', assetRoot: join(app.getAppPath(), 'renderer') }); });
});
app.on('window-all-closed', () => app.quit());
