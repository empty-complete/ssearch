import {
  BuiltinRegistryError, type BuiltinRegistryErrorCode,
  type DesktopBuiltinRegistry, type DesktopExtensionDescriptor,
} from './contracts.js';
import { trustedBuiltinCatalog } from './catalog.js';

/** Plain static metadata only. Inspect data properties without invoking accessors. */
function record(value: unknown, keys: readonly string[], code: BuiltinRegistryErrorCode): Record<string, unknown> {
  if (typeof value !== 'object' || value === null
    || ![Object.prototype, null].includes(Object.getPrototypeOf(value))) throw new BuiltinRegistryError(code);
  const names = Object.getOwnPropertyNames(value);
  if (names.length !== keys.length || names.some(name => !keys.includes(name))
    || Object.getOwnPropertySymbols(value).length) throw new BuiltinRegistryError(code);
  const result: Record<string, unknown> = Object.create(null);
  for (const name of names) {
    const property = Object.getOwnPropertyDescriptor(value, name)!;
    if (!Object.hasOwn(property, 'value') || !property.enumerable) throw new BuiltinRegistryError(code);
    result[name] = property.value;
  }
  return result;
}

function list(value: unknown, limit: number, code: BuiltinRegistryErrorCode): unknown[] {
  if (!Array.isArray(value)) throw new BuiltinRegistryError(code);
  const length = Object.getOwnPropertyDescriptor(value, 'length')?.value as unknown;
  if (typeof length !== 'number' || !Number.isInteger(length) || length < 0 || length > limit
    || Object.getOwnPropertyNames(value).length !== length + 1
    || Object.getOwnPropertySymbols(value).length) throw new BuiltinRegistryError(code);
  const result: unknown[] = [];
  for (let index = 0; index < length; index++) {
    const property = Object.getOwnPropertyDescriptor(value, String(index));
    if (!property || !Object.hasOwn(property, 'value') || !property.enumerable) throw new BuiltinRegistryError(code);
    result.push(property.value);
  }
  return result;
}

function identifier(value: unknown): value is string {
  return typeof value === 'string' && value.length <= 64
    && /^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9-]*){1,3}$/.test(value);
}

function resolveApproved(value: unknown): DesktopExtensionDescriptor {
  const descriptor = record(value, [
    'id', 'apiVersion', 'capabilities', 'settingsNamespace', 'contributions',
  ], 'invalid_descriptor');
  if (!identifier(descriptor.id)) throw new BuiltinRegistryError('invalid_descriptor');
  if (descriptor.apiVersion !== 1) throw new BuiltinRegistryError('unsupported_api_version');
  const approved = trustedBuiltinCatalog.find(builtin => builtin.id === descriptor.id);
  if (!approved) throw new BuiltinRegistryError('builtin_not_allowed');

  const capabilities = list(descriptor.capabilities, 8, 'capability_not_allowed');
  if (capabilities.length !== approved.capabilities.length
    || capabilities.some((capability, index) => capability !== approved.capabilities[index])) {
    throw new BuiltinRegistryError('capability_not_allowed');
  }
  if (!identifier(descriptor.settingsNamespace) || descriptor.settingsNamespace !== approved.settingsNamespace) {
    throw new BuiltinRegistryError('settings_namespace_not_allowed');
  }

  const contributions = list(descriptor.contributions, 8, 'contribution_not_allowed');
  if (contributions.length !== approved.contributions.length) throw new BuiltinRegistryError('contribution_not_allowed');
  for (let index = 0; index < contributions.length; index++) {
    const contribution = record(contributions[index], ['kind', 'modes'], 'contribution_not_allowed');
    const canonical = approved.contributions[index];
    if (contribution.kind !== canonical.kind) throw new BuiltinRegistryError('contribution_not_allowed');
    const modes = list(contribution.modes, 3, 'contribution_not_allowed');
    if (modes.length !== canonical.modes.length) throw new BuiltinRegistryError('contribution_not_allowed');
    for (let option = 0; option < modes.length; option++) {
      const mode = record(modes[option], ['value', 'label'], 'contribution_not_allowed');
      if (mode.value !== canonical.modes[option].value || mode.label !== canonical.modes[option].label) {
        throw new BuiltinRegistryError('contribution_not_allowed');
      }
    }
  }
  // Never return candidate references, permissions or contribution data.
  return approved;
}

/** Select only host-defined builtins. This is not an external registration API. */
export function createBuiltinRegistry(input: unknown = trustedBuiltinCatalog): DesktopBuiltinRegistry {
  const descriptors: DesktopExtensionDescriptor[] = [];
  const selected = new Set<string>();
  for (const candidate of list(input, 16, 'invalid_descriptor')) {
    const approved = resolveApproved(candidate);
    if (selected.has(approved.id)) throw new BuiltinRegistryError('duplicate_builtin');
    selected.add(approved.id);
    descriptors.push(approved);
  }
  const entries = Object.freeze(descriptors);
  const themePreviewModes = Object.freeze(entries.flatMap(descriptor =>
    descriptor.contributions.flatMap(contribution => contribution.modes)));
  const resolve = Object.freeze((id: string) => entries.find(descriptor => descriptor.id === id) ?? null);
  return Object.freeze({ descriptors: entries, themePreviewModes, resolve });
}

export const builtinExtensions = createBuiltinRegistry();
