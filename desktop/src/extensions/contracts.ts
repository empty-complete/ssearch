/** UI metadata only. These capabilities confer no service or owner authority. */
export type DesktopExtensionCapability = 'ui.theme-preview';
export type ThemePreviewMode = 'system' | 'light' | 'dark';

export interface ThemePreviewOption {
  readonly value: ThemePreviewMode;
  readonly label: string;
}

export interface ThemePreviewContribution {
  readonly kind: 'theme-preview';
  readonly modes: readonly ThemePreviewOption[];
}

export type DesktopExtensionContribution = ThemePreviewContribution;

export interface DesktopExtensionDescriptor {
  readonly id: string;
  readonly apiVersion: 1;
  readonly capabilities: readonly DesktopExtensionCapability[];
  /** Reserved metadata only; never a store, schema or settings write interface. */
  readonly settingsNamespace: string;
  readonly contributions: readonly DesktopExtensionContribution[];
}

export interface DesktopBuiltinRegistry {
  readonly descriptors: readonly DesktopExtensionDescriptor[];
  readonly themePreviewModes: readonly ThemePreviewOption[];
  resolve(id: string): DesktopExtensionDescriptor | null;
}

export type BuiltinRegistryErrorCode =
  | 'invalid_descriptor'
  | 'unsupported_api_version'
  | 'builtin_not_allowed'
  | 'capability_not_allowed'
  | 'settings_namespace_not_allowed'
  | 'contribution_not_allowed'
  | 'duplicate_builtin';

export class BuiltinRegistryError extends Error {
  constructor(readonly code: BuiltinRegistryErrorCode) {
    super(code);
    this.name = 'BuiltinRegistryError';
  }
}
