/** Build-only consumer of the common PRODUCT export; never a second catalog. */
const { lstatSync, readFileSync, realpathSync, openSync, readSync, closeSync, readdirSync } = require('node:fs');
const { isAbsolute, resolve, join } = require('node:path');
const { createHash } = require('node:crypto');

const productKeys = ['product_name', 'product_name_ru', 'support_email', 'author_name',
  'author_title', 'organization_group', 'copyright_holder'];
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value)
  && Object.getPrototypeOf(value) === Object.prototype;
const exact = (value, keys) => plain(value) && Object.keys(value).length === keys.length
  && keys.every(key => Object.hasOwn(value, key));
const version = value => typeof value === 'string' && /^\d+\.\d+\.\d+$/.test(value);

function readProductMetadata(path, { sourceCommit, manifest, lock }) {
  try {
    if (typeof path !== 'string' || !isAbsolute(path)) throw new Error();
    const stat = lstatSync(path);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 65536
      || realpathSync(path).toLowerCase() !== resolve(path).toLowerCase()) throw new Error();
    const metadata = JSON.parse(readFileSync(path, 'utf8'));
    if (!exact(metadata, ['schema_version', 'source_commit', 'app_version', 'core_version', 'product'])
      || metadata.schema_version !== 1 || !/^[0-9a-f]{40}$/.test(sourceCommit)
      || metadata.source_commit !== sourceCommit || !version(metadata.app_version) || !version(metadata.core_version)
      || !exact(metadata.product, productKeys)
      || productKeys.some(key => typeof metadata.product[key] !== 'string'
        || !metadata.product[key].trim() || metadata.product[key].length > 512
        || /[\u0000-\u001f\u007f]/.test(metadata.product[key]))
      || manifest.version !== metadata.app_version || lock.lockfileVersion !== 3
      || lock.name !== manifest.name || lock.version !== metadata.app_version
      || lock.packages?.['']?.name !== manifest.name || lock.packages[''].version !== metadata.app_version) throw new Error();
    return Object.freeze({ ...metadata, product: Object.freeze(metadata.product) });
  } catch { throw new Error('product_metadata_invalid'); }
}

function stampPackageManifest(manifest, metadata) {
  if (manifest.version !== metadata.app_version) throw new Error('product_metadata_invalid');
  return { ...manifest, version: metadata.app_version, productName: metadata.product.product_name,
    author: `${metadata.product.author_name} <${metadata.product.support_email}>` };
}

function verifyElectronZip(directory, electronVersion, expectedSha256) {
  try {
    if (typeof directory !== 'string' || !isAbsolute(directory) || !version(electronVersion)
      || typeof expectedSha256 !== 'string' || !/^[0-9a-f]{64}$/.test(expectedSha256)) throw new Error();
    const canonical = realpathSync(directory);
    if (canonical.toLowerCase() !== resolve(directory).toLowerCase() || !lstatSync(directory).isDirectory()) throw new Error();
    const archive = join(canonical, `electron-v${electronVersion}-win32-x64.zip`);
    const stat = lstatSync(archive);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size === 0
      || realpathSync(archive).toLowerCase() !== archive.toLowerCase()) throw new Error();
    const hash = createHash('sha256');
    const file = openSync(archive, 'r');
    try {
      const buffer = Buffer.alloc(1024 * 1024);
      let size;
      while ((size = readSync(file, buffer, 0, buffer.length, null)) > 0) hash.update(buffer.subarray(0, size));
    } finally { closeSync(file); }
    if (hash.digest('hex') !== expectedSha256) throw new Error();
    return canonical;
  } catch { throw new Error('electron_archive_unverified'); }
}

function verifyServiceStage(directory) {
  try {
    if (typeof directory !== 'string' || !isAbsolute(directory)) throw new Error();
    const canonical = realpathSync(directory);
    const check = path => {
      const stat = lstatSync(path);
      if (stat.isSymbolicLink() || (!stat.isDirectory() && !stat.isFile())
        || realpathSync(path).toLowerCase() !== path.toLowerCase()) throw new Error();
      if (stat.isDirectory()) for (const entry of readdirSync(path)) check(join(path, entry));
    };
    if (canonical.toLowerCase() !== resolve(directory).toLowerCase() || !lstatSync(directory).isDirectory()) throw new Error();
    check(canonical);
    const image = lstatSync(join(canonical, 'ssearch-service.exe'));
    if (!image.isFile() || image.size === 0 || !lstatSync(join(canonical, '_internal')).isDirectory()
      || !lstatSync(join(canonical, '_internal/models')).isDirectory()) throw new Error();
    return canonical;
  } catch { throw new Error('service_stage_invalid'); }
}

module.exports = { readProductMetadata, stampPackageManifest, verifyElectronZip, verifyServiceStage };
