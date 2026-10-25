import { readdir, readFile, writeFile } from 'node:fs/promises';
import { resolve, join, relative } from 'node:path';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
const root = resolve(import.meta.dirname, '../dist');
const repository = resolve(root, '../..');
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const sourceSha = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repository, encoding: 'utf8' }).trim();
execFileSync('git', ['diff', '--exit-code', 'HEAD', '--'], { cwd: repository });
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
    for (const forbidden of ['Учебные документы', 'mockService', '947e61b4-5200-4f7e-8bbc-ced7be362d7b', 'c8b0b9c4-d68a-4c20-a9da-031df6728856']) if (text.includes(forbidden)) throw new Error(`Development fixture in production: ${name}`);
    if (/(?:src|href)\s*=\s*["'](?:https?:)?\/\//i.test(text) || /@import\s*["']https?:|url\(["']?https?:/i.test(text)) throw new Error(`Remote asset in ${name}`);
    if (/from\s*["'](?:electron|node:)|require\(["'](?:electron|node:)/.test(text)) throw new Error(`Host import in renderer: ${name}`);
  }
  inventory.push({ file: name, sha256: sha256(bytes), bytes: bytes.length });
}
for (const required of ['index.html', 'app-icon.png', 'LICENSE.txt', 'THIRD_PARTY_NOTICES.txt']) if (!inventory.some(item => item.file === required)) throw new Error(`Missing local asset: ${required}`);
const npmVersion = execFileSync(process.execPath, [process.env.npm_execpath, '--version'], { encoding: 'utf8' }).trim();
await writeFile(join(root, 'asset-inventory.json'), JSON.stringify({
  schema_version: 1,
  source_sha: sourceSha,
  node_version: process.versions.node,
  npm_version: npmVersion,
  candidate_sha256: sha256(await readFile(join(repository, 'frontend/compatibility/candidate-versions.json'))),
  locks,
  assets: inventory,
}, null, 2));
console.log(`Verified ${inventory.length} local production assets; development fixtures excluded.`);
