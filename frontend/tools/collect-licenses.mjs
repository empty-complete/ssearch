import { readFile, readdir, mkdir, copyFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { createHash } from 'node:crypto';

const root = resolve(import.meta.dirname, '..');
const lock = JSON.parse(await readFile(join(root, 'package-lock.json'), 'utf8'));
const inventory = [];

for (const [location, entry] of Object.entries(lock.packages)) {
  if (!location || entry.dev || !location.includes('node_modules/')) continue;
  let manifest;
  try {
    manifest = JSON.parse(await readFile(join(root, location, 'package.json'), 'utf8'));
  } catch {
    if (entry.optional) continue;
    throw new Error(`Missing production dependency ${location}`);
  }
  const destination = join(root, 'dist/licenses', location.replaceAll('node_modules/', ''));
  await mkdir(destination, { recursive: true });
  const licenses = (await readdir(join(root, location)))
    .filter(name => /^(license|licence|copying|notice)(\.|$|-)/i.test(name));
  if (!licenses.length) {
    const knownIntegrity = 'sha512-IV3Ou0jSMzZrd3pZ48nLkT9DA7Ag1pnPzaiQhpW7c3RbcqqzvzzVu+L8gfqMp/8IM2MQtSiqaCxrrcfu8I8rMA==';
    if (manifest.name !== 'client-only' || manifest.version !== '0.0.1'
      || manifest.license !== 'MIT' || entry.integrity !== knownIntegrity) {
      throw new Error(`No license text in ${location}`);
    }
    const evidence = JSON.parse(await readFile(join(root,
      'compatibility/upstream/client-only-0.0.1-evidence.json'), 'utf8'));
    const actualNames = (await readdir(join(root, location))).sort();
    const expectedNames = evidence.files.map(file => file.file).sort();
    if (JSON.stringify(actualNames) !== JSON.stringify(expectedNames)) {
      throw new Error('client-only source inventory changed');
    }
    for (const expected of evidence.files) {
      const bytes = await readFile(join(root, location, expected.file));
      const digest = createHash('sha256').update(bytes).digest('hex');
      if (bytes.length !== expected.bytes || digest !== expected.sha256) {
        throw new Error(`client-only source differs: ${expected.file}`);
      }
    }
    if (manifest.exports?.['.']?.default !== './index.js'
      || evidence.files.find(file => file.file === 'index.js')?.bytes !== 0) {
      throw new Error('client-only browser entry is no longer empty');
    }
    await copyFile(join(root, location, 'package.json'), join(destination, 'package.json'));
    await writeFile(join(destination, 'upstream-provenance.json'), JSON.stringify(evidence, null, 2));
    inventory.push({
      package: manifest.name, version: manifest.version, license: manifest.license,
      integrity: entry.integrity, texts: [], upstream_license_text_supplied: false,
      copyright_holder: null, empty_browser_entry: true,
      provenance: 'upstream-provenance.json', packaging_provenance_gate: 'open',
    });
    continue;
  }
  for (const name of licenses) {
    await copyFile(join(root, location, name), join(destination, name));
  }
  inventory.push({
    package: manifest.name,
    version: manifest.version,
    license: manifest.license,
    integrity: entry.integrity,
    texts: licenses,
  });
}
await writeFile(join(root, 'dist/licenses/inventory.json'), JSON.stringify(inventory, null, 2));
