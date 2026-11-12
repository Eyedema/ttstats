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
 *   - `iphone-dark`    the palette's BASE. Dark is not the alternate theme in
 *                      this design -- light is the override -- so the default
 *                      light rendering a developer sees on a desktop is the
 *                      variant, not the norm. A spec suite that only ever ran
 *                      in light mode would be testing the exception.
 *   - `desktop-chrome` the wide layout, so the sidebar path stays covered.
 *   - `android-chrome` / `android-firefox`  Pixel 7 Chromium, and Gecko at a
 *                      phone viewport with touch (Playwright's Firefox has no
 *                      isMobile, so an approximation). `*.android.spec.js`;
 *                      not in CI, which installs only WebKit.
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
  // One retry on CI only: GitHub's WebKit runner stalls on a random test now
  // and then (a click whose "stable" check never resolves on a loaded page),
  // which never reproduces locally. A test that needs the retry is still
  // reported as flaky in the list output; locally a failure stays a failure.
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'list' : [['list'], ['html', { open: 'never' }]],

  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    // Service workers are BLOCKED in every project unless it opts in, and
    // only `iphone-pwa` does. Once a worker controls the page, page.route()
    // -- and so applyProdCSP() -- stops seeing its requests: in WebKit for
    // every subresource and POST, worker fetch handler or not; in Chromium
    // and Gecko as soon as a route calls respondWith(). The rule is then
    // silently skipped and the request goes to the server, so a spec that
    // aborts /live/point/ fails looking for an error it never provoked, and a
    // CSP spec passes against a page that had no policy. It is a race against
    // how fast the worker claims the page, so it fails in CI and passes
    // locally. Measured per engine: docs/decisions/kan-76-sw-e2e-audit.md.
    //
    // Blocking costs no fidelity outside `iphone-pwa`: the worker answers no
    // request itself today and only exists for push.
    serviceWorkers: 'block',
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
      testIgnore: [
        /.*\.nojs\.spec\.js/,
        /.*\.reduced\.spec\.js/,
        /.*\.desktop\.spec\.js/,
        /.*\.dark\.spec\.js/,
        /.*\.android\.spec\.js/,
        // The PWA specs need a live service worker; see `iphone-pwa`.
        /pwa\.spec\.js/,
      ],
      // Workers blocked (top-level `use`): the scoreboard specs abort and
      // delay /live/point/ with page.route().
      use: { ...devices['iPhone 13'], storageState: STORAGE_STATE },
    },
    {
      // The mirror image: the specs that are *about* the service worker, so
      // it has to be allowed to register here -- the ONLY project that does.
      // A spec here that needs page.route() / applyProdCSP() either blocks
      // workers for itself (`test.use({ serviceWorkers: 'block' })`, as
      // pull-to-refresh-cases.js does) or proves its route applied (as
      // pwa.spec.js's CSP block does). Offline specs (KAN-82) belong here.
      name: 'iphone-pwa',
      dependencies: ['setup'],
      testMatch: /pwa\.spec\.js/,
      use: {
        ...devices['iPhone 13'],
        storageState: STORAGE_STATE,
        serviceWorkers: 'allow',
      },
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
      name: 'iphone-dark',
      dependencies: ['setup'],
      testMatch: /.*\.dark\.spec\.js/,
      use: {
        ...devices['iPhone 13'],
        storageState: STORAGE_STATE,
        colorScheme: 'dark',
      },
    },
    {
      // Chrome for Android, as far as Playwright can stand in for it:
      // Chromium with the Pixel 7 profile (mobile viewport, touch, isMobile).
      name: 'android-chrome',
      dependencies: ['setup'],
      testMatch: /.*\.android\.spec\.js/,
      use: { ...devices['Pixel 7'], storageState: STORAGE_STATE },
    },
    {
      // Firefox for Android, approximately: Playwright's Firefox has no
      // isMobile, so this is desktop Gecko at a phone viewport with touch.
      // It exercises the engine (touch events, CSS, CSP), not Fenix's shell.
      name: 'android-firefox',
      dependencies: ['setup'],
      testMatch: /.*\.android\.spec\.js/,
      use: {
        browserName: 'firefox',
        viewport: { width: 412, height: 915 },
        hasTouch: true,
        storageState: STORAGE_STATE,
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
