const { expect } = require('@playwright/test');

/** A Content-Security-Policy with no 'unsafe-eval'.
 *
 *  Dev sends no CSP at all, which is why the mobile drawer could be broken in
 *  production while pytest, a manual browser pass and this entire suite were
 *  green. Specs that exercise interactive behaviour should apply it.
 *
 *  This is deliberately STRICTER than what prod.py now sends: production had
 *  to add 'unsafe-eval' so the Alpine-based scoreboard works at all. Keeping
 *  it out here means the drawer -- which is plain JS precisely so it does not
 *  need eval -- cannot quietly regress onto a framework expression and take
 *  the whole navigation down again. If you find yourself adding 'unsafe-eval'
 *  to this constant to make a test pass, that is the regression. */
const PROD_CSP = [
  "script-src 'self' 'unsafe-inline'",
  "frame-ancestors 'none'",
  "connect-src 'self'",
  "default-src 'self'",
  "img-src 'self' data:",
  "style-src 'self' 'unsafe-inline'",
  "form-action 'self'",
  "font-src 'self'",
  // The service worker and the web app manifest. Both would fall through to
  // default-src and happen to work, but naming them here means a future
  // default-src change cannot silently kill push notifications -- and on iOS,
  // a blocked manifest means no home-screen install, which means no push at
  // all.
  "worker-src 'self'",
  "manifest-src 'self'",
].join('; ');

/** Serve every response with the production CSP. */
async function applyProdCSP(page) {
  await page.route('**/*', async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      response,
      headers: { ...response.headers(), 'content-security-policy': PROD_CSP },
    });
  });
}

const E2E_USERNAME = 'e2e';
const E2E_PASSWORD = 'e2e-local-only';

/** Log in through the real form. Throwaway local-only account, seeded by
 *  `manage.py seed_e2e`, which refuses to run outside DEBUG. */
async function login(page) {
  await page.goto('/accounts/login/');
  await page.fill('input[name="username"]', E2E_USERNAME);
  await page.fill('input[name="password"]', E2E_PASSWORD);
  await Promise.all([
    page.waitForURL(/\/pingpong\//),
    page.click('button[type="submit"]'),
  ]);
}

/** The mobile drawer panel. Identified by role so the spec does not depend on
 *  the Tailwind class soup, which changes for visual reasons. */
function drawer(page) {
  return page.locator('#mobile-menu-panel');
}

function drawerScrim(page) {
  return page.locator('#mobile-menu-scrim');
}

/** Assert that nothing belonging to the drawer is covering the page.
 *  Checks the scrim too: a transparent-but-present scrim swallows every tap
 *  on the page underneath, which is its own flavour of "can't be closed". */
async function expectDrawerClosed(page) {
  await expect(drawer(page)).toBeHidden();
  await expect(drawerScrim(page)).toBeHidden();
}

/** Wait until the drawer has finished sliding.
 *
 *  `toBeVisible()` resolves the instant display stops being none -- which is
 *  the *start* of the 300ms enter transition, with the panel still translated
 *  fully off-screen. Measuring geometry there reports -256 and looks like a
 *  bug in the app rather than in the test. */
async function waitForDrawerSettled(page) {
  await expect
    .poll(async () => {
      const box = await drawer(page).boundingBox();
      return box ? Math.round(box.x) : null;
    }, { timeout: 2000 })
    .toBe(0);
}

module.exports = {
  PROD_CSP,
  applyProdCSP,
  waitForDrawerSettled,
  E2E_USERNAME,
  E2E_PASSWORD,
  login,
  drawer,
  drawerScrim,
  expectDrawerClosed,
};
