import type { ChildProcessWithoutNullStreams } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { realpathSync } from 'node:fs';
import { isAbsolute } from 'node:path';
import type { JsonObject, UiMethod, WireEnvelope } from './contracts.js';
import type { ServicePort } from './ipc.js';
import { operationBudget } from './ipc.js';
import { BrokerFrameDecoder, encodeBrokerFrame } from './brokerFrames.js';
import { exactKeys, failure, mutations, plainObject, publicUuid, validResponse, validateArguments } from './validation.js';
import type { ProfileIdentity, SourceRuntime } from './sourceRuntime.js';

export interface ProcessProof { readonly pid: number; readonly executable: string; readonly created: string }
export interface ServiceConnection {
  readonly generation: string;
  readonly installationId: string;
  readonly process: ProcessProof;
  readonly owner: boolean;
}
interface Pending {
  id: string; method: UiMethod; args: JsonObject; settled: boolean;
  finish(reply: WireEnvelope<unknown>): void;
}

function processProof(value: unknown): value is ProcessProof {
  return plainObject(value) && exactKeys(value, ['pid', 'executable', 'created'])
    && Number.isInteger(value.pid) && (value.pid as number) > 0
    && typeof value.executable === 'string' && isAbsolute(value.executable)
    && typeof value.created === 'string' && /^[1-9][0-9]{0,19}$/.test(value.created);
}

/** One trusted child, one serial wire lane. Abort never retries or interrupts a mutation. */
export class ServiceBridgeClient implements ServicePort {
  private child: ChildProcessWithoutNullStreams | null = null;
  private verified: ServicePort['authority'] = null;
  private connection: ServiceConnection | null = null;
  private session = randomUUID();
  private decoder = new BrokerFrameDecoder();
  private pending = new Map<string, Pending>();
  private queue: Pending[] = [];
  private active: Pending | null = null;
  private frameTimer: ReturnType<typeof setTimeout> | null = null;
  private wireTimer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;
  private disconnected = new Set<() => void>();
  get authority(): ServicePort['authority'] { return this.closed ? null : this.verified; }

  constructor(private runtime: Pick<SourceRuntime, 'spawnBroker'>) {}
  onDisconnect(callback: () => void): () => void {
    this.disconnected.add(callback);
    return () => this.disconnected.delete(callback);
  }

  async connect(profile: ProfileIdentity): Promise<ServiceConnection> {
    if (this.child || this.closed) throw new Error('broker_already_connected');
    const child = this.runtime.spawnBroker(profile);
    this.child = child;
    child.stderr.resume();
    const challenge = randomUUID();
    return new Promise((accept, reject) => {
      let handshake = true;
      const deadline = setTimeout(() => fail(), 20_000);
      const fail = () => {
        clearTimeout(deadline);
        if (handshake) reject(new Error('service_unavailable'));
        handshake = false;
        this.invalidate();
      };
      child.on('error', fail);
      child.stdin.on('error', fail);
      child.stdout.on('end', () => {
        try { this.decoder.end(); } catch { /* A partial EOF is a terminal fault. */ }
        fail();
      });
      child.on('close', fail);
      child.stdout.on('data', chunk => {
        if (this.closed) return;
        try {
          const partial = this.decoder.partial;
          const frames = this.decoder.push(chunk);
          if (this.frameTimer && (!this.decoder.partial || frames.length)) clearTimeout(this.frameTimer);
          if (this.decoder.partial && (!partial || frames.length)) this.frameTimer = setTimeout(fail, 10_000);
          for (const value of frames) {
            if (handshake) {
              const fields = ['schema_version', 'kind', 'session_id', 'challenge', 'profile', 'key',
                'api_version', 'service_version', 'app_version', 'generation', 'installation_id',
                'owner', 'capabilities', 'process', 'broker'];
              if (!plainObject(value) || !exactKeys(value, fields) || value.schema_version !== 1
                || value.kind !== 'ready' || value.session_id !== this.session || value.challenge !== challenge
                || value.profile !== profile.profile || value.key !== profile.key || value.api_version !== '1'
                || value.service_version !== profile.serviceVersion || value.app_version !== profile.serviceVersion
                || !publicUuid(value.generation) || !publicUuid(value.installation_id)
                || typeof value.owner !== 'boolean' || !processProof(value.process) || !processProof(value.broker)
                || value.broker.pid !== child.pid
                || realpathSync(value.broker.executable).toLowerCase() !== realpathSync(profile.pythonBase).toLowerCase()
                || !Array.isArray(value.capabilities)
                || JSON.stringify(value.capabilities) !== JSON.stringify(value.owner ? Array.from(mutations) : [])) throw new Error();
              this.verified = Object.freeze({ generation: value.generation, owner: value.owner,
                capabilities: new Set(value.capabilities as UiMethod[]) });
              this.connection = Object.freeze({ generation: value.generation, installationId: value.installation_id,
                process: Object.freeze({ ...value.process }), owner: value.owner });
              clearTimeout(deadline);
              handshake = false;
              accept(this.connection);
            } else {
              if (!plainObject(value) || !exactKeys(value, ['session_id', 'generation', 'envelope'])
                || value.session_id !== this.session || value.generation !== this.verified?.generation
                || !this.active || !validResponse(value.envelope, this.active.id)) throw new Error();
              let reply = value.envelope;
              const active = this.active;
              if (reply.error && mutations.has(active.method) && [
                'service_unavailable', 'request_timeout', 'service_identity_mismatch',
                'authentication_required', 'invalid_service_response',
              ].includes(reply.error.code)) {
                reply = { ...reply, error: { ...reply.error, details: { ...reply.error.details, outcome: 'unknown' } } };
              }
              if (reply.error && ['service_identity_mismatch', 'authentication_required'].includes(reply.error.code)) {
                active.finish(reply);
                fail();
                return;
              }
              if (this.wireTimer) clearTimeout(this.wireTimer);
              this.active = null;
              this.pending.delete(active.id);
              active.finish(reply);
              this.dispatch();
            }
          }
        } catch { fail(); }
      });
      child.stdin.write(encodeBrokerFrame({ schema_version: 1, session_id: this.session, challenge }));
    });
  }

  request(method: UiMethod, args: JsonObject, id: string, signal: AbortSignal): Promise<WireEnvelope<unknown>> {
    try { validateArguments(method, args); } catch { return Promise.resolve(failure(id, 'invalid_request')); }
    if (!publicUuid(id) || this.pending.has(id) || this.active?.id === id) return Promise.resolve(failure(id, 'invalid_request'));
    if (!this.verified || this.closed) return Promise.resolve(failure(id, 'service_unavailable'));
    if (mutations.has(method) && (!this.verified.owner || !this.verified.capabilities.has(method))) {
      return Promise.resolve(failure(id, 'access_denied'));
    }
    if (this.pending.size >= 64) return Promise.resolve(failure(id, 'too_many_requests'));
    if (signal.aborted) return Promise.resolve(failure(id, 'request_cancelled'));
    return new Promise(resolve => {
      const pending: Pending = { id, method, args, settled: false, finish: reply => {
        if (pending.settled) return;
        pending.settled = true;
        signal.removeEventListener('abort', abort);
        resolve(reply);
      } };
      const abort = () => {
        pending.finish(failure(id, 'request_cancelled', mutations.has(method) && this.active === pending ? { outcome: 'unknown' } : {}));
        if (this.active !== pending) {
          this.pending.delete(id);
          this.queue = this.queue.filter(item => item !== pending);
        }
      };
      signal.addEventListener('abort', abort, { once: true });
      this.pending.set(id, pending);
      this.queue.push(pending);
      this.dispatch();
    });
  }

  private dispatch(): void {
    if (this.active || !this.verified || this.closed) return;
    const pending = this.queue.shift();
    if (!pending) return;
    this.active = pending;
    this.wireTimer = setTimeout(() => this.invalidate(), operationBudget(pending.method) + 10_000);
    try {
      this.child!.stdin.write(encodeBrokerFrame({ session_id: this.session, generation: this.verified.generation,
        envelope: { schema_version: 1, request_id: pending.id,
          data: { method: pending.method, arguments: pending.args }, error: null } }));
    } catch { this.invalidate(); }
  }

  private invalidate(): void {
    if (this.closed) return;
    this.closed = true;
    this.verified = null;
    if (this.frameTimer) clearTimeout(this.frameTimer);
    if (this.wireTimer) clearTimeout(this.wireTimer);
    for (const pending of this.pending.values()) pending.finish(failure(pending.id, 'service_unavailable',
      mutations.has(pending.method) && pending === this.active ? { outcome: 'unknown' } : {}));
    this.pending.clear();
    this.queue = [];
    this.active = null;
    this.child?.stdin.end();
    for (const callback of this.disconnected) callback();
  }

  async close(): Promise<void> {
    this.disconnected.clear();
    this.invalidate();
    const child = this.child;
    if (!child || child.exitCode !== null || child.signalCode !== null) return;
    await new Promise<void>(resolve => {
      const timer = setTimeout(() => { child.kill(); resolve(); }, 40_000);
      child.once('close', () => { clearTimeout(timer); resolve(); });
    });
  }
}
