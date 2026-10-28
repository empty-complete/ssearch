import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync, realpathSync, mkdirSync, readFileSync, symlinkSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
const { readProductMetadata, stampPackageManifest, verifyElectronZip, verifyServiceStage } = require('../product-metadata.cjs');

function fixture() {
  const root = realpathSync(mkdtempSync(join(tmpdir(), 'ssearch product metadata ')));
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
    (f: ReturnType<typeof fixture>) => { Object.assign(f.metadata, { ignored_field: 'unsupported' }); },
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

test('app and core export versions retain their separate meanings', () => {
  const f = fixture();
  try {
    f.metadata.core_version = '0.3.0';
    f.write();
    const metadata = readProductMetadata(f.path, f.expected);
    assert.equal(stampPackageManifest(f.manifest, metadata).version, '0.2.0');
    assert.equal(metadata.core_version, '0.3.0');
  } finally { f.cleanup(); }
});

test('only the independently confirmed exact local Electron archive can reach packaging', () => {
  const root = realpathSync(mkdtempSync(join(tmpdir(), 'ssearch prepared Electron ')));
  const archive = join(root, 'electron-v45.0.0-win32-x64.zip');
  const bytes = Buffer.from('synthetic archive policy fixture; not a runnable Electron distribution');
  const confirmed = createHash('sha256').update(bytes).digest('hex');
  try {
    assert.throws(() => verifyElectronZip(root, '45.0.0', confirmed), { message: 'electron_archive_unverified' });
    writeFileSync(archive, bytes);
    assert.equal(verifyElectronZip(root, '45.0.0', confirmed).toLowerCase(), root.toLowerCase());
    for (const digest of [undefined, '', '0'.repeat(64)]) {
      assert.throws(() => verifyElectronZip(root, '45.0.0', digest), { message: 'electron_archive_unverified' });
    }
    assert.throws(() => verifyElectronZip(root, '44.0.0', confirmed), { message: 'electron_archive_unverified' });
    writeFileSync(archive, Buffer.concat([bytes, Buffer.from('changed')]));
    assert.throws(() => verifyElectronZip(root, '45.0.0', confirmed), { message: 'electron_archive_unverified' });
    writeFileSync(archive, Buffer.alloc(0));
    assert.throws(() => verifyElectronZip(root, '45.0.0', confirmed), { message: 'electron_archive_unverified' });
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('Forge consumes current PRODUCT and copies the complete fixed service closure outside ASAR', async () => {
  const f = fixture();
  const stage = join(f.root, 'frozen-stage');
  const output = join(f.root, 'ssearch-win32-x64');
  const bytes = Buffer.from('synthetic archive; no Electron package is executed by this policy test');
  const envKeys = ['SSEARCH_PRODUCT_METADATA', 'SSEARCH_ELECTRON_ZIP_DIR',
    'SSEARCH_ELECTRON_ZIP_SHA256', 'SSEARCH_SERVICE_STAGE_DIR'];
  const previous = envKeys.map(key => process.env[key]);
  const configPath = require.resolve('../forge.config.cjs');
  try {
    f.metadata.source_commit = execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
    f.write();
    mkdirSync(join(stage, '_internal/models'), { recursive: true });
    writeFileSync(join(stage, 'ssearch-service.exe'), 'synthetic frozen-image bytes');
    writeFileSync(join(stage, '_internal/models/model.bin'), Buffer.from([0, 1, 2, 255]));
    writeFileSync(join(stage, '_internal/native.dll'), 'synthetic native-resource bytes');
    mkdirSync(join(output, 'resources'), { recursive: true });
    writeFileSync(join(f.root, 'electron-v45.0.0-win32-x64.zip'), bytes);
    process.env.SSEARCH_PRODUCT_METADATA = f.path;
    process.env.SSEARCH_ELECTRON_ZIP_DIR = f.root;
    process.env.SSEARCH_ELECTRON_ZIP_SHA256 = createHash('sha256').update(bytes).digest('hex');
    process.env.SSEARCH_SERVICE_STAGE_DIR = stage;
    delete require.cache[configPath];
    const config = require(configPath);
    assert.equal(config.packagerConfig.appVersion, f.metadata.app_version);
    assert.equal(config.packagerConfig.buildVersion, f.metadata.app_version);
    assert.deepEqual(config.packagerConfig.extraResource, [f.path]);
    assert.equal(config.packagerConfig.electronZipDir, f.root);
    await config.hooks.prePackage(config, 'win32', 'x64');
    await assert.rejects(config.hooks.prePackage(config, 'linux', 'x64'), { message: 'package_target_unsupported' });
    const stamped = await config.hooks.readPackageJson(config, f.manifest);
    assert.equal(stamped.version, f.metadata.app_version);
    assert.equal(stamped.author, `${f.metadata.product.author_name} <${f.metadata.product.support_email}>`);
    await config.hooks.postPackage(config, { platform: 'win32', arch: 'x64', outputPaths: [output] });
    for (const relative of ['ssearch-service.exe', '_internal/models/model.bin', '_internal/native.dll']) {
      assert.deepEqual(readFileSync(join(output, 'service', relative)), readFileSync(join(stage, relative)));
    }
    assert.equal(existsSync(join(output, 'resources/service')), false);
    await assert.rejects(config.hooks.postPackage(config, { platform: 'win32', arch: 'x64', outputPaths: [output] }),
      { message: 'package_service_already_exists' });
    delete process.env.SSEARCH_ELECTRON_ZIP_SHA256;
    delete require.cache[configPath];
    assert.throws(() => require(configPath), { message: 'electron_archive_unverified' });
  } finally {
    delete require.cache[configPath];
    envKeys.forEach((key, index) => {
      if (previous[index] === undefined) delete process.env[key];
      else process.env[key] = previous[index];
    });
    f.cleanup();
  }
});

test('stage validation refuses missing fixed resources and reparse entries anywhere in the closure', () => {
  const f = fixture();
  const foreign = realpathSync(mkdtempSync(join(tmpdir(), 'ssearch foreign resource ')));
  try {
    assert.throws(() => verifyServiceStage(undefined), { message: 'service_stage_invalid' });
    assert.throws(() => verifyServiceStage(f.root), { message: 'service_stage_invalid' });
    writeFileSync(join(f.root, 'ssearch-service.exe'), 'synthetic image');
    mkdirSync(join(f.root, '_internal/models'), { recursive: true });
    assert.equal(verifyServiceStage(f.root), f.root);
    symlinkSync(foreign, join(f.root, '_internal/escaped-resource'), 'junction');
    assert.throws(() => verifyServiceStage(f.root), { message: 'service_stage_invalid' });
  } finally { f.cleanup(); rmSync(foreign, { recursive: true, force: true }); }
});
