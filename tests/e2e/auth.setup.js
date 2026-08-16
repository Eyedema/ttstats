const { test: setup } = require('@playwright/test');
const path = require('path');

const AUTH_FILE = path.join(__dirname, '.auth', 'user.json');

/**
 * Log in exactly once per run and save the session for every other spec.
 *
 * Not just a speed optimisation: the login view is rate limited to 5 POSTs per
 * IP per 15 minutes, so a suite that logs in per test starts failing at the
 * sixth test with a timeout that looks nothing like its real cause.
 */
setup('authenticate', async ({ page }) => {
  await page.goto('/accounts/login/');
  await page.fill('input[name="username"]', 'e2e');
  await page.fill('input[name="password"]', 'e2e-local-only');
  await Promise.all([
    page.waitForURL(/\/pingpong\//),
    page.click('button[type="submit"]'),
  ]);
  await page.context().storageState({ path: AUTH_FILE });
});

module.exports = { AUTH_FILE };
