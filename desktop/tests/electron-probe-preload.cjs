// Adversarial fixture only: excluded from the product by Forge's tests ignore.
const { ipcRenderer } = require('electron');

void (async () => {
  const context = await ipcRenderer.invoke('security:context');
  const request = {
    ...context, request_id: globalThis.crypto.randomUUID(), method: 'status', arguments: {},
  };
  const valid = await ipcRenderer.invoke('ssearch:status', request);
  const forged = await ipcRenderer.invoke('ssearch:status', {
    ...request, session_id: globalThis.crypto.randomUUID(),
  });
  const oversized = await ipcRenderer.invoke('ssearch:settings_apply', {
    ...request, request_id: globalThis.crypto.randomUUID(), method: 'settings_apply',
    arguments: { expected_revision: 1, candidate: { value: 'x'.repeat(65_536) } },
  });
  const wrongChannel = await ipcRenderer.invoke('ssearch:search', request);
  let unknownDenied = false;
  try { await ipcRenderer.invoke('ssearch:not_allowed', request); } catch { unknownDenied = true; }
  await ipcRenderer.invoke('security:result', { valid, forged, oversized, wrongChannel, unknownDenied });
})().catch(() => ipcRenderer.invoke('security:result', { fixtureFailure: true }));
