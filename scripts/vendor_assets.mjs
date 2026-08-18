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
const vendorFonts = join(root, 'ttstats/pingpong/static/pingpong/fonts');

// Source maps are not optional extras here. WhiteNoise's manifest storage
// resolves every sourceMappingURL a vendored file declares, and hard-fails
// collectstatic -- and therefore the deploy -- if the target is missing.
const assets = [
  ['htmx.org/dist/htmx.min.js', vendorJs, 'htmx.min.js'],
  ['alpinejs/dist/cdn.min.js', vendorJs, 'alpine.min.js'],
  ['chart.js/dist/chart.umd.js', vendorJs, 'chart.umd.js'],
  ['chart.js/dist/chart.umd.js.map', vendorJs, 'chart.umd.js.map'],
  ['tom-select/dist/js/tom-select.complete.min.js', vendorJs, 'tom-select.complete.min.js'],
  [
    'tom-select/dist/js/tom-select.complete.min.js.map',
    vendorJs,
    'tom-select.complete.min.js.map',
  ],
  [
    'tom-select/dist/css/tom-select.bootstrap5.min.css',
    vendorCss,
    'tom-select.bootstrap5.min.css',
  ],
  [
    'tom-select/dist/css/tom-select.bootstrap5.min.css.map',
    vendorCss,
    'tom-select.bootstrap5.min.css.map',
  ],
  // Archivo (OFL), the app's only typeface. Self-hosted because prod's CSP
  // allows no external hosts at all -- a Google Fonts link would silently not
  // load and the whole design would fall back to system-ui. Three weights,
  // matching the type scale in tailwind.config.js; app.css @font-face's them
  // from ../fonts relative to the compiled stylesheet.
  ['@fontsource/archivo/files/archivo-latin-400-normal.woff2', vendorFonts, 'archivo-400.woff2'],
  ['@fontsource/archivo/files/archivo-latin-600-normal.woff2', vendorFonts, 'archivo-600.woff2'],
  ['@fontsource/archivo/files/archivo-latin-800-normal.woff2', vendorFonts, 'archivo-800.woff2'],
];

mkdirSync(vendorJs, { recursive: true });
mkdirSync(vendorCss, { recursive: true });
mkdirSync(vendorFonts, { recursive: true });

for (const [source, destDir, name] of assets) {
  const from = join(root, 'node_modules', source);
  const to = join(destDir, name);
  copyFileSync(from, to);
  console.log(`  ${name} (${statSync(to).size} bytes)`);
}

console.log(`vendored ${assets.length} asset(s)`);
