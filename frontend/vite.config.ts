import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { mkdirSync, copyFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { readProductMetadata } from './tools/read-product';
import { clientOnlyGraph } from './tools/client-only-graph';
import { paletteCssPlugin } from './tools/palette-css';

const product = readProductMetadata(resolve(import.meta.dirname, '..'));
mkdirSync(resolve(import.meta.dirname, 'public'), { recursive: true });
copyFileSync(resolve(import.meta.dirname, 'assets/ssearch.png'), resolve(import.meta.dirname, 'public/app-icon.png'));
copyFileSync(resolve(import.meta.dirname, '../LICENSE.txt'), resolve(import.meta.dirname, 'public/LICENSE.txt'));
copyFileSync(resolve(import.meta.dirname, 'THIRD_PARTY_NOTICES.txt'), resolve(import.meta.dirname, 'public/THIRD_PARTY_NOTICES.txt'));

export default defineConfig({
  base: './', plugins: [
    {
      name: 'explicit-development-csp', apply: 'serve',
      // React refresh's preamble and Vite's loopback websocket exist only in HMR.
      transformIndexHtml: html => html.replace("script-src 'self';", "script-src 'self' 'unsafe-inline';").replace("connect-src 'none';", "connect-src 'self' ws://127.0.0.1:5173;"),
    }, paletteCssPlugin(import.meta.dirname), react(), tailwindcss(), clientOnlyGraph(import.meta.dirname),
  ],
  define: { __PRODUCT__: JSON.stringify(product) },
  server: { host: '127.0.0.1', strictPort: true, port: 5173 },
  build: { sourcemap: false, assetsInlineLimit: 0 },
  test: { environment: 'jsdom', setupFiles: ['./tests/setup.ts'], include: ['tests/**/*.test.{ts,tsx}'], restoreMocks: true },
});
