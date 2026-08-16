#!/usr/bin/env node
// Copy the browser builds of our JS dependencies out of node_modules and into
// the static tree, so pages load them from 'self' instead of a CDN.
//
// The copies are gitignored and rebuilt by `npm run vendor` -- same contract as
// the compiled CSS. Pinning happens in package-lock.json rather than in a URL.

import { mkdirSync, copyFileSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const vendorJs = join(root, 'ttstats/pingpong/static/pingpong/js/vendor');
const vendorCss = join(root, 'ttstats/pingpong/static/pingpong/css/vendor');

const assets = [
  ['htmx.org/dist/htmx.min.js', vendorJs, 'htmx.min.js'],
  ['alpinejs/dist/cdn.min.js', vendorJs, 'alpine.min.js'],
  ['chart.js/dist/chart.umd.js', vendorJs, 'chart.umd.js'],
  ['tom-select/dist/js/tom-select.complete.min.js', vendorJs, 'tom-select.complete.min.js'],
  [
    'tom-select/dist/css/tom-select.bootstrap5.min.css',
    vendorCss,
    'tom-select.bootstrap5.min.css',
  ],
];

mkdirSync(vendorJs, { recursive: true });
mkdirSync(vendorCss, { recursive: true });

for (const [source, destDir, name] of assets) {
  const from = join(root, 'node_modules', source);
  const to = join(destDir, name);
  copyFileSync(from, to);
  console.log(`  ${name} (${statSync(to).size} bytes)`);
}

console.log(`vendored ${assets.length} asset(s)`);
