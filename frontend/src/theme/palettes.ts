import type { SemanticPaletteInput } from './contracts';
import catppuccin from '../../compatibility/upstream/catppuccin-palette.json';

const latte = catppuccin.latte.colors;
const mocha = catppuccin.mocha.colors;
// Preserve the two existing first-party adaptations, rather than claiming upstream origin.
const neutralWhite = '#ffffff';
const lightWarning = '#7f4f0b';

const defaultTokens = Object.freeze({
  light: Object.freeze({
    background: latte.base.hex, surface: neutralWhite, 'surface-secondary': latte.mantle.hex,
    foreground: latte.text.hex, muted: latte.subtext1.hex, accent: latte.blue.hex,
    'accent-foreground': neutralWhite, border: latte.surface1.hex, focus: latte.blue.hex,
    danger: latte.red.hex, warning: lightWarning, selected: latte.crust.hex,
    'field-background': neutralWhite, 'field-foreground': latte.text.hex,
    'field-placeholder': latte.subtext1.hex, 'field-border': latte.overlay1.hex,
  }),
  dark: Object.freeze({
    background: mocha.base.hex, surface: mocha.mantle.hex, 'surface-secondary': mocha.surface0.hex,
    foreground: mocha.text.hex, muted: mocha.subtext1.hex, accent: mocha.blue.hex,
    'accent-foreground': mocha.crust.hex, border: mocha.surface2.hex, focus: mocha.blue.hex,
    danger: mocha.red.hex, warning: mocha.yellow.hex, selected: mocha.surface0.hex,
    'field-background': mocha.mantle.hex, 'field-foreground': mocha.text.hex,
    'field-placeholder': mocha.subtext1.hex, 'field-border': mocha.overlay1.hex,
  }),
});

// Named values transcribed from the complete pinned primary snapshots; mappings are ssearch's.
const nord = {
  nord0: '#2e3440', nord1: '#3b4252', nord2: '#434c5e', nord3: '#4c566a',
  nord4: '#d8dee9', nord5: '#e5e9f0', nord6: '#eceff4', nord7: '#8fbcbb',
  nord8: '#88c0d0', nord9: '#81a1c1', nord10: '#5e81ac', nord11: '#bf616a',
  nord12: '#d08770', nord13: '#ebcb8b', nord14: '#a3be8c', nord15: '#b48ead',
};
const dracula = {
  background: '#282a36', currentLine: '#44475a', foreground: '#f8f8f2', comment: '#6272a4',
  cyan: '#8be9fd', green: '#50fa7b', orange: '#ffb86c', pink: '#ff79c6',
  purple: '#bd93f9', red: '#ff5555', yellow: '#f1fa8c',
};

const inputs: readonly SemanticPaletteInput[] = Object.freeze([
  Object.freeze({
    id: 'catppuccin' as const, status: 'default' as const,
    availableModes: Object.freeze(['light', 'dark'] as const), lightComposition: 'default_pair' as const,
    source: Object.freeze({
      repository: 'catppuccin/palette', revision: '07d02aa110ef9eb7e7427afca5c73ba9cf7f8ebd',
      paletteSnapshot: 'catppuccin-palette.json', licenseSnapshot: 'catppuccin-LICENSE',
    }),
    tokens: defaultTokens,
  }),
  Object.freeze({
    id: 'nord' as const, status: 'prepared' as const,
    availableModes: Object.freeze(['dark'] as const), lightComposition: 'requires_explicit_choice' as const,
    source: Object.freeze({
      repository: 'nordtheme/nord', revision: '1cef71605416a222e57225b544540ce0fcec18d4',
      paletteSnapshot: 'nord-palette.css', licenseSnapshot: 'nord-LICENSE',
    }),
    tokens: Object.freeze({ dark: Object.freeze({
      background: nord.nord0, surface: nord.nord1, 'surface-secondary': nord.nord2,
      foreground: nord.nord6, muted: nord.nord4, accent: nord.nord8, 'accent-foreground': nord.nord0,
      border: nord.nord3, focus: nord.nord8, danger: nord.nord11, warning: nord.nord13,
      selected: nord.nord2, 'field-background': nord.nord1, 'field-foreground': nord.nord6,
      'field-placeholder': nord.nord4, 'field-border': nord.nord3,
    }) }),
  }),
  Object.freeze({
    id: 'dracula' as const, status: 'prepared' as const,
    availableModes: Object.freeze(['dark'] as const), lightComposition: 'requires_explicit_choice' as const,
    source: Object.freeze({
      repository: 'dracula/dracula-theme', revision: 'e22ebcc048b8aff586f9bc94db30ff5347070b42',
      paletteSnapshot: 'dracula-palette.md', licenseSnapshot: 'dracula-LICENSE',
    }),
    tokens: Object.freeze({ dark: Object.freeze({
      background: dracula.background, surface: dracula.background, 'surface-secondary': dracula.currentLine,
      foreground: dracula.foreground, muted: dracula.comment, accent: dracula.purple,
      'accent-foreground': dracula.background, border: dracula.currentLine, focus: dracula.purple,
      danger: dracula.red, warning: dracula.yellow, selected: dracula.currentLine,
      'field-background': dracula.background, 'field-foreground': dracula.foreground,
      'field-placeholder': dracula.comment, 'field-border': dracula.comment,
    }) }),
  }),
]);

/** Trusted authoring data. Prepared inputs are not selectable UI presets. */
export function getPaletteInputs(): readonly SemanticPaletteInput[] { return inputs; }
