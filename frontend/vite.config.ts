import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { readFileSync, mkdirSync, copyFileSync } from 'node:fs';
import { resolve } from 'node:path';

// Read the literal stdlib-only PRODUCT catalog without importing Python/Qt/service.
const catalog = readFileSync(resolve(import.meta.dirname, '../src/atom_search/content.py'), 'utf8');
const block = catalog.match(/PRODUCT = (\{[\s\S]*?\n\})/)?.[1];
if (!block) throw new Error('PRODUCT catalog is missing');
const product = JSON.parse(block) as Record<string, string>;
for (const key of ['product_name', 'product_name_ru', 'support_email', 'author_name']) {
  if (!product[key]) throw new Error(`PRODUCT.${key} is missing`);
}
mkdirSync(resolve(import.meta.dirname, 'public'), { recursive: true });
copyFileSync(resolve(import.meta.dirname, '../resources/branding/atom-search.png'), resolve(import.meta.dirname, 'public/app-icon.png'));
copyFileSync(resolve(import.meta.dirname, '../LICENSE.txt'), resolve(import.meta.dirname, 'public/LICENSE.txt'));
copyFileSync(resolve(import.meta.dirname, 'THIRD_PARTY_NOTICES.txt'), resolve(import.meta.dirname, 'public/THIRD_PARTY_NOTICES.txt'));

export default defineConfig({
  base: './', plugins: [
    {
      name: 'explicit-development-csp', apply: 'serve',
      // React refresh's preamble and Vite's loopback websocket exist only in HMR.
      transformIndexHtml: html => html.replace("script-src 'self';", "script-src 'self' 'unsafe-inline';").replace("connect-src 'none';", "connect-src 'self' ws://127.0.0.1:5173;"),
    }, react(), tailwindcss(),
  ],
  define: { __PRODUCT__: JSON.stringify(product) },
  server: { host: '127.0.0.1', strictPort: true, port: 5173 },
  build: { sourcemap: false, assetsInlineLimit: 0 },
  test: { environment: 'jsdom', setupFiles: ['./tests/setup.ts'], include: ['tests/**/*.test.{ts,tsx}'], restoreMocks: true },
});
