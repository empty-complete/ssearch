import { readdir, readFile, writeFile } from 'node:fs/promises';
import { resolve, join, relative } from 'node:path';
import { createHash } from 'node:crypto';
const root = resolve(import.meta.dirname, '../dist');
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
  inventory.push({ file: name, sha256: createHash('sha256').update(bytes).digest('hex'), bytes: bytes.length });
}
for (const required of ['index.html', 'app-icon.png', 'LICENSE.txt', 'THIRD_PARTY_NOTICES.txt']) if (!inventory.some(item => item.file === required)) throw new Error(`Missing local asset: ${required}`);
await writeFile(join(root, 'asset-inventory.json'), JSON.stringify({ schema_version: 1, assets: inventory }, null, 2));
console.log(`Verified ${inventory.length} local production assets; development fixtures excluded.`);
