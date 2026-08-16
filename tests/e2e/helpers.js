const { expect } = require('@playwright/test');

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
  waitForDrawerSettled,
  E2E_USERNAME,
  E2E_PASSWORD,
  login,
  drawer,
  drawerScrim,
  expectDrawerClosed,
};
