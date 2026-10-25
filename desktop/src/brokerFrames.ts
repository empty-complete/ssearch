export const MAX_REQUEST_FRAME = 65_536 + 4096;
export const MAX_RESPONSE_FRAME = 2_097_152 + 4096;

/** Fail-closed transport checkpoint; no frame is admitted until decoding is implemented. */
export class BrokerFrameDecoder {
  get partial(): boolean { return false; }
  push(_chunk: Uint8Array): unknown[] { return []; }
  end(): void {}
}

export function encodeBrokerFrame(_value: unknown, _maximum = MAX_REQUEST_FRAME): Buffer {
  throw new Error('broker_unavailable');
}
