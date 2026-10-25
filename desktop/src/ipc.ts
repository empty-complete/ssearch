import { randomUUID } from 'node:crypto';
import type { IpcMain, IpcMainInvokeEvent, Session, WebContents, WebFrameMain } from 'electron';
import type { JsonObject, UiMethod, WireEnvelope } from './contracts.js';
import { exactKeys, failure, methods, mutations, plainObject, publicUuid, validResponse, validateArguments } from './validation.js';
import { isRendererUrl } from './windowPolicy.js';

/** Supplied by the verified MAIN-side broker adapter, never by the renderer. */
export interface ServicePort {
  authority: { generation: string; owner: boolean; capabilities: ReadonlySet<UiMethod> } | null;
  ensureReadConnection?(): Promise<void>;
  request(method: UiMethod, args: JsonObject, id: string, signal: AbortSignal): Promise<WireEnvelope<unknown>>;
}

export const unavailableServicePort: ServicePort = {
  authority: null,
  async request(_method, _args, id) { return failure(id, 'service_unavailable'); },
};

type Context = { session_id: string; renderer_generation: string };
type Schedule = (callback: () => void, milliseconds: number) => () => void;
const scheduleTimeout: Schedule = (callback, milliseconds) => {
  const timer = setTimeout(callback, milliseconds);
  return () => clearTimeout(timer);
};

export function operationBudget(method: UiMethod): number {
  return method === 'settings_apply' ? 35_000 : 15_000;
}

/** One binding per actual webContents/session. No owner start, discovery or transport. */
export class RendererBoundary {
  private context: Context = this.newContext();
  private frame: WebFrameMain | null = null;
  private closed = false;
  private pending = new Map<string, { method: UiMethod; finish: (response: WireEnvelope<unknown>) => void }>();

  constructor(
    private contents: WebContents,
    private isolated: Session,
    private port: ServicePort = unavailableServicePort,
    private schedule: Schedule = scheduleTimeout,
  ) {}

  private newContext(): Context {
    return { session_id: randomUUID(), renderer_generation: randomUUID() };
  }

  private senderAllowed(event: Pick<IpcMainInvokeEvent, 'sender' | 'senderFrame'>): boolean {
    return !this.closed && !this.contents.isDestroyed() && event.sender === this.contents
      && this.contents.session === this.isolated && event.senderFrame !== null
      && event.senderFrame === this.contents.mainFrame && isRendererUrl(event.senderFrame.url);
  }

  handshake(event: Pick<IpcMainInvokeEvent, 'sender' | 'senderFrame'>): Context | null {
    if (!this.senderAllowed(event)) return null;
    if (this.frame && this.frame !== event.senderFrame) this.rollover();
    this.frame = event.senderFrame;
    return { ...this.context };
  }

  rollover(): void {
    for (const [id, pending] of this.pending) pending.finish(failure(id, 'stale_session',
      mutations.has(pending.method) ? { outcome: 'unknown' } : {}));
    this.context = this.newContext();
    this.frame = null;
  }

  close(): void {
    this.closed = true;
    this.rollover();
  }

  async invoke(event: Pick<IpcMainInvokeEvent, 'sender' | 'senderFrame'>, request: unknown): Promise<WireEnvelope<unknown>> {
    const id = plainObject(request) && publicUuid(request.request_id) ? request.request_id : randomUUID();
    if (!this.senderAllowed(event) || this.frame !== event.senderFrame || !plainObject(request)
      || request.session_id !== this.context.session_id
      || request.renderer_generation !== this.context.renderer_generation) return failure(id, 'access_denied');
    if (!exactKeys(request, ['session_id', 'renderer_generation', 'request_id', 'method', 'arguments'], ['cancel'])
      || !publicUuid(request.request_id) || (Object.hasOwn(request, 'cancel') && request.cancel !== true)) {
      return failure(id, 'invalid_request');
    }
    let method = request.method;
    try {
      validateArguments(method, request.arguments);
    } catch (error) {
      const code = error instanceof Error && ['route_not_found', 'request_too_large'].includes(error.message)
        ? error.message : 'invalid_request';
      return failure(id, code);
    }
    if (request.cancel === true) {
      const pending = this.pending.get(id);
      if (pending?.method === method) pending.finish(failure(id, 'request_cancelled',
        mutations.has(method) ? { outcome: 'unknown' } : {}));
      return failure(id, 'request_cancelled');
    }
    const authority = this.port.authority;
    if (mutations.has(method) && (!authority?.owner || !authority.capabilities.has(method))) {
      return failure(id, 'access_denied');
    }
    if (!authority && !this.port.ensureReadConnection) return failure(id, 'service_unavailable');
    if (this.pending.has(id)) return failure(id, 'invalid_request');
    if (this.pending.size >= 64) return failure(id, 'too_many_requests');
    const context = this.context;
    return new Promise(resolve => {
      const abort = new AbortController();
      let settled = false;
      let stopTimer = () => {};
      const finish = (response: WireEnvelope<unknown>) => {
        if (settled) return;
        settled = true;
        stopTimer();
        this.pending.delete(id);
        abort.abort();
        resolve(response);
      };
      this.pending.set(id, { method, finish });
      stopTimer = this.schedule(() => finish(failure(id, 'request_timeout',
        mutations.has(method) ? { outcome: 'unknown' } : {})), operationBudget(method));
      const dispatch = () => {
        if (settled) return;
        if (context !== this.context || !this.senderAllowed(event) || this.frame !== event.senderFrame) {
          finish(failure(id, 'stale_session'));
          return;
        }
        const connected = this.port.authority;
        if (!connected || !publicUuid(connected.generation)) {
          finish(failure(id, 'service_unavailable'));
          return;
        }
        const generation = connected.generation;
        let result: Promise<WireEnvelope<unknown>>;
        try {
          result = this.port.request(method, request.arguments as JsonObject, id, abort.signal);
        } catch {
          finish(failure(id, 'service_unavailable', mutations.has(method) ? { outcome: 'unknown' } : {}));
          return;
        }
        void result.then(response => {
          if (settled) return;
          if (context !== this.context || this.port.authority !== connected
            || this.port.authority.generation !== generation) {
            finish(failure(id, 'stale_session', mutations.has(method) ? { outcome: 'unknown' } : {}));
          } else if (!validResponse(response, id)) {
            finish(failure(id, 'invalid_service_response', mutations.has(method) ? { outcome: 'unknown' } : {}));
          } else if (response.error) {
            // A transport's raw message can contain secrets. Keep public code/details.
            finish({ ...response, error: { ...response.error, message: response.error.code } });
          } else {
            finish(response);
          }
        }).catch(() => finish(failure(id, 'service_unavailable',
          mutations.has(method) ? { outcome: 'unknown' } : {})));
      };
      if (!authority && !mutations.has(method)) {
        void Promise.resolve().then(() => settled ? undefined : this.port.ensureReadConnection!())
          .then(dispatch).catch(() => finish(failure(id, 'service_unavailable')));
      } else {
        dispatch();
      }
    });
  }
}

/** Fixed channels are private to preload. Unknown channels have no handler. */
export function registerDesktopIpc(ipc: IpcMain): {
  bind(contents: WebContents, isolated: Session, port?: ServicePort): RendererBoundary;
} {
  const bindings = new Map<WebContents, RendererBoundary>();
  ipc.handle('ssearch:handshake', event => bindings.get(event.sender)?.handshake(event) ?? null);
  for (const method of methods) {
    ipc.handle(`ssearch:${method}`, (event, request: unknown) => {
      const boundary = bindings.get(event.sender);
      if (!boundary || !plainObject(request) || request.method !== method) {
        return failure(plainObject(request) && publicUuid(request.request_id) ? request.request_id : randomUUID(), 'access_denied');
      }
      return boundary.invoke(event, request);
    });
  }
  return {
    bind(contents, isolated, port) {
      if (bindings.has(contents)) throw new Error('renderer_already_bound');
      const boundary = new RendererBoundary(contents, isolated, port);
      bindings.set(contents, boundary);
      // Commit events exclude navigation attempts refused by the host policy.
      contents.on('did-navigate', () => boundary.rollover());
      contents.on('render-process-gone', () => boundary.rollover());
      contents.once('destroyed', () => {
        boundary.close();
        bindings.delete(contents);
      });
      return boundary;
    },
  };
}
