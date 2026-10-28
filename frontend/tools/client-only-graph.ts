import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import type { Plugin, ResolvedConfig } from 'vite';

const sha256 = (bytes: Uint8Array | string) => createHash('sha256').update(bytes).digest('hex');
const marker = (id: string) => {
  const normalized = id.replaceAll('\\', '/');
  return normalized === 'client-only' || normalized.startsWith('client-only/')
    || /(?:^|\/)node_modules\/client-only(?:\/|$)/.test(normalized)
    || /(?:^|\/)client-only\.(?:js|mjs|cjs)$/.test(normalized);
};

/** Observe the real browser build; never resolve, alias, load or rewrite modules. */
export function clientOnlyGraph(projectRoot: string): Plugin {
  const root = resolve(projectRoot).replaceAll('\\', '/');
  const relativeId = (id: string) => {
    const normalized = id.replaceAll('\\', '/');
    return normalized.startsWith(`${root}/`) ? normalized.slice(root.length + 1) : normalized;
  };
  let config: ResolvedConfig;
  let parsedModules = 0;
  const parsedMarkerModules = new Map<string, number | null>();
  return {
    name: 'ssearch-client-only-provenance', apply: 'build', enforce: 'post',
    configResolved(resolved) { config = resolved; },
    buildStart() {
      parsedModules = 0;
      parsedMarkerModules.clear();
    },
    moduleParsed(info) {
      parsedModules++;
      if (marker(info.id)) {
        parsedMarkerModules.set(info.id, typeof info.code === 'string' ? Buffer.byteLength(info.code) : null);
      }
    },
    generateBundle(_options, bundle) {
      if (config.build.ssr || typeof this.getModuleIds !== 'function') {
        throw new Error('client-only browser graph observation is unsupported');
      }
      const ids = Array.from(this.getModuleIds());
      const packageModules = ids.filter(marker).map(id => {
        const entry = relativeId(id);
        let bytes: Buffer | null = null;
        // Only an ordinary on-disk module is read. Virtual/query/external IDs stay unknown.
        if (entry === 'node_modules/client-only/index.js' || entry === 'node_modules/client-only/error.js') {
          bytes = readFileSync(resolve(projectRoot, entry));
        }
        return {
          id: entry, module_parsed: parsedMarkerModules.has(id),
          source_bytes: bytes?.length ?? null, source_sha256: bytes === null ? null : sha256(bytes),
          transformed_bytes: parsedMarkerModules.get(id) ?? null,
        };
      });
      const renderedModules: Array<{ id: string; file: string; rendered_length: number }> = [];
      const externalImports = new Set<string>();
      const packageAssets: string[] = [];
      const chunks: Array<{ file: string; bytes: number; sha256: string }> = [];
      let outputModulesObserved = true;
      let renderedModuleCount = 0;
      for (const output of Object.values(bundle)) {
        if (output.type === 'chunk') {
          if (!output.modules || typeof output.modules !== 'object') outputModulesObserved = false;
          for (const [id, module] of Object.entries(output.modules ?? {})) {
            renderedModuleCount++;
            if (marker(id)) renderedModules.push({
              id: relativeId(id), file: output.fileName, rendered_length: module.renderedLength,
            });
          }
          for (const imported of [...output.imports, ...output.dynamicImports]) {
            if (marker(imported)) externalImports.add(relativeId(imported));
          }
          chunks.push({ file: output.fileName, bytes: Buffer.byteLength(output.code), sha256: sha256(output.code) });
        } else {
          if (!Array.isArray(output.originalFileNames)) {
            throw new Error('client-only asset origin observation is unsupported');
          }
          if (marker(output.fileName) || output.originalFileNames.some(marker)) packageAssets.push(output.fileName);
        }
      }
      const complete = ids.length > 0 && parsedModules > 0 && chunks.length > 0
        && renderedModuleCount > 0 && outputModulesObserved;
      const proof = {
        schema_version: 1, package: 'client-only', version: '0.0.1',
        source_sha: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: projectRoot, encoding: 'utf8' }).trim(),
        lock_sha256: sha256(readFileSync(resolve(projectRoot, 'package-lock.json'))),
        evidence_sha256: sha256(readFileSync(resolve(projectRoot, 'compatibility/upstream/client-only-0.0.1-evidence.json'))),
        resolve_conditions: config.resolve.conditions,
        telemetry: {
          graph_api: 'rolldown.getModuleIds', complete, module_count: ids.length,
          parsed_module_count: parsedModules,
          application_entry_observed: ids.some(id => relativeId(id).split('?')[0] === 'src/main.tsx'),
          output_modules_observed: outputModulesObserved && renderedModuleCount > 0,
        },
        package_modules: packageModules,
        rendered_modules: renderedModules,
        external_imports: Array.from(externalImports),
        package_assets: packageAssets,
        chunks,
      };
      this.emitFile({
        type: 'asset', fileName: 'licenses/client-only/build-graph.json',
        source: JSON.stringify(proof, null, 2),
      });
    },
  };
}
