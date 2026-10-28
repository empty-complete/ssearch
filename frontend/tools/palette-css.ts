import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { Plugin } from 'vite';
import { getPaletteInputs } from '../src/theme/palettes';

/** Build-only, no-argument serializer. Custom inputs are never accepted here. */
export function defaultPaletteCss(): string {
  const palette = getPaletteInputs().find(input => input.status === 'default');
  if (!palette) throw new Error('Missing pinned default palette');
  return (['light', 'dark'] as const).map(mode => {
    const selector = mode === 'light' ? ':root, .light, [data-theme="light"]' : '.dark, [data-theme="dark"]';
    const colors = Object.entries(palette.tokens[mode]).map(([role, color]) => {
      if (!/^#[0-9a-f]{6}$/.test(color)) throw new Error('Invalid pinned semantic color');
      return `--${role}:${color};`;
    }).join(' ');
    return `${selector} {\n  color-scheme:${mode}; ${colors}\n}`;
  }).join('\n');
}

/** Keep the existing stylesheet/static link; no runtime CSS or custom-input path. */
export function paletteCssPlugin(projectRoot: string): Plugin {
  const stylesheet = resolve(projectRoot, 'src/styles.css').replaceAll('\\', '/');
  const marker = '/* ssearch:default-palette-tokens */';
  return {
    name: 'ssearch-static-default-palette', enforce: 'pre',
    transform(code, id) {
      if (id.split('?')[0].replaceAll('\\', '/') !== stylesheet) return null;
      if (code.split(marker).length !== 2) throw new Error('Expected exactly one default palette placeholder');
      return { code: code.replace(marker, defaultPaletteCss()), map: null };
    },
    generateBundle() {
      for (const name of ['catppuccin', 'nord', 'dracula']) {
        const original = resolve(projectRoot, `compatibility/upstream/${name}-LICENSE`);
        this.emitFile({
          type: 'asset', fileName: `licenses/palettes/${name}-LICENSE.txt`,
          originalFileName: original, source: readFileSync(original),
        });
      }
    },
  };
}
