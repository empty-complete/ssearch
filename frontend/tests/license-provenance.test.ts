import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { afterEach, expect, it } from 'vitest';

const execute = promisify(execFile);
// npm test runs with frontend as its explicit working directory; no jsdom URL conversion.
const source = resolve(process.cwd());
const temporary: string[] = [];
const integrity = 'sha512-IV3Ou0jSMzZrd3pZ48nLkT9DA7Ag1pnPzaiQhpW7c3RbcqqzvzzVu+L8gfqMp/8IM2MQtSiqaCxrrcfu8I8rMA==';
const digest = (bytes: Uint8Array | string) => createHash('sha256').update(bytes).digest('hex');

afterEach(async () => {
  await Promise.all(temporary.splice(0).map(path => rm(path, { recursive: true, force: true })));
});

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), 'ssearch-marker-notice-'));
  temporary.push(root);
  await mkdir(join(root, 'tools'), { recursive: true });
  await mkdir(join(root, 'compatibility/upstream'), { recursive: true });
  await mkdir(join(root, 'node_modules'), { recursive: true });
  await mkdir(join(root, 'dist/assets'), { recursive: true });
  await cp(join(source, 'tools/collect-licenses.mjs'), join(root, 'tools/collect-licenses.mjs'));
  await cp(join(source, 'node_modules/client-only'), join(root, 'node_modules/client-only'), { recursive: true });
  const evidence = await readFile(join(source, 'compatibility/upstream/client-only-0.0.1-evidence.json'));
  await writeFile(join(root, 'compatibility/upstream/client-only-0.0.1-evidence.json'), evidence);
  const lock = JSON.stringify({ packages: {
    'node_modules/client-only': { version: '0.0.1', integrity, license: 'MIT' },
  } });
  await writeFile(join(root, 'package-lock.json'), lock);
  const script = 'console.log("synthetic application fixture");\n';
  await writeFile(join(root, 'dist/assets/browser.js'), script);
  // Synthetic telemetry validates the collector only; the real Vite build supplies its own receipt.
  const proof = {
    schema_version: 1, package: 'client-only', version: '0.0.1', source_sha: 'a'.repeat(40),
    lock_sha256: digest(lock), evidence_sha256: digest(evidence),
    telemetry: { graph_api: 'rolldown.getModuleIds', complete: true, module_count: 12,
      parsed_module_count: 12, application_entry_observed: true, output_modules_observed: true },
    package_modules: [{ id: 'node_modules/client-only/index.js', source_bytes: 0,
      source_sha256: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
      transformed_bytes: 0 }],
    rendered_modules: [] as Array<{ id: string; file: string; rendered_length: number }>,
    external_imports: [] as string[],
    package_assets: [] as string[],
    chunks: [{ file: 'assets/browser.js', bytes: Buffer.byteLength(script), sha256: digest(script) }],
  };
  const save = async () => {
    await mkdir(join(root, 'dist/licenses/client-only'), { recursive: true });
    await writeFile(join(root, 'dist/licenses/client-only/build-graph.json'), JSON.stringify(proof));
  };
  const collect = () => execute(process.execPath, [join(root, 'tools/collect-licenses.mjs')], { cwd: root });
  return { root, proof, save, collect };
}

it('refuses the missing-license marker without actual browser graph evidence', async () => {
  const f = await fixture();
  await expect(f.collect()).rejects.toThrow(/client-only build proof/);
});

it('accepts the exact empty browser marker and preserves truthful original metadata', async () => {
  const f = await fixture();
  await f.save();
  await f.collect();
  const inventory = JSON.parse(await readFile(join(f.root, 'dist/licenses/inventory.json'), 'utf8'));
  expect(inventory[0]).toMatchObject({
    package: 'client-only', version: '0.0.1', integrity,
    upstream_license_text_supplied: false, copyright_holder: null,
    source_bundle_disposition: 'no_client_only_implementation',
    browser_graph_case: 'observed_empty_default_entry',
    actual_package_acceptance: 'not_run',
  });
  expect(await readFile(join(f.root, 'dist/licenses/client-only/package.json')))
    .toEqual(await readFile(join(source, 'compatibility/upstream/client-only-0.0.1-package.json')));
  await expect(readFile(join(f.root, 'dist/licenses/client-only/error.js'))).rejects.toThrow();
});

it('distinguishes a complete graph with no marker from unsupported or incomplete telemetry', async () => {
  const f = await fixture();
  f.proof.package_modules = [];
  await f.save();
  await f.collect();
  const inventory = JSON.parse(await readFile(join(f.root, 'dist/licenses/inventory.json'), 'utf8'));
  expect(inventory[0].browser_graph_case).toBe('absent_from_complete_browser_graph');
  f.proof.telemetry.complete = false;
  await f.save();
  await expect(f.collect()).rejects.toThrow(/client-only build proof/);
});

it('refuses server branch, rendered implementation, external marker imports and package assets', async () => {
  for (const scenario of ['server', 'rendered', 'external', 'asset']) {
    const f = await fixture();
    if (scenario === 'server') f.proof.package_modules[0].id = 'node_modules/client-only/error.js';
    if (scenario === 'rendered') f.proof.rendered_modules = [{
      id: 'node_modules/client-only/index.js', file: 'assets/browser.js', rendered_length: 1,
    }];
    if (scenario === 'external') f.proof.external_imports = ['client-only'];
    if (scenario === 'asset') f.proof.package_assets = ['assets/client-only.js'];
    await f.save();
    await expect(f.collect()).rejects.toThrow(/client-only build proof/);
  }
});

it('keeps the exact entry/file/integrity guard and rejects every other missing-license package', async () => {
  for (const scenario of ['entry', 'file', 'integrity', 'other']) {
    const f = await fixture();
    await f.save();
    if (scenario === 'entry') await writeFile(join(f.root, 'node_modules/client-only/index.js'), 'export const changed = true;');
    if (scenario === 'file') await writeFile(join(f.root, 'node_modules/client-only/error.js'), 'changed');
    if (scenario === 'integrity') {
      const lock = JSON.parse(await readFile(join(f.root, 'package-lock.json'), 'utf8'));
      lock.packages['node_modules/client-only'].integrity = 'sha512-unknown';
      await writeFile(join(f.root, 'package-lock.json'), JSON.stringify(lock));
    }
    if (scenario === 'other') {
      const manifest = JSON.parse(await readFile(join(f.root, 'node_modules/client-only/package.json'), 'utf8'));
      manifest.name = 'unlicensed-fixture';
      await writeFile(join(f.root, 'node_modules/client-only/package.json'), JSON.stringify(manifest));
    }
    await expect(f.collect()).rejects.toThrow(/client-only source differs|No license text/);
  }
});
