import { readFile, readdir, mkdir, copyFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
const root = resolve(import.meta.dirname, '..');
const lock = JSON.parse(await readFile(join(root, 'package-lock.json'), 'utf8'));
const inventory = [];
for (const [location, entry] of Object.entries(lock.packages)) {
  if (!location || entry.dev || !location.includes('node_modules/')) continue;
  let manifest;
  try { manifest = JSON.parse(await readFile(join(root, location, 'package.json'), 'utf8')); }
  catch { if (entry.optional) continue; throw new Error(`Missing production dependency ${location}`); }
  const destination = join(root, 'dist/licenses', location.replaceAll('node_modules/', ''));
  await mkdir(destination, { recursive: true });
  const licenses = (await readdir(join(root, location))).filter(name => /^(license|licence|copying|notice)(\.|$|-)/i.test(name));
  if (!licenses.length) throw new Error(`No license text in ${location}`);
  for (const name of licenses) await copyFile(join(root, location, name), join(destination, name));
  inventory.push({ package: manifest.name, version: manifest.version, license: manifest.license, integrity: entry.integrity, texts: licenses });
}
await writeFile(join(root, 'dist/licenses/inventory.json'), JSON.stringify(inventory, null, 2));
