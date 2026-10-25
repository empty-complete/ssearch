import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { permitsAsset } from '../src/windowPolicy.js';

test('only existing assets inside the selected local root may load', () => {
  const dir = mkdtempSync(join(tmpdir(), 'ssearch-assets-'));
  try {
    const root = join(dir, 'Локальный интерфейс'); mkdirSync(root);
    const index = join(root, 'index.html'); writeFileSync(index, '<!doctype html>');
    const secret = join(dir, 'outside.txt'); writeFileSync(secret, 'not an asset');
    assert.equal(permitsAsset(pathToFileURL(index).href, root), true);
    for (const url of ['https://example.com/x.js', 'http://127.0.0.1/status', 'data:text/html,x', 'javascript:alert(1)', 'file://server/share/x.js', pathToFileURL(secret).href, pathToFileURL(join(root, 'missing.js')).href, `${pathToFileURL(index).href}?other=1`]) assert.equal(permitsAsset(url, root), false, url);
    const executable = join(root, 'run.exe'); writeFileSync(executable, 'x');
    assert.equal(permitsAsset(pathToFileURL(executable).href, root), false);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
