import { app, BrowserWindow } from 'electron';
import { join } from 'node:path';
import { createWindow } from './windowHost.js';

// Candidate shell only: no service/model/database ownership or update lifecycle.
app.enableSandbox();
void app.whenReady().then(() => {
  const open = () => createWindow({
    mode: 'application', assetRoot: join(app.getAppPath(), 'renderer'),
  });
  open();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) open();
  });
});
app.on('window-all-closed', () => app.quit());
