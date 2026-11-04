const { test: setup } = require('@playwright/test');
const path = require('path');

const AUTH_FILE = path.join(__dirname, '.auth', 'user.json');
// The seasons user (KAN-48/50) has the `seasons` flag on by override; every
// other spec keeps the flag-off UI. Two logins per run, still well inside the
// limit -- and the dev server's LocMemCache restarts with every run.
const SEASONS_AUTH_FILE = path.join(__dirname, '.auth', 'seasons.json');

async function authenticate(page, username, file) {
  await page.goto('/accounts/login/');
  await page.fill('input[name="username"]', username);
  await page.fill('input[name="password"]', 'e2e-local-only');
  await Promise.all([
    page.waitForURL(/\/pingpong\//),
    page.click('button[type="submit"]'),
  ]);
  await page.context().storageState({ path: file });
}

/**
 * Log in exactly once per run and save the session for every other spec.
 *
 * Not just a speed optimisation: the login view is rate limited to 5 POSTs per
 * IP per 15 minutes, so a suite that logs in per test starts failing at the
 * sixth test with a timeout that looks nothing like its real cause.
 */
setup('authenticate', async ({ page }) => {
  await authenticate(page, 'e2e', AUTH_FILE);
});

setup('authenticate the seasons user', async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await authenticate(page, 'e2e-seasons', SEASONS_AUTH_FILE);
  await context.close();
});

module.exports = { AUTH_FILE, SEASONS_AUTH_FILE };
