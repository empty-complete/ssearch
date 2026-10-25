import type { SemanticPaletteInput } from './contracts';

/** Closed checkpoint: prepared inputs are not exposed until their pinned data is supplied. */
export function getPaletteInputs(): readonly SemanticPaletteInput[] {
  return Object.freeze([]);
}
