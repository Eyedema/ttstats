const { definePullToRefreshCases } = require('./pull-to-refresh-cases');

/**
 * Pull to refresh in the app installed from Chrome for Android and Firefox
 * for Android (KAN-126). Runs in `android-chrome` (Chromium, Pixel 7) and
 * `android-firefox` (desktop Gecko at a phone viewport with touch: Playwright
 * has no Firefox mobile emulation, so this approximates Fenix's engine, not
 * its shell). Same cases as the iPhone spec.
 */
definePullToRefreshCases();
