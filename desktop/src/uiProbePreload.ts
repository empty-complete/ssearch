// Adversarial self-test fixture only; never selected by the normal product window.
import { ipcRenderer } from 'electron';

void (async () => {
  const context = await ipcRenderer.invoke('ssearch-proof:context');
  const request = { ...context, request_id: crypto.randomUUID(), method: 'status', arguments: {} };
  const valid = await ipcRenderer.invoke('ssearch:status', request);
  const forged = await ipcRenderer.invoke('ssearch:status', { ...request, session_id: crypto.randomUUID() });
  const oversized = await ipcRenderer.invoke('ssearch:settings_apply', {
    ...request, request_id: crypto.randomUUID(), method: 'settings_apply',
    arguments: { expected_revision: 1, candidate: { value: 'x'.repeat(65536) } },
  });
  const wrongChannel = await ipcRenderer.invoke('ssearch:search', request);
  let unknownDenied = false;
  try { await ipcRenderer.invoke('ssearch:not_allowed', request); } catch { unknownDenied = true; }
  await ipcRenderer.invoke('ssearch-proof:result', { valid, forged, oversized, wrongChannel, unknownDenied });
})().catch(() => ipcRenderer.invoke('ssearch-proof:result', { fixtureFailure: true }));
