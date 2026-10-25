import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { permitsAsset } from '../src/windowPolicy.js';

test('only canonical ssearch UI assets inside the selected local root may load', () => {
  const dir = mkdtempSync(join(tmpdir(), 'ssearch-assets-'));
  try {
    const root = join(dir, 'Локальный интерфейс'); mkdirSync(root);
    const index = join(root, 'index.html'); writeFileSync(index, '<!doctype html>');
    const secret = join(dir, 'outside.txt'); writeFileSync(secret, 'not an asset');
    assert.equal(permitsAsset('ssearch://ui/', root), true);
    assert.equal(permitsAsset('ssearch://ui/index.html', root), true);
    for (const url of [
      'atom://ui/', 'https://example.com/x.js', 'http://127.0.0.1/status', 'data:text/html,x',
      'javascript:alert(1)', 'file://server/share/x.js', pathToFileURL(index).href,
      pathToFileURL(secret).href, 'ssearch://ui/missing.js', 'ssearch://ui/unknown',
      'ssearch://ui/index.html?other=1', 'ssearch://ui/index.html#fragment',
      'ssearch://other/index.html', 'ssearch://ui:80/index.html', 'ssearch://user@ui/index.html',
      'ssearch://ui/../outside.txt', 'ssearch://ui/%2e%2e/outside.txt',
      'ssearch://ui/%252e%252e/outside.txt', 'ssearch://ui/%2foutside.txt',
      'ssearch://ui/%5coutside.txt', 'ssearch://ui/C:/outside.txt',
      'ssearch://ui//server/share/x.js', 'ssearch://ui/./index.html',
    ]) assert.equal(permitsAsset(url, root), false, url);
    const executable = join(root, 'run.exe'); writeFileSync(executable, 'x');
    assert.equal(permitsAsset('ssearch://ui/run.exe', root), false);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('directory junctions cannot escape or replace the canonical asset root', () => {
  const dir = mkdtempSync(join(tmpdir(), 'ssearch-junction-'));
  try {
    const root = join(dir, 'ui');
    const outside = join(dir, 'outside');
    mkdirSync(root);
    mkdirSync(outside);
    writeFileSync(join(outside, 'secret.js'), 'private');
    symlinkSync(outside, join(root, 'linked'), 'junction');
    symlinkSync(outside, join(dir, 'linked-root'), 'junction');
    assert.equal(permitsAsset('ssearch://ui/linked/secret.js', root), false);
    assert.equal(permitsAsset('ssearch://ui/secret.js', join(dir, 'linked-root')), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
