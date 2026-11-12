/**
 * KAN-136 background warm-up, for the offline specs.
 *
 * Every e2e-reader page load may make the worker fetch and store up to six
 * pages in the background. A spec that counts what is saved turns that off
 * with `noWarming(page)`: it reports Save-Data, which snapshot_warm.js
 * honours (WebKit has no navigator.connection of its own). Nothing else in
 * those specs reads Save-Data except prefetch.js, which they don't exercise.
 */
async function noWarming(page) {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'connection', {
      configurable: true,
      value: { saveData: true },
    });
  });
}

module.exports = { noWarming };
