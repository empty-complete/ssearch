import { protocol, type Session } from 'electron';
import { readFileSync } from 'node:fs';
import { extname } from 'node:path';
import { resolveAsset, permitsAsset } from './windowPolicy.js';

export const contentSecurityPolicy = [
  "default-src 'none'", "script-src 'self'", "style-src 'self' 'unsafe-inline'",
  "img-src 'self'", "font-src 'self'", "connect-src 'none'", "object-src 'none'",
  "frame-src 'none'", "base-uri 'none'", "form-action 'none'",
].join('; ');

export function registerAssetScheme(): void {
  protocol.registerSchemesAsPrivileged([{
    scheme: 'ssearch', privileges: { standard: true, secure: true },
  }]);
}

const mime: Record<string, string> = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.ico': 'image/x-icon',
  '.txt': 'text/plain; charset=utf-8', '.woff2': 'font/woff2',
};

export function installAssetHandler(isolated: Session, assetRoot: string): void {
  isolated.protocol.handle('ssearch', request => {
    const headers = {
      'Content-Security-Policy': contentSecurityPolicy,
      'X-Content-Type-Options': 'nosniff',
      'Cache-Control': 'no-store',
    };
    const path = resolveAsset(request.url, assetRoot);
    if (!path || !['GET', 'HEAD'].includes(request.method)) {
      return new Response('Forbidden', { status: 403, headers });
    }
    try {
      const bytes = readFileSync(path);
      if (resolveAsset(request.url, assetRoot) !== path) throw new Error('changed_asset');
      return new Response(request.method === 'HEAD' ? null : new Uint8Array(bytes), {
        headers: { ...headers, 'Content-Type': mime[extname(path)] },
      });
    } catch {
      return new Response('Forbidden', { status: 403, headers });
    }
  });
  isolated.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  isolated.setPermissionCheckHandler(() => false);
  isolated.on('will-download', event => event.preventDefault());
  isolated.webRequest.onBeforeRequest((details, callback) => callback({
    cancel: details.method !== 'GET' || details.resourceType === 'subFrame'
      || !permitsAsset(details.url, assetRoot),
  }));
  isolated.webRequest.onHeadersReceived((details, callback) => callback({
    responseHeaders: {
      ...details.responseHeaders,
      'Content-Security-Policy': [contentSecurityPolicy],
      'X-Content-Type-Options': ['nosniff'],
    },
  }));
}
