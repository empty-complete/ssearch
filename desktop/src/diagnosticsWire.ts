import type { Writable } from 'node:stream';
import { BrokerFrameDecoder, MAX_REQUEST_FRAME } from './brokerFrames.js';
import type { UiMethod } from './contracts.js';
import { exactKeys, methods, mutations, plainObject, publicUuid } from './validation.js';

export type BrokerFrameCounts = Readonly<Record<UiMethod, number>>;
export const auditFailureCategories = ['unclassified', 'duplicate_stream', 'stream_limit', 'end_payload_empty',
  'end_payload_nonempty', 'unsupported_chunk', 'frame_decode', 'frame_shape', 'request_limit',
  'truncated_frame', 'partial_snapshot'] as const;
export type AuditFailureCategory = typeof auditFailureCategories[number];

/** Self-test observer only: unchanged bytes/callbacks/backpressure reach the real
 * stdin writer. Counts mean submitted frames, never proof the owner committed.
 * No arguments, credentials or raw frames are retained or reported. */
export class BrokerStdinAudit {
  private counts = Object.fromEntries(methods.map(method => [method, 0])) as Record<UiMethod, number>;
  private requests = new Map<string, number>();
  private decoders: BrokerFrameDecoder[] = [];
  private observed = new WeakSet<Writable>();
  private incomplete = false;

  get failureCategory(): AuditFailureCategory | null {
    return this.incomplete || this.decoders.some(decoder => decoder.partial) ? 'unclassified' : null;
  }

  observe(stdin: Writable): void {
    if (this.observed.has(stdin) || this.decoders.length >= 64) {
      this.incomplete = true;
      return;
    }
    this.observed.add(stdin);
    const decoder = new BrokerFrameDecoder(MAX_REQUEST_FRAME);
    this.decoders.push(decoder);
    const audit = this;
    const originalWrite = stdin.write;
    stdin.write = function(this: Writable, ...arguments_: unknown[]) {
      const result = Reflect.apply(originalWrite, this, arguments_) as boolean;
      audit.submitted(decoder, arguments_[0]);
      return result;
    } as typeof stdin.write;
    const originalEnd = stdin.end;
    stdin.end = function(this: Writable, ...arguments_: unknown[]) {
      const chunk = arguments_[0];
      // Production closes without a payload. A future alternate writer must not
      // silently bypass observation or be double-counted by an end implementation.
      if (chunk !== undefined && chunk !== null && typeof chunk !== 'function') audit.incomplete = true;
      return Reflect.apply(originalEnd, this, arguments_);
    } as typeof stdin.end;
    const finished = () => { try { decoder.end(); } catch { audit.incomplete = true; } };
    stdin.once('finish', finished);
    stdin.once('close', finished);
  }

  private submitted(decoder: BrokerFrameDecoder, chunk: unknown): void {
    try {
      if (!(chunk instanceof Uint8Array)) throw new Error();
      for (const frame of decoder.push(chunk)) {
        if (!plainObject(frame)) throw new Error();
        if (exactKeys(frame, ['schema_version', 'session_id', 'challenge'])) continue; // Private bootstrap, no operation.
        if (!plainObject(frame.envelope) || !plainObject(frame.envelope.data)
          || !publicUuid(frame.envelope.request_id)
          || !methods.includes(frame.envelope.data.method as UiMethod)) throw new Error();
        const method = frame.envelope.data.method as UiMethod;
        const key = `${method}/${frame.envelope.request_id}`;
        if (this.requests.size >= 1024 && !this.requests.has(key)) throw new Error();
        this.requests.set(key, (this.requests.get(key) ?? 0) + 1);
        this.counts[method]++;
      }
    } catch { this.incomplete = true; }
  }

  snapshot(): BrokerFrameCounts {
    if (this.incomplete || this.decoders.some(decoder => decoder.partial)) throw new Error('ui_proof_wire_audit_incomplete');
    return Object.freeze({ ...this.counts });
  }

  requireSingleMutation(method: UiMethod, id: string, before: BrokerFrameCounts): void {
    const after = this.snapshot();
    if (!mutations.has(method) || after[method] - before[method] !== 1 || this.requests.get(`${method}/${id}`) !== 1) {
      throw new Error('ui_proof_mutation_dispatch_not_once');
    }
  }
}
