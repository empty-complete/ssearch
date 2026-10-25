import { realpathSync } from 'node:fs';
import { isAbsolute, relative, extname } from 'node:path';
import { fileURLToPath } from 'node:url';

const assetExtensions = new Set(['.html', '.js', '.css', '.png', '.ico', '.txt', '.woff2']);
/** Task 1 file loader: existing local assets only, no network or outside roots. */
export function permitsAsset(url: string, assetRoot: string): boolean {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'file:' || parsed.hostname || parsed.search || parsed.hash) return false;
    const root = realpathSync(assetRoot); const path = realpathSync(fileURLToPath(parsed));
    const child = relative(root, path);
    return child !== '' && !child.startsWith('..') && !isAbsolute(child) && assetExtensions.has(extname(path).toLowerCase());
  } catch { return false; }
}
