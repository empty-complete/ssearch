import type { DesktopExtensionDescriptor } from './contracts.js';

/** Compile-time host policy, not discoveries or registrations from callers. */
const appearance: DesktopExtensionDescriptor = Object.freeze({
  id: 'ssearch.appearance',
  apiVersion: 1 as const,
  capabilities: Object.freeze(['ui.theme-preview'] as const),
  settingsNamespace: 'ssearch.appearance',
  contributions: Object.freeze([Object.freeze({
    kind: 'theme-preview' as const,
    modes: Object.freeze([
      Object.freeze({ value: 'system' as const, label: 'Как в системе' }),
      Object.freeze({ value: 'light' as const, label: 'Светлая' }),
      Object.freeze({ value: 'dark' as const, label: 'Тёмная' }),
    ]),
  })]),
});

export const trustedBuiltinCatalog: readonly DesktopExtensionDescriptor[] = Object.freeze([appearance]);
