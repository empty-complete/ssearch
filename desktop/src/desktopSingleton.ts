import type { App } from 'electron';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

/** Call before ready, using only the trusted canonical user/profile key. */
export function acquireDesktopSingleton(app: App, key: string, activate: () => void): boolean {
  if (!/^[0-9a-f]{64}$/.test(key) || app.isReady()) throw new Error('singleton_identity_required_before_ready');
  const userData = join(app.getPath('appData'), 'ssearch-desktop', key);
  mkdirSync(userData, { recursive: true });
  app.setPath('userData', userData);
  const primary = app.requestSingleInstanceLock();
  if (primary) app.on('second-instance', activate);
  return primary;
}
