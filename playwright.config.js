// @ts-check
const { defineConfig, devices } = require('@playwright/test');

/**
 * End-to-end suite for the parts of the UI that pytest cannot see.
 *
 * pytest renders templates and asserts on the markup string; it cannot tell
 * you whether Alpine initialised, whether a drawer is actually on screen, or
 * what happens at a 390px viewport in Safari. Every gap that shipped the
 * fail-open mobile drawer lives in exactly that blind spot, so the projects
 * below are chosen to cover it:
 *
 *   - `iphone-safari`  real WebKit, real iPhone viewport, touch enabled.
 *                      This is the configuration the drawer bug reproduces in
 *                      and the one no desktop browser can stand in for.
 *   - `iphone-no-js`   the same, with JavaScript disabled. UI whose hidden
 *                      state depends on a framework booting must still be
 *                      hidden when it does not.
 *   - `iphone-reduced` reduced motion + reduced transparency, which are OS
 *                      settings a great many iPhone users actually have on.
 *   - `desktop-chrome` the wide layout, so the sidebar path stays covered.
 */
const path = require('path');

const PORT = process.env.E2E_PORT || 8125;
const BASE_URL = `http://localhost:${PORT}`;
const STORAGE_STATE = path.join(__dirname, 'tests/e2e/.auth/user.json');

module.exports = defineConfig({
  testDir: './tests/e2e',
  // The suite drives one shared Django dev server against one SQLite file;
  // parallel workers would race on the live-match fixture.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? 'list' : [['list'], ['html', { open: 'never' }]],

  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },

  projects: [
    // Logs in once and saves the session. The login view is rate limited to
    // 5 POSTs per IP per 15 minutes, so per-test login makes the suite fail
    // partway through for reasons that have nothing to do with the tests.
    // Runs in WebKit like the specs it feeds, so only one engine is required
    // to be installed for the default suite.
    { name: 'setup', testMatch: /auth\.setup\.js/, use: { ...devices['iPhone 13'] } },
    {
      name: 'iphone-safari',
      dependencies: ['setup'],
      testIgnore: [/.*\.nojs\.spec\.js/, /.*\.reduced\.spec\.js/, /.*\.desktop\.spec\.js/],
      use: { ...devices['iPhone 13'], storageState: STORAGE_STATE },
    },
    {
      name: 'iphone-no-js',
      dependencies: ['setup'],
      testMatch: /.*\.nojs\.spec\.js/,
      // Cookies still apply with scripting off, so the saved session works.
      use: {
        ...devices['iPhone 13'],
        storageState: STORAGE_STATE,
        javaScriptEnabled: false,
      },
    },
    {
      name: 'iphone-reduced',
      dependencies: ['setup'],
      testMatch: /.*\.reduced\.spec\.js/,
      use: {
        ...devices['iPhone 13'],
        storageState: STORAGE_STATE,
        reducedMotion: 'reduce',
      },
    },
    {
      name: 'desktop-chrome',
      dependencies: ['setup'],
      testMatch: /.*\.desktop\.spec\.js/,
      use: { ...devices['Desktop Chrome'], storageState: STORAGE_STATE },
    },
  ],

  // Playwright owns the server lifecycle so a stale one from a previous run
  // cannot silently serve old CSS. `dev` settings, never prod.
  webServer: {
    command: `bash scripts/e2e_server.sh ${PORT}`,
    url: `${BASE_URL}/accounts/login/`,
    reuseExistingServer: false,
    timeout: 120000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
});
