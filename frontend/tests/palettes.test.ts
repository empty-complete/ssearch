import { expect, it } from 'vitest';
import { defaultPaletteCss } from '../tools/palette-css';
import { getPaletteInputs } from '../src/theme/palettes';
import type { ColorByte, CustomPaletteInputV1, RgbBytes } from '../src/theme/contracts';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// Independent baseline of the accepted CSS, including existing first-party white/warning choices.
const accepted = {
  light: {
    background: '#eff1f5', surface: '#ffffff', 'surface-secondary': '#e6e9ef',
    foreground: '#4c4f69', muted: '#5c5f77', accent: '#1e66f5', 'accent-foreground': '#ffffff',
    border: '#bcc0cc', focus: '#1e66f5', danger: '#d20f39', warning: '#7f4f0b',
    selected: '#dce0e8', 'field-background': '#ffffff', 'field-foreground': '#4c4f69',
    'field-placeholder': '#5c5f77', 'field-border': '#8c8fa1',
  },
  dark: {
    background: '#1e1e2e', surface: '#181825', 'surface-secondary': '#313244',
    foreground: '#cdd6f4', muted: '#bac2de', accent: '#89b4fa', 'accent-foreground': '#11111b',
    border: '#585b70', focus: '#89b4fa', danger: '#f38ba8', warning: '#f9e2af',
    selected: '#313244', 'field-background': '#181825', 'field-foreground': '#cdd6f4',
    'field-placeholder': '#bac2de', 'field-border': '#7f849c',
  },
};

it('serializes exactly the accepted semantic color roles and original light/dark selectors', () => {
  const css = defaultPaletteCss();
  const blocks = [...css.matchAll(/([^{}]+)\{([^{}]+)\}/g)];
  expect(blocks.map(block => block[1].trim())).toEqual([
    ':root, .light, [data-theme="light"]', '.dark, [data-theme="dark"]',
  ]);
  for (const [index, mode] of (['light', 'dark'] as const).entries()) {
    const body = blocks[index]?.[2] ?? '';
    expect(body).toContain(`color-scheme:${mode};`);
    const colors = Object.fromEntries([...body.matchAll(/--([a-z-]+):([^;]+);/g)]
      .map(match => [match[1], match[2]]));
    expect(colors).toEqual(accepted[mode]);
  }
});

it('keeps prepared dark-only contributions immutable and separate from active default CSS', () => {
  const inputs = getPaletteInputs();
  expect(inputs.map(input => [input.id, input.status, input.availableModes])).toEqual([
    ['catppuccin', 'default', ['light', 'dark']], ['nord', 'prepared', ['dark']], ['dracula', 'prepared', ['dark']],
  ]);
  for (const input of inputs) {
    expect(Reflect.set(input, 'status', 'default')).toBe(false);
    expect(Reflect.set(input.tokens.dark, 'accent', 'url(https://invalid.example)')).toBe(false);
    if (input.status === 'prepared') {
      expect(input.lightComposition).toBe('requires_explicit_choice');
      expect(Object.hasOwn(input.tokens, 'light')).toBe(false);
    }
  }
  expect(defaultPaletteCss()).not.toContain('nord');
  expect(defaultPaletteCss()).not.toContain('dracula');
});

it('never reads or serializes custom CSS/URL data supplied as an extra runtime argument', () => {
  let reads = 0;
  const custom = Object.defineProperty({}, 'tokens', { get() { reads++; return 'url(https://invalid.example)'; } });
  expect(Reflect.apply(defaultPaletteCss, undefined, [custom])).toBe(defaultPaletteCss());
  expect(reads).toBe(0);
});

it('keeps prepared semantic mappings inside their complete pinned primary palettes', () => {
  const sources = resolve(process.cwd(), 'compatibility/upstream');
  const nord = readFileSync(resolve(sources, 'nord-palette.css'), 'utf8');
  const dracula = readFileSync(resolve(sources, 'dracula-palette.md'), 'utf8')
    .split('### Dracula')[1].split('### Alucard')[0];
  const colors = {
    nord: new Set([...nord.matchAll(/--nord\d+:\s*(#[0-9a-f]{6});/g)].map(match => match[1])),
    dracula: new Set([...dracula.matchAll(/`(#[0-9a-f]{6})`/g)].map(match => match[1])),
  };
  expect(colors.nord.size).toBe(16);
  for (const input of getPaletteInputs()) {
    if (input.status !== 'prepared') continue;
    for (const color of Object.values(input.tokens.dark)) expect(colors[input.id].has(color)).toBe(true);
  }
});

// Compile-time public contract checks; these shapes do not activate any runtime input path.
function customContract(input: CustomPaletteInputV1) {
  const byte: ColorByte = 'ff';
  const rgb: RgbBytes = [byte, '00', '80'];
  const allowed: CustomPaletteInputV1 = { ...input, tokens: { ...input.tokens, accent: rgb } };
  // @ts-expect-error CSS text is not bounded RGB data.
  const css: RgbBytes = 'red; background:url(https://invalid.example)';
  // @ts-expect-error A channel must be one byte, not arbitrary text.
  const oversized: ColorByte = '100';
  // @ts-expect-error The token vocabulary has no arbitrary property or URL slot.
  const url: CustomPaletteInputV1 = { ...input, url: 'https://invalid.example' };
  return { allowed, css, oversized, url };
}
void customContract;
