import { contextBridge, ipcRenderer } from 'electron';
import type { CallControl, DesktopApi, UiMethod, WireEnvelope } from './contracts.js';

// tsc emits a self-contained CommonJS file: the only runtime require is electron.
// Sandboxed preloads cannot follow arbitrary local require/import chains.
const context = ipcRenderer.invoke('ssearch:handshake').catch(() => null) as Promise<{
  session_id: string; renderer_generation: string;
} | null>;

async function invoke(method: UiMethod, args: unknown, control: CallControl): Promise<WireEnvelope<never>> {
  const id = typeof control?.request_id === 'string' ? control.request_id : '00000000-0000-0000-0000-000000000000';
  const failed = (code: string, uncertain = false): WireEnvelope<never> => ({
    schema_version: 1, request_id: id, data: null,
    error: {
      code, message: code, retryable: code === 'service_unavailable',
      details: uncertain ? { outcome: 'unknown' } : {},
    },
  });
  if (!control || typeof control !== 'object'
    || Object.keys(control).some(key => key !== 'request_id' && key !== 'cancel')
    || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id)
    || ('cancel' in control && control.cancel !== true)) return failed('invalid_request');
  const bound = await context;
  if (!bound) return failed('access_denied');
  try {
    return await ipcRenderer.invoke(`ssearch:${method}`, {
      session_id: bound.session_id, renderer_generation: bound.renderer_generation,
      request_id: id, method, arguments: args, ...(control.cancel === true ? { cancel: true } : {}),
    }) as WireEnvelope<never>;
  } catch {
    return failed('service_unavailable', [
      'settings_validate', 'settings_apply', 'start_catalog', 'cancel_job',
    ].includes(method));
  }
}

const api: DesktopApi = {
  search: (args, control) => invoke('search', args, control),
  fetch: (args, control) => invoke('fetch', args, control),
  status: (args, control) => invoke('status', args, control),
  settingsGet: (args, control) => invoke('settings_get', args, control),
  settingsValidate: (args, control) => invoke('settings_validate', args, control),
  settingsApply: (args, control) => invoke('settings_apply', args, control),
  startCatalog: (args, control) => invoke('start_catalog', args, control),
  cancelJob: (args, control) => invoke('cancel_job', args, control),
};
contextBridge.exposeInMainWorld('ssearch', api);
