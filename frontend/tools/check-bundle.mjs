import { readdir, readFile, writeFile } from 'node:fs/promises';
import { resolve, join, relative } from 'node:path';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
const root = resolve(import.meta.dirname, '../dist');
const repository = resolve(root, '../..');
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const sourceSha = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repository, encoding: 'utf8' }).trim();
execFileSync('git', ['diff', '--exit-code', 'HEAD', '--'], { cwd: repository });
const markerProof = JSON.parse(await readFile(join(root, 'licenses/client-only/build-graph.json'), 'utf8'));
if (markerProof.source_sha !== sourceSha) throw new Error('client-only build proof source SHA mismatch');
const candidate = JSON.parse(await readFile(join(repository, 'frontend/compatibility/candidate-versions.json'), 'utf8'));
const locks = [];
for (const path of ['frontend/package-lock.json', 'desktop/package-lock.json']) {
  const digest = sha256(await readFile(join(repository, path)));
  if (candidate.locks.find(lock => lock.path === path)?.sha256 !== digest) throw new Error(`Candidate lock mismatch: ${path}`);
  locks.push({ path, sha256: digest });
}
const html = await readFile(join(root, 'index.html'), 'utf8');
const csp = html.match(/http-equiv="Content-Security-Policy"\s+content="([^"]+)"/i)?.[1];
if (!csp || !csp.includes("connect-src 'none'") || !csp.includes("script-src 'self';") || /script-src[^;]*'unsafe-inline'/.test(csp)) throw new Error('Production CSP must deny network connections and inline scripts');
async function files(dir) { return (await Promise.all((await readdir(dir, { withFileTypes: true })).map(entry => entry.isDirectory() ? files(join(dir, entry.name)) : [join(dir, entry.name)]))).flat(); }
const inventory = [];
for (const file of await files(root)) {
  const bytes = await readFile(file); const name = relative(root, file).replaceAll('\\', '/');
  if (/\.(js|html|css)$/.test(file)) {
    const text = bytes.toString('utf8');
    for (const forbidden of ['Учебные документы', 'Учебные данные.', 'Предпросмотр: Проводник', 'mockService', '947e61b4-5200-4f7e-8bbc-ced7be362d7b', 'c8b0b9c4-d68a-4c20-a9da-031df6728856']) if (text.includes(forbidden)) throw new Error(`Development fixture in production: ${name}`);
    if (/(?:src|href)\s*=\s*["'](?:https?:)?\/\//i.test(text) || /@import\s*["']https?:|url\(["']?https?:/i.test(text)) throw new Error(`Remote asset in ${name}`);
    if (/from\s*["'](?:electron|node:)|require\(["'](?:electron|node:)/.test(text)) throw new Error(`Host import in renderer: ${name}`);
  }
  inventory.push({ file: name, sha256: sha256(bytes), bytes: bytes.length });
}
for (const required of ['index.html', 'app-icon.png', 'LICENSE.txt', 'THIRD_PARTY_NOTICES.txt']) if (!inventory.some(item => item.file === required)) throw new Error(`Missing local asset: ${required}`);
const browserScripts = inventory.filter(item => item.file.endsWith('.js') && !item.file.startsWith('licenses/'));
if (!Array.isArray(markerProof.chunks) || markerProof.chunks.length !== browserScripts.length
  || new Set(markerProof.chunks.map(chunk => chunk.file)).size !== browserScripts.length
  || browserScripts.some(script => !markerProof.chunks.some(chunk => chunk.file === script.file
    && chunk.bytes === script.bytes && chunk.sha256 === script.sha256))) {
  throw new Error('client-only build proof does not cover the current browser chunks');
}
if (markerProof.lock_sha256 !== locks.find(lock => lock.path === 'frontend/package-lock.json').sha256
  || markerProof.evidence_sha256 !== sha256(await readFile(join(repository,
    'frontend/compatibility/upstream/client-only-0.0.1-evidence.json')))) {
  throw new Error('client-only build proof input hashes mismatch');
}
const npmVersion = execFileSync(process.execPath, [process.env.npm_execpath, '--version'], { encoding: 'utf8' }).trim();
const offlineDependencies = JSON.parse(await readFile(join(repository,
  'frontend/.cache/offline-dependencies.json'), 'utf8'));
const offlineNegative = JSON.parse(await readFile(join(repository,
  'frontend/.cache/offline-negative.json'), 'utf8'));
if (offlineDependencies.source_sha !== sourceSha) throw new Error('Offline proof source SHA mismatch');
for (const project of ['frontend', 'desktop']) {
  const receipt = offlineDependencies.projects.find(item => item.project === project);
  const digest = locks.find(item => item.path === `${project}/package-lock.json`).sha256;
  if (!receipt?.offline_npm_ci || !receipt.cache_integrity_verified || receipt.install_scripts
    || receipt.lock_sha256 !== digest) throw new Error(`Missing locked offline proof: ${project}`);
}
if (offlineNegative.length !== 2 || offlineNegative.some(item =>
  !item.rejected_before_npm_ci || item.package_downloads)) throw new Error('Missing cache-negative proof');
await writeFile(join(root, 'asset-inventory.json'), JSON.stringify({
  schema_version: 1,
  source_sha: sourceSha,
  node_version: process.versions.node,
  npm_version: npmVersion,
  candidate_sha256: sha256(await readFile(join(repository, 'frontend/compatibility/candidate-versions.json'))),
  locks,
  offline_dependencies: offlineDependencies,
  offline_cache_negative: offlineNegative,
  client_only_build_proof: {
    file: 'licenses/client-only/build-graph.json',
    sha256: sha256(await readFile(join(root, 'licenses/client-only/build-graph.json'))),
    source_sha: markerProof.source_sha,
  },
  assets: inventory,
}, null, 2));
console.log(`Verified ${inventory.length} local production assets; development fixtures excluded.`);
