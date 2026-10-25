import type { ChildProcess } from 'node:child_process';
import type { JsonObject, UiMethod, WireEnvelope } from './contracts.js';
import type { ServicePort } from './ipc.js';
import { failure } from './validation.js';
import { ServiceBridgeClient, type ServiceConnection } from './serviceClient.js';
import { SourceRuntime, type ProfileIdentity } from './sourceRuntime.js';

function exited(child: ChildProcess): boolean { return child.exitCode !== null || child.signalCode !== null; }
function waitExit(child: ChildProcess, milliseconds: number, clean = false): Promise<void> {
  if (exited(child)) return clean && child.exitCode !== 0
    ? Promise.reject(new Error('source_shutdown_incomplete')) : Promise.resolve();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('source_shutdown_incomplete')), milliseconds);
    child.once('close', code => {
      clearTimeout(timer);
      if (clean && code !== 0) reject(new Error('source_shutdown_incomplete'));
      else resolve();
    });
  });
}

/** Shared MAIN session; windows never own a model, owner or independent broker. */
export class ServiceLifecycle implements ServicePort {
  private bridge: ServiceBridgeClient | null = null;
  private opening: Promise<{ mode: 'attached' | 'owned'; connection: ServiceConnection }> | null = null;
  private ownerChild: ChildProcess | null = null;
  private connecting: ServiceBridgeClient | null = null;
  private ready: { mode: 'attached' | 'owned'; connection: ServiceConnection } | null = null;
  private ownedGeneration: string | null = null;
  private stopped = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectAttempts = 0;
  get authority(): ServicePort['authority'] { return this.bridge?.authority ?? null; }
  constructor(readonly runtime: SourceRuntime, readonly profile: ProfileIdentity) {}

  ensureService(): Promise<{ mode: 'attached' | 'owned'; connection: ServiceConnection }> {
    if (this.stopped) return Promise.reject(new Error('service_stopping'));
    if (this.authority && this.ready) return Promise.resolve(this.ready);
    if (this.opening) return this.opening;
    this.opening = this.open().finally(() => { this.opening = null; });
    return this.opening;
  }

  private async open(): Promise<{ mode: 'attached' | 'owned'; connection: ServiceConnection }> {
    const deadline = Date.now() + 120_000;
    let attemptedStart = false;
    while (!this.stopped && Date.now() < deadline) {
      // Keep this exact retained child even if its exit callback clears ownerChild during handshake.
      const handshakeOwner = this.ownerChild;
      const bridge = new ServiceBridgeClient(this.runtime);
      this.connecting = bridge;
      try {
        const connection = await bridge.connect(this.profile);
        if (this.stopped) { await bridge.close(); throw new Error('service_stopping'); }
        if (!bridge.authority || (handshakeOwner && exited(handshakeOwner)
          && connection.process.pid === handshakeOwner.pid)) throw new Error('service_unavailable');
        if (this.ownerChild && !exited(this.ownerChild) && connection.process.pid !== this.ownerChild.pid) {
          // Our concurrent source entry attached; closing its pipe cannot stop the discovered owner.
          this.ownerChild.stdin?.end();
          await waitExit(this.ownerChild, 50_000);
          this.ownerChild = null;
        }
        // Closing an attached startup helper above awaited another event-loop turn.
        if (!bridge.authority || (handshakeOwner && exited(handshakeOwner)
          && connection.process.pid === handshakeOwner.pid)) throw new Error('service_unavailable');
        const owned = this.ownerChild !== null && !exited(this.ownerChild) && connection.process.pid === this.ownerChild.pid;
        this.ownedGeneration = owned ? connection.generation : null;
        this.bridge = bridge;
        this.connecting = null;
        bridge.onDisconnect(() => {
          if (this.bridge !== bridge) return;
          this.bridge = null;
          this.ready = null;
          void bridge.close();
          this.reconnect();
        });
        this.reconnectAttempts = 0;
        this.ready = { mode: owned ? 'owned' : 'attached', connection };
        return this.ready;
      } catch {
        await bridge.close();
        if (this.stopped) break;
        if (this.ownerChild && exited(this.ownerChild)) this.ownerChild = null;
        if (!this.ownerChild && !attemptedStart) {
          attemptedStart = true;
          const child = this.runtime.spawnOwner(this.profile);
          this.ownerChild = child;
          const ended = () => {
            if (this.ownerChild !== child) return;
            this.ownerChild = null;
            if (this.ready?.mode === 'owned' && this.ready.connection.process.pid === child.pid
              && this.ready.connection.generation === this.ownedGeneration) {
              const obsolete = this.bridge;
              this.bridge = null;
              this.ready = null;
              this.ownedGeneration = null;
              void obsolete?.close();
            }
            if (!this.stopped) this.reconnect();
          };
          child.once('error', ended);
          child.once('exit', ended);
        } else if (!this.ownerChild && attemptedStart) break;
        await new Promise(resolve => setTimeout(resolve, 250));
      }
    }
    throw new Error('service_unavailable');
  }

  private reconnect(): void {
    if (this.stopped || this.opening || this.reconnectTimer || this.reconnectAttempts >= 3) return;
    const delay = [500, 1500, 5000][this.reconnectAttempts++];
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.ensureService().catch(() => this.reconnect());
    }, delay);
  }

  /** MAIN-only readiness hook for a new validated read, never a mutation replay. */
  async ensureReadConnection(): Promise<void> {
    await this.ensureService();
  }

  request(method: UiMethod, args: JsonObject, id: string, signal: AbortSignal): Promise<WireEnvelope<unknown>> {
    // A failed operation is never repeated across connection generations.
    return this.bridge?.request(method, args, id, signal) ?? Promise.resolve(failure(id, 'service_unavailable'));
  }

  async releaseService(): Promise<void> {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    const closeBroker = this.bridge?.close();
    const closeConnecting = this.connecting?.close();
    const child = this.ownerChild;
    // This is the retained private control handle from our own spawn, not a discovered PID.
    // Successful ready proof bound its actual owner PID/generation before an owned lease existed.
    if (child && this.ownedGeneration && !exited(child)) child.stdin?.end();
    // A startup/concurrent-attach helper is also ours; EOF cancels only that source entry.
    else if (child && !exited(child)) child.stdin?.end();
    await Promise.all([closeBroker, closeConnecting, this.opening?.catch(() => {}),
      ...(child ? [waitExit(child, 50_000, this.ownedGeneration !== null)] : [])]);
    this.bridge = null;
    this.ownerChild = null;
    this.connecting = null;
    this.ready = null;
  }
}
