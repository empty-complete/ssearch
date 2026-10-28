import { lstatSync, realpathSync } from 'node:fs';
import { resolve, relative, isAbsolute, extname, join } from 'node:path';

const extensions = new Set(['.html', '.js', '.css', '.png', '.ico', '.txt', '.woff2']);

export function isRendererUrl(url: string): boolean {
  return url === 'ssearch://ui/' || url === 'ssearch://ui/index.html';
}

/** Inspect the original string before URL() can normalize traversal away. */
export function resolveAsset(url: string, assetRoot: string): string | null {
  try {
    if (!url.startsWith('ssearch://ui/') || /[\\?#\u0000-\u0020\u007f]/.test(url)) return null;
    const rawPath = url.slice('ssearch://ui'.length);
    if (/%(?:2f|5c)/i.test(rawPath)) return null;
    const decoded = decodeURIComponent(rawPath);
    if (/[\\%:\u0000-\u001f\u007f]/.test(decoded)) return null;
    const parts = (decoded === '/' ? '/index.html' : decoded).slice(1).split('/');
    if (parts.some(part => !part || part === '.' || part === '..' || /[. ]$/.test(part))) return null;
    const root = resolve(assetRoot);
    if (realpathSync(root) !== root || lstatSync(root).isSymbolicLink()) return null;
    let path = root;
    for (const part of parts) {
      path = join(path, part);
      if (lstatSync(path).isSymbolicLink()) return null;
    }
    const canonical = realpathSync(path);
    const child = relative(root, canonical);
    if (!child || child.startsWith('..') || isAbsolute(child) || canonical !== path) return null;
    if (!extensions.has(extname(path)) || !lstatSync(path).isFile()) return null;
    return path;
  } catch {
    return null;
  }
}

export function permitsAsset(url: string, assetRoot: string): boolean {
  return resolveAsset(url, assetRoot) !== null;
}
