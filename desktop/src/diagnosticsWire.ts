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
  private decoders = new Set<BrokerFrameDecoder>();
  private observed = new WeakSet<Writable>();
  private incomplete: AuditFailureCategory | null = null;

  get failureCategory(): AuditFailureCategory | null {
    return this.incomplete ?? ([...this.decoders].some(decoder => decoder.partial) ? 'partial_snapshot' : null);
  }

  private refuse(category: AuditFailureCategory): void { this.incomplete ??= category; }

  observe(stdin: Writable): void {
    if (this.observed.has(stdin)) { this.refuse('duplicate_stream'); return; }
    if (this.decoders.size >= 64) { this.refuse('stream_limit'); return; }
    this.observed.add(stdin);
    let decoder: BrokerFrameDecoder | null = new BrokerFrameDecoder(MAX_REQUEST_FRAME);
    this.decoders.add(decoder);
    const audit = this;
    const originalWrite = stdin.write;
    stdin.write = function(this: Writable, ...arguments_: unknown[]) {
      const result = Reflect.apply(originalWrite, this, arguments_) as boolean;
      if (decoder) audit.submitted(decoder, arguments_[0]);
      else audit.refuse('frame_shape'); // A retired writer cannot resume outside observation.
      return result;
    } as typeof stdin.write;
    const originalEnd = stdin.end;
    stdin.end = function(this: Writable, ...arguments_: unknown[]) {
      const chunk = arguments_[0];
      // Production closes without a payload. A future alternate writer must not
      // silently bypass observation or be double-counted by an end implementation.
      if (chunk !== undefined && chunk !== null && typeof chunk !== 'function') {
        audit.refuse(chunk === '' || (chunk instanceof Uint8Array && chunk.byteLength === 0)
          ? 'end_payload_empty' : 'end_payload_nonempty');
      }
      return Reflect.apply(originalEnd, this, arguments_);
    } as typeof stdin.end;
    const finished = () => {
      if (!decoder) return;
      const completed = decoder;
      try { completed.end(); }
      catch { audit.refuse('truncated_frame'); }
      finally {
        audit.decoders.delete(completed);
        // runtime.brokers can retain this stream and its wrappers. Release the
        // completed decoder here too, while keeping aggregate counts/IDs.
        decoder = null;
      }
    };
    stdin.once('finish', finished);
    stdin.once('close', finished);
  }

  private submitted(decoder: BrokerFrameDecoder, chunk: unknown): void {
    if (!(chunk instanceof Uint8Array)) { this.refuse('unsupported_chunk'); return; }
    let frames: unknown[];
    try { frames = decoder.push(chunk); }
    catch { this.refuse('frame_decode'); return; }
    try {
      for (const frame of frames) {
        if (!plainObject(frame)) throw new Error();
        if (exactKeys(frame, ['schema_version', 'session_id', 'challenge'])) continue; // Private bootstrap, no operation.
        if (!plainObject(frame.envelope) || !plainObject(frame.envelope.data)
          || !publicUuid(frame.envelope.request_id)
          || !methods.includes(frame.envelope.data.method as UiMethod)) throw new Error();
        const method = frame.envelope.data.method as UiMethod;
        const key = `${method}/${frame.envelope.request_id}`;
        if (this.requests.size >= 1024 && !this.requests.has(key)) { this.refuse('request_limit'); return; }
        this.requests.set(key, (this.requests.get(key) ?? 0) + 1);
        this.counts[method]++;
      }
    } catch { this.refuse('frame_shape'); }
  }

  snapshot(): BrokerFrameCounts {
    if (this.incomplete || [...this.decoders].some(decoder => decoder.partial)) throw new Error('ui_proof_wire_audit_incomplete');
    return Object.freeze({ ...this.counts });
  }

  requireSingleMutation(method: UiMethod, id: string, before: BrokerFrameCounts): void {
    const after = this.snapshot();
    if (!mutations.has(method) || after[method] - before[method] !== 1 || this.requests.get(`${method}/${id}`) !== 1) {
      throw new Error('ui_proof_mutation_dispatch_not_once');
    }
  }
}
