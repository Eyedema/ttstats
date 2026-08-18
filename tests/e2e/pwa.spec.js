const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');

/**
 * The PWA layer: manifest, icons, service worker, and the notification panel.
 *
 * pytest asserts that these views return the right bytes. It cannot tell you
 * whether a real browser accepts the manifest, whether the worker registers
 * with a usable scope, or whether the production CSP blocks either -- and a
 * blocked manifest on iOS means no home-screen install, which means no push
 * notifications at all. That is exactly the class of failure that shipped the
 * broken mobile drawer, so it gets a browser test.
 */

test.describe('PWA install surface', () => {
  test('the manifest is linked and parses', async ({ page }) => {
    await page.goto('/pingpong/');

    const href = await page
      .locator('link[rel="manifest"]')
      .getAttribute('href');
    expect(href).toBe('/manifest.webmanifest');

    const response = await page.request.get(href);
    expect(response.status()).toBe(200);
    const manifest = await response.json();
    expect(manifest.display).toBe('standalone');
    expect(manifest.start_url).toBe('/pingpong/');
  });

  test('every declared icon actually exists', async ({ page }) => {
    // A 404 icon does not fail the manifest; it just gives you a blank
    // home-screen tile, which nobody notices until it is on their phone.
    const manifest = await (
      await page.request.get('/manifest.webmanifest')
    ).json();

    for (const icon of manifest.icons) {
      const response = await page.request.get(icon.src);
      expect(response.status(), `${icon.src} is missing`).toBe(200);
      expect(response.headers()['content-type']).toContain('image/png');
    }
  });

  test('the apple-touch-icon exists', async ({ page }) => {
    // iOS ignores the manifest icons entirely and uses this one. Without it
    // the home-screen icon is a blurry screenshot of the page.
    await page.goto('/pingpong/');
    const href = await page
      .locator('link[rel="apple-touch-icon"]')
      .getAttribute('href');

    expect(href).toBeTruthy();
    expect((await page.request.get(href)).status()).toBe(200);
  });

  test('iOS standalone meta tags are present', async ({ page }) => {
    // Push on iOS only works from an installed app, and iOS only installs as
    // a standalone app if these are here. Load-bearing, not decoration.
    await page.goto('/pingpong/');

    await expect(
      page.locator('meta[name="apple-mobile-web-app-capable"]')
    ).toHaveAttribute('content', 'yes');
  });
});

test.describe('service worker', () => {
  test('registers at the site root so its scope covers the app', async ({ page }) => {
    await page.goto('/pingpong/');

    // Registration is asynchronous and push.js is deferred, so this has to
    // be polled -- reading it straight after goto() reliably catches the
    // moment before the worker exists.
    await expect
      .poll(
        async () =>
          page.evaluate(async () => {
            if (!('serviceWorker' in navigator)) return 'unsupported';
            const reg = await navigator.serviceWorker.getRegistration('/');
            return reg ? new URL(reg.scope).pathname : null;
          }),
        { timeout: 10000 }
      )
      // A worker served from /static/ or /pingpong/ could only ever control
      // that subtree, which is the whole reason it is a Django view at /sw.js.
      .toMatch(/^(\/|unsupported)$/);
  });

  test('the worker script is served as JavaScript', async ({ page }) => {
    const response = await page.request.get('/sw.js');

    expect(response.status()).toBe(200);
    expect(response.headers()['content-type']).toContain('javascript');
  });
});

test.describe('under the production CSP', () => {
  test.beforeEach(async ({ page }) => {
    await applyProdCSP(page);
  });

  test('nothing on the notifications page is blocked', async ({ page }) => {
    const violations = [];
    page.on('console', (msg) => {
      const text = msg.text();
      if (/Content Security Policy|unsafe-eval|Refused to/i.test(text)) {
        violations.push(text);
      }
    });
    page.on('pageerror', (err) => violations.push(String(err)));

    await page.goto('/pingpong/notifications/');
    await page.waitForTimeout(1000);

    expect(
      violations,
      `CSP violations on the notifications page:\n${violations.join('\n')}`
    ).toHaveLength(0);
  });

  test('a configured server offers a way forward on iOS', async ({ page }) => {
    // The e2e server has VAPID keys set (scripts/e2e_server.sh), so this is
    // the production-shaped case. In mobile Safari, PushManager does not
    // exist until the app has been installed -- the user must be told to add
    // it to the Home Screen, not told their browser cannot do notifications.
    await page.goto('/pingpong/notifications/');

    await expect(page.locator('[data-push-state="ios-install"]')).toBeVisible();
    await expect(page.locator('[data-push-state="unconfigured"]')).toBeHidden();
  });

  test('push.js resolves the panel to exactly one state', async ({ page }) => {
    await page.goto('/pingpong/notifications/');

    // Whatever this browser supports, the user must end up looking at one
    // explanation -- never several, and never a bare panel with no controls
    // and no reason given.
    await expect
      .poll(
        async () =>
          page
            .locator('[data-push-state]:not(.hidden)')
            .count(),
        { timeout: 5000 }
      )
      .toBe(1);
  });
});
