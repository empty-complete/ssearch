import { readFile, readdir, mkdir, copyFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { createHash } from 'node:crypto';

const root = resolve(import.meta.dirname, '..');
const lock = JSON.parse(await readFile(join(root, 'package-lock.json'), 'utf8'));
const inventory = [];
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const knownFiles = [
  { file: 'error.js', bytes: 144, sha256: '7d211552731db17e1ff8635eea0970ef57ecc91b04b95536b9fa5abbfafaec26' },
  { file: 'index.js', bytes: 0, sha256: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855' },
  { file: 'package.json', bytes: 467, sha256: '4d6342705767832f299b9a59c28e4275bcf02db19472732f93f67d979441df8f' },
];

async function outputFiles(directory) {
  return (await Promise.all((await readdir(directory, { withFileTypes: true })).map(entry =>
    entry.isDirectory() ? outputFiles(join(directory, entry.name)) : [join(directory, entry.name)]))).flat();
}

async function verifyClientOnlyBuildProof(evidencePath) {
  try {
    const proof = JSON.parse(await readFile(join(root, 'dist/licenses/client-only/build-graph.json'), 'utf8'));
    const telemetry = proof.telemetry;
    if (proof.schema_version !== 1 || proof.package !== 'client-only' || proof.version !== '0.0.1'
      || !/^[0-9a-f]{40}$/.test(proof.source_sha)
      || proof.lock_sha256 !== sha256(await readFile(join(root, 'package-lock.json')))
      || proof.evidence_sha256 !== sha256(await readFile(evidencePath))
      || telemetry?.graph_api !== 'rolldown.getModuleIds' || telemetry.complete !== true
      || !Number.isInteger(telemetry.module_count) || telemetry.module_count < 1
      || !Number.isInteger(telemetry.parsed_module_count) || telemetry.parsed_module_count < 1
      || telemetry.application_entry_observed !== true || telemetry.output_modules_observed !== true
      || !Array.isArray(proof.resolve_conditions) || proof.resolve_conditions.includes('react-server')) {
      throw new Error('unsupported or stale telemetry');
    }
    if (!Array.isArray(proof.package_modules) || proof.package_modules.length > 1
      || !Array.isArray(proof.rendered_modules) || !Array.isArray(proof.external_imports)
      || proof.external_imports.length || !Array.isArray(proof.package_assets) || proof.package_assets.length) {
      throw new Error('package graph or output is not empty');
    }
    for (const module of proof.package_modules) {
      if (module.id !== 'node_modules/client-only/index.js' || module.module_parsed !== true
        || module.source_bytes !== 0 || module.source_sha256 !== knownFiles[1].sha256
        || module.transformed_bytes !== 0) throw new Error('nonempty or unsupported module');
    }
    for (const module of proof.rendered_modules) {
      if (module.id !== 'node_modules/client-only/index.js' || module.rendered_length !== 0
        || proof.package_modules.length !== 1) throw new Error('rendered package implementation');
    }
    if (!Array.isArray(proof.chunks) || !proof.chunks.length) throw new Error('missing chunk inventory');
    const expectedScripts = new Set();
    for (const chunk of proof.chunks) {
      if (typeof chunk.file !== 'string' || !chunk.file.endsWith('.js')
        || /[\\:\u0000?#]/.test(chunk.file) || chunk.file.split('/').some(part => !part || part === '.' || part === '..')
        || expectedScripts.has(chunk.file)) throw new Error('invalid chunk path');
      expectedScripts.add(chunk.file);
      const bytes = await readFile(join(root, 'dist', chunk.file));
      if (bytes.length !== chunk.bytes || sha256(bytes) !== chunk.sha256) throw new Error('changed or missing chunk');
    }
    const actualScripts = [];
    for (const file of await outputFiles(join(root, 'dist'))) {
      const name = file.slice(join(root, 'dist').length + 1).replaceAll('\\', '/');
      const bytes = await readFile(file);
      if (bytes.length === knownFiles[0].bytes && sha256(bytes) === knownFiles[0].sha256) {
        throw new Error('copied package implementation');
      }
      if (/\.(?:js|mjs|cjs)$/.test(name) && (!name.startsWith('licenses/') || name.startsWith('licenses/client-only/'))) {
        actualScripts.push(name);
      }
    }
    if (actualScripts.length !== expectedScripts.size || actualScripts.some(file => !expectedScripts.has(file))) {
      throw new Error('added or missing output chunk');
    }
    return proof.package_modules.length ? 'observed_empty_default_entry' : 'absent_from_complete_browser_graph';
  } catch {
    throw new Error('client-only build proof is missing, stale, unsupported or includes implementation');
  }
}

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
    const evidencePath = join(root, 'compatibility/upstream/client-only-0.0.1-evidence.json');
    const evidence = JSON.parse(await readFile(evidencePath, 'utf8'));
    if (!Array.isArray(evidence.files) || evidence.files.length !== knownFiles.length
      || evidence.files.some((file, index) => file.file !== knownFiles[index].file
        || file.bytes !== knownFiles[index].bytes || file.sha256 !== knownFiles[index].sha256)) {
      throw new Error('client-only source evidence differs from the exact original release');
    }
    const actualNames = (await readdir(join(root, location))).sort();
    const expectedNames = knownFiles.map(file => file.file).sort();
    if (JSON.stringify(actualNames) !== JSON.stringify(expectedNames)) {
      throw new Error('client-only source inventory changed');
    }
    for (const expected of knownFiles) {
      const bytes = await readFile(join(root, location, expected.file));
      const digest = createHash('sha256').update(bytes).digest('hex');
      if (bytes.length !== expected.bytes || digest !== expected.sha256) {
        throw new Error(`client-only source differs: ${expected.file}`);
      }
    }
    if (manifest.exports?.['.']?.default !== './index.js'
      || manifest.exports?.['.']?.['react-server'] !== './error.js') {
      throw new Error('client-only browser entry is no longer empty');
    }
    const graphCase = await verifyClientOnlyBuildProof(evidencePath);
    await copyFile(join(root, location, 'package.json'), join(destination, 'package.json'));
    await writeFile(join(destination, 'upstream-provenance.json'), JSON.stringify(evidence, null, 2));
    inventory.push({
      package: manifest.name, version: manifest.version, license: manifest.license,
      integrity: entry.integrity, texts: [], upstream_license_text_supplied: false,
      copyright_holder: null, empty_browser_entry: true,
      provenance: 'upstream-provenance.json', packaging_provenance_gate: 'open',
      source_bundle_disposition: 'no_client_only_implementation', browser_graph_case: graphCase,
      browser_build_proof: 'build-graph.json', actual_package_acceptance: 'not_run',
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
