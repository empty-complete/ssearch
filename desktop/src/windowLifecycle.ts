import type { BrowserWindow } from 'electron';

/** Closing a view keeps the shared service session; explicit quit releases it. */
export function installWindowLifecycle(window: BrowserWindow, quitting: () => boolean): void {
  let recoveries = 0;
  window.on('close', event => {
    if (quitting()) return;
    event.preventDefault();
    window.hide();
  });
  window.webContents.on('render-process-gone', () => {
    if (!quitting() && !window.isDestroyed() && recoveries++ < 3) window.webContents.reload();
  });
}
