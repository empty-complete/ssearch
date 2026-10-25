import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { permitsAsset } from '../src/windowPolicy.js';

test('only canonical atom UI assets inside the selected local root may load', () => {
  const dir = mkdtempSync(join(tmpdir(), 'ssearch-assets-'));
  try {
    const root = join(dir, 'Локальный интерфейс'); mkdirSync(root);
    const index = join(root, 'index.html'); writeFileSync(index, '<!doctype html>');
    const secret = join(dir, 'outside.txt'); writeFileSync(secret, 'not an asset');
    assert.equal(permitsAsset('atom://ui/', root), true);
    assert.equal(permitsAsset('atom://ui/index.html', root), true);
    for (const url of [
      'https://example.com/x.js', 'http://127.0.0.1/status', 'data:text/html,x',
      'javascript:alert(1)', 'file://server/share/x.js', pathToFileURL(index).href,
      pathToFileURL(secret).href, 'atom://ui/missing.js', 'atom://ui/unknown',
      'atom://ui/index.html?other=1', 'atom://ui/index.html#fragment',
      'atom://other/index.html', 'atom://ui:80/index.html', 'atom://user@ui/index.html',
      'atom://ui/../outside.txt', 'atom://ui/%2e%2e/outside.txt',
      'atom://ui/%252e%252e/outside.txt', 'atom://ui/%2foutside.txt',
      'atom://ui/%5coutside.txt', 'atom://ui/C:/outside.txt',
      'atom://ui//server/share/x.js', 'atom://ui/./index.html',
    ]) assert.equal(permitsAsset(url, root), false, url);
    const executable = join(root, 'run.exe'); writeFileSync(executable, 'x');
    assert.equal(permitsAsset('atom://ui/run.exe', root), false);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
