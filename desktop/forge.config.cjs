const { cp, access } = require('node:fs/promises');
const path = require('node:path');
/** Package folder only. Inno and P8 own installation and updates. */
module.exports = {
  packagerConfig: { asar: true, executableName: 'ssearch', icon: path.resolve(__dirname, 'assets/ssearch.ico'),
    ignore: [/^\/tests($|\/)/, /^\/src($|\/)/, /^\/out($|\/)/, /^\/\.cache($|\/)/, /^\/artifacts($|\/)/] },
  hooks: { packageAfterCopy: async (_config, buildPath) => {
    const renderer = path.resolve(__dirname, '../frontend/dist');
    await access(path.join(renderer, 'index.html'));
    await cp(renderer, path.join(buildPath, 'renderer'), { recursive: true });
  } },
};
