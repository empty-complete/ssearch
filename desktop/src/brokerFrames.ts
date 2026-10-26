export const MAX_REQUEST_FRAME = 65_536 + 4096;
export const MAX_RESPONSE_FRAME = 2_097_152 + 4096;
export const decoderFailureCategories = ['length', 'utf8', 'json', 'truncated'] as const;
export type DecoderFailureCategory = typeof decoderFailureCategories[number];

/** Incremental, allocation-bounded four-byte BE/UTF-8 JSON framing. */
export class BrokerFrameDecoder {
  private header = Buffer.alloc(4);
  private headerBytes = 0;
  private body: Buffer | null = null;
  private bodyBytes = 0;
  private failure: DecoderFailureCategory | null = null;
  constructor(private maximum = MAX_RESPONSE_FRAME) {}
  get failureCategory(): DecoderFailureCategory | null { return this.failure; }
  get partial(): boolean { return this.headerBytes > 0 || this.body !== null; }
  push(chunk: Uint8Array): unknown[] {
    const results: unknown[] = [];
    let offset = 0;
    while (offset < chunk.length) {
      if (this.body === null) {
        const count = Math.min(4 - this.headerBytes, chunk.length - offset);
        this.header.set(chunk.subarray(offset, offset + count), this.headerBytes);
        offset += count;
        this.headerBytes += count;
        if (this.headerBytes !== 4) continue;
        const length = this.header.readUInt32BE(0);
        if (length < 1 || length > this.maximum) {
          this.failure ??= 'length';
          throw new Error('invalid_broker_frame');
        }
        this.body = Buffer.alloc(length);
        this.bodyBytes = 0;
      }
      const count = Math.min(this.body.length - this.bodyBytes, chunk.length - offset);
      this.body.set(chunk.subarray(offset, offset + count), this.bodyBytes);
      this.bodyBytes += count;
      offset += count;
      if (this.bodyBytes === this.body.length) {
        let text: string;
        try { text = new TextDecoder('utf-8', { fatal: true }).decode(this.body); }
        catch (error) { this.failure ??= 'utf8'; throw error; }
        try { results.push(JSON.parse(text) as unknown); }
        catch (error) { this.failure ??= 'json'; throw error; }
        this.body = null;
        this.headerBytes = 0;
        this.bodyBytes = 0;
      }
    }
    return results;
  }
  end(): void {
    if (this.partial) {
      this.failure ??= 'truncated';
      throw new Error('truncated_broker_frame');
    }
  }
}

export function encodeBrokerFrame(value: unknown, maximum = MAX_REQUEST_FRAME): Buffer {
  const body = Buffer.from(JSON.stringify(value), 'utf8');
  if (body.length < 1 || body.length > maximum) throw new Error('invalid_broker_frame');
  const header = Buffer.alloc(4);
  header.writeUInt32BE(body.length);
  return Buffer.concat([header, body]);
}
