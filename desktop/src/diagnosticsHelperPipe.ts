/** Fixed self-test helper boundary only. No argv/env descriptor selection. */
export const HELPER_PROTOCOL_FD = 3;
export interface HelperPipeIO {
  inspect(fd: number): { isFIFO(): boolean };
  write(fd: number, bytes: Buffer, offset: number, length: number,
    callback: (error: Error | null, written: number) => void): void;
}
export type HelperPipeClock = (callback: () => void, milliseconds: number) => () => void;

/** Explicit non-executing I/O seam; no default fd access under Node test runners.
 * Current fail-closed baseline awaits its valid-private-pipe RED. */
export function writeHelperPipe(_bytes: Buffer, callback: (error?: Error | null) => void,
  _io: HelperPipeIO, _clock?: HelperPipeClock): () => void {
  callback(new Error('ui_proof_helper_write_failed'));
  return () => {};
}
