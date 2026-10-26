import { fstatSync, write } from 'node:fs';

/** Fixed self-test helper boundary only. No argv/env descriptor selection. */
export const HELPER_PROTOCOL_FD = 3;
export interface HelperPipeIO {
  inspect(fd: number): { isFIFO(): boolean };
  write(fd: number, bytes: Buffer, offset: number, length: number,
    callback: (error: Error | null, written: number) => void): void;
}
export type HelperPipeClock = (callback: () => void, milliseconds: number) => () => void;

/** Native descriptor classification boundary; current Stats predicate retained
 * until the Windows st_mode contract's assertion RED has been observed. */
export function helperDescriptorIsPipe(stat: { readonly mode?: unknown; isFIFO(): boolean }): boolean {
  return stat.isFIFO();
}

const pipeClock: HelperPipeClock = (callback, milliseconds) => {
  const timer = setTimeout(callback, milliseconds);
  return () => clearTimeout(timer);
};

/** Explicit I/O adapter keeps Node policy tests away from their own fd3. Only
 * unwritten bytes advance; no error/deadline can retry a frame or resume it. */
export function writeHelperPipe(bytes: Buffer, callback: (error?: Error | null) => void,
  io: HelperPipeIO, clock: HelperPipeClock = pipeClock): () => void {
  let settled = false;
  let offset = 0;
  let cancelTimer = () => {};
  const finish = (failed: boolean) => {
    if (settled) return;
    settled = true;
    cancelTimer();
    callback(failed ? new Error('ui_proof_helper_write_failed') : null);
  };
  const cancel = () => finish(true);
  const advance = () => {
    if (settled) return;
    const length = bytes.length - offset;
    let called = false;
    try {
      io.write(HELPER_PROTOCOL_FD, bytes, offset, length, (error, written) => {
        if (settled || called) return;
        called = true;
        if (error || !Number.isInteger(written) || written <= 0 || written > length) { finish(true); return; }
        offset += written;
        if (offset === bytes.length) finish(false);
        else queueMicrotask(advance);
      });
    } catch { finish(true); }
  };
  try {
    if (!Buffer.isBuffer(bytes) || bytes.length < 4 || bytes.length > 65_540
      || !io.inspect(HELPER_PROTOCOL_FD).isFIFO()) { finish(true); return cancel; }
    cancelTimer = clock(cancel, 2_000);
    if (settled) cancelTimer();
    else advance();
  } catch { finish(true); }
  return cancel;
}

/** fd3 can otherwise be absent or reused for an unrelated file. No descriptor
 * access occurs outside the Windows Electron MAIN helper process. */
export function writeNativeHelperPipe(bytes: Buffer, callback: (error?: Error | null) => void): () => void {
  if (process.platform !== 'win32' || !process.versions.electron || process.type !== 'browser') {
    callback(new Error('ui_proof_helper_write_failed'));
    return () => {};
  }
  return writeHelperPipe(bytes, callback, {
    inspect: fd => {
      const stat = fstatSync(fd);
      return { isFIFO: () => helperDescriptorIsPipe(stat) };
    },
    write: (fd, buffer, offset, length, done) => { write(fd, buffer, offset, length, null, done); },
  });
}
