import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const { readProductMetadata, stampPackageManifest } = require('../product-metadata.cjs');

function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'ssearch product metadata '));
  const path = join(root, 'product-metadata.json');
  const metadata = { schema_version: 1, source_commit: 'a'.repeat(40), app_version: '0.2.0', core_version: '0.2.0',
    product: { product_name: 'ssearch', product_name_ru: 'Simple Search', support_email: 'test@example.invalid',
      author_name: 'Synthetic author', author_title: 'Synthetic title', organization_group: 'synthetic-test',
      copyright_holder: 'Synthetic holder' } };
  const manifest = { name: 'ssearch-desktop', version: '0.2.0', main: 'dist/main.js' };
  const lock = { name: manifest.name, version: manifest.version, lockfileVersion: 3,
    packages: { '': { name: manifest.name, version: manifest.version } } };
  const expected = { sourceCommit: metadata.source_commit, manifest, lock };
  const write = () => writeFileSync(path, JSON.stringify(metadata));
  write();
  return { root, path, metadata, manifest, lock, expected, write,
    cleanup: () => rmSync(root, { recursive: true, force: true }) };
}

test('common PRODUCT export stamps the packaged manifest without another identity catalog', () => {
  const f = fixture();
  try {
    const metadata = readProductMetadata(f.path, f.expected);
    const stamped = stampPackageManifest(f.manifest, metadata);
    assert.equal(stamped.version, metadata.app_version);
    assert.equal(stamped.productName, metadata.product.product_name);
    assert.equal(stamped.author, `${metadata.product.author_name} <${metadata.product.support_email}>`);
    assert.equal(stamped.main, f.manifest.main);
    assert.equal(Object.hasOwn(f.manifest, 'productName'), false);
    assert.equal(metadata.source_commit, f.expected.sourceCommit);
  } finally { f.cleanup(); }
});

test('stale source, unsupported schema, malformed PRODUCT and stale source package versions refuse packaging', () => {
  for (const mutate of [
    (f: ReturnType<typeof fixture>) => { f.metadata.source_commit = 'b'.repeat(40); },
    (f: ReturnType<typeof fixture>) => { f.metadata.schema_version = 2; },
    (f: ReturnType<typeof fixture>) => { f.metadata.core_version = 'unverified'; },
    (f: ReturnType<typeof fixture>) => { f.metadata.product.support_email = ''; },
    (f: ReturnType<typeof fixture>) => { f.manifest.version = '0.1.1'; },
    (f: ReturnType<typeof fixture>) => { f.lock.packages[''].version = '0.1.1'; },
  ]) {
    const f = fixture();
    try {
      mutate(f);
      f.write();
      assert.throws(() => readProductMetadata(f.path, f.expected), { message: 'product_metadata_invalid' });
    } finally { f.cleanup(); }
  }
});
