const { cp, access, readFile, writeFile } = require('node:fs/promises');
const { readFileSync, lstatSync, realpathSync } = require('node:fs');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const { readProductMetadata, stampPackageManifest, verifyElectronZip, verifyServiceStage } = require('./product-metadata.cjs');

const metadataPath = process.env.SSEARCH_PRODUCT_METADATA;
const sourceCommit = execFileSync('git', ['rev-parse', 'HEAD'], {
  cwd: path.resolve(__dirname, '..'), encoding: 'utf8',
}).trim();
const manifest = JSON.parse(readFileSync(path.join(__dirname, 'package.json'), 'utf8'));
const lock = JSON.parse(readFileSync(path.join(__dirname, 'package-lock.json'), 'utf8'));
const metadata = readProductMetadata(metadataPath, { sourceCommit, manifest, lock });
if (path.basename(metadataPath) !== 'product-metadata.json') throw new Error('product_metadata_invalid');
const electronZipDir = verifyElectronZip(process.env.SSEARCH_ELECTRON_ZIP_DIR,
  manifest.devDependencies.electron, process.env.SSEARCH_ELECTRON_ZIP_SHA256);
const serviceStage = verifyServiceStage(process.env.SSEARCH_SERVICE_STAGE_DIR);

/** Package folder only. Inno and P8 own installation and updates. */
module.exports = {
  packagerConfig: {
    asar: true,
    executableName: 'ssearch',
    icon: path.resolve(__dirname, 'assets/ssearch.ico'),
    name: metadata.product.product_name,
    appVersion: metadata.app_version,
    buildVersion: metadata.app_version,
    appCopyright: metadata.product.copyright_holder,
    extraResource: [metadataPath],
    electronZipDir,
    ignore: [
      /^\/tests($|\/)/, /^\/src($|\/)/, /^\/out($|\/)/, /^\/\.cache($|\/)/, /^\/artifacts($|\/)/,
      /^\/(?:forge\.config\.cjs|product-metadata\.cjs|tsconfig(?:\.proof)?\.json)$/,
    ],
  },
  hooks: {
    prePackage: async (_config, platform, arch) => {
      if (platform !== 'win32' || arch !== 'x64') throw new Error('package_target_unsupported');
      verifyElectronZip(electronZipDir, manifest.devDependencies.electron, process.env.SSEARCH_ELECTRON_ZIP_SHA256);
      verifyServiceStage(serviceStage);
    },
    readPackageJson: async (_config, packageJson) => stampPackageManifest(packageJson, metadata),
    packageAfterCopy: async (_config, buildPath) => {
      const renderer = path.resolve(__dirname, '../frontend/dist');
      await access(path.join(renderer, 'index.html'));
      await cp(renderer, path.join(buildPath, 'renderer'), { recursive: true });
      const manifestPath = path.join(buildPath, 'package.json');
      const copiedManifest = JSON.parse(await readFile(manifestPath, 'utf8'));
      await writeFile(manifestPath, JSON.stringify(stampPackageManifest(copiedManifest, metadata), null, 2) + '\n');
    },
    postPackage: async (_config, { platform, arch, outputPaths }) => {
      if (platform !== 'win32' || arch !== 'x64') throw new Error('package_target_unsupported');
      if (!Array.isArray(outputPaths) || outputPaths.length === 0) throw new Error('package_output_invalid');
      verifyServiceStage(serviceStage);
      for (const outputPath of outputPaths) {
        if (!path.isAbsolute(outputPath) || !lstatSync(outputPath).isDirectory()
          || lstatSync(outputPath).isSymbolicLink()
          || realpathSync(outputPath).toLowerCase() !== path.resolve(outputPath).toLowerCase()) throw new Error('package_output_invalid');
        const target = path.join(outputPath, 'service');
        try {
          lstatSync(target);
          throw new Error('package_service_already_exists');
        } catch (error) { if (error.code !== 'ENOENT') throw error; }
        await cp(serviceStage, target, { recursive: true, dereference: false, force: false,
          errorOnExist: true, preserveTimestamps: true });
        verifyServiceStage(target);
      }
    },
  },
};
