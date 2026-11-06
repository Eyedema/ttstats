// Shared by the prefetch specs (KAN-124). Not a spec: no tests in here.
const { expect } = require('@playwright/test');

/** Every request prefetch.js made (it alone sends `Purpose: prefetch`). */
function watchPrefetches(page) {
  const seen = [];
  page.on('request', (req) => {
    if (req.headers()['purpose'] === 'prefetch') seen.push(req);
  });
  return seen;
}

function tab(page, path) {
  return page.locator(`nav[aria-label="Primary"] a[href="${path}"]`);
}

/**
 * Tap the tab for `path` and prove the navigation was answered by the
 * prefetch the tap started: exactly one prefetch went out, and the document
 * the browser shows carries `Cache-Control: private, max-age=5` -- a header
 * the server puts ONLY on a `Purpose: prefetch` response. A navigation that
 * went to the network itself would have none.
 *
 * (Not the prefetch's own Response object: Chromium hands the in-flight
 * fetch over to the navigation and the page that made it unloads, so
 * Playwright never sees that fetch complete.)
 */
async function tapTabExpectReuse(page, path, locator = tab(page, path)) {
  const prefetches = watchPrefetches(page);
  const [response] = await Promise.all([
    page.waitForNavigation({ url: `**${path}` }),
    locator.tap(),
  ]);
  await page.waitForLoadState('load');

  const forPath = prefetches.filter((r) => new URL(r.url()).pathname === path);
  expect(forPath, 'one prefetch for the tapped tab').toHaveLength(1);
  expect(response.status()).toBe(200);
  expect(
    (await response.allHeaders())['cache-control'],
    'the document shown is the prefetched response',
  ).toBe('private, max-age=5');
}

module.exports = { watchPrefetches, tab, tapTabExpectReuse };
