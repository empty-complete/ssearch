import { BuiltinRegistryError, type DesktopBuiltinRegistry } from './contracts.js';

/** Fail-closed source checkpoint. Host-approved metadata is not enabled yet. */
export function createBuiltinRegistry(descriptors?: unknown): DesktopBuiltinRegistry {
  if (descriptors !== undefined) throw new BuiltinRegistryError('builtin_not_allowed');
  return Object.freeze({
    descriptors: Object.freeze([]),
    themePreviewModes: Object.freeze([]),
    resolve: () => null,
  });
}
