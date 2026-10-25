import { build } from 'esbuild';
import { copyFile, writeFile } from 'node:fs/promises';
import { icons } from '@awesome.me/webawesome/dist/components/icon/library.system.js';

const output = 'src/applypilot/web/static';
const symbols = Object.entries(icons).flatMap(([variant, collection]) => Object.entries(collection).map(([name, svg]) =>
  svg.replace('<svg ', `<symbol id="${variant}-${name}" `).replace('</svg>', '</symbol>')));
await writeFile(`${output}/webawesome-icons.svg`, `<svg xmlns="http://www.w3.org/2000/svg">${symbols.join('\n')}</svg>\n`);
await build({
  entryPoints: ['src/applypilot/web/frontend/components.js'],
  outfile: `${output}/components.js`, bundle: true, minify: true, format: 'esm',
  target: ['es2022'], legalComments: 'linked', metafile: true,
}).then(result => writeFile(`${output}/components-manifest.json`, JSON.stringify({
  entry: 'frontend/components.js', libraries: { webawesome: '3.14.0', lit: '3.3.3' },
  components: Object.keys(result.metafile.inputs).filter(path => /\/components\/[^/]+\/[^/]+\.js$/.test(path)),
}, null, 2) + '\n'));
await build({
  entryPoints: ['node_modules/@awesome.me/webawesome/dist/styles/webawesome.css'],
  outfile: `${output}/webawesome.css`, bundle: true, minify: true, legalComments: 'linked',
});
await copyFile('node_modules/@awesome.me/webawesome/LICENSE.md', `${output}/webawesome-LICENSE.md`);
await copyFile('node_modules/lit/LICENSE', `${output}/lit-LICENSE.txt`);
