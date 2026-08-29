const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');

/**
 * Club invite links, in a real WebKit at iPhone width (KAN-18 / S1.6).
 *
 * pytest already asserts the flow's *behaviour* thoroughly. What it cannot see
 * is whether the pages are usable: it asserts on a rendered string, and the
 * string was perfect while the mobile drawer was shipping broken.
 *
 * So this spec covers the two things only a browser can answer:
 *
 *   1. Does the invite link the admin is supposed to copy actually appear,
 *      legibly, on a 390px screen -- without pushing the page sideways?
 *   2. Does the dead-link page render, icon and all? `{% icon %}` resolves
 *      against a committed sprite and a missing symbol renders an EMPTY BOX,
 *      silently: no error, no failed request, nothing pytest can detect.
 */

const SETTINGS = '/pingpong/club/settings/';

/** Nothing may push the page sideways at phone width. A horizontal scrollbar
 *  is the classic symptom of an unbreakable string -- and an invite URL is a
 *  32-character token with no spaces in it, which is exactly that hazard. */
async function expectNoHorizontalScroll(page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
}

// The iphone-safari project carries a stored auth state from auth.setup.js,
// so these run as the seeded e2e user -- who seed_e2e makes the OWNER of the
// e2e club. No per-test login: /accounts/login/ redirects away when you are
// already signed in, which just times out looking for a username field.
test.describe('club invites', () => {
  test('an admin can create a link and read it on a phone', async ({ page }) => {
    await page.goto(SETTINGS);
    await expect(page.getByRole('heading', { name: 'Invite someone' })).toBeVisible();

    await page.click('button:has-text("Create invite link")');

    // The whole point of the feature: a URL the admin can copy out.
    const link = page.locator('text=/\\/pingpong\\/join\\/[0-9a-f]{32}\\//');
    await expect(link).toBeVisible();

    // It must not be clipped to unreadability or run off the screen. The
    // template uses `break-all` precisely because a token has no break points.
    await expectNoHorizontalScroll(page);
    const box = await link.boundingBox();
    expect(box.width).toBeLessThanOrEqual(page.viewportSize().width);
  });

  test('the link is shown once, not left on the page forever', async ({ page }) => {
    await page.goto(SETTINGS);
    await page.click('button:has-text("Create invite link")');
    await expect(page.locator('text=New link ready')).toBeVisible();

    // An invite link is a bearer credential. A settings page that always
    // displays every live link is a settings page you cannot show anyone.
    await page.reload();
    await expect(page.locator('text=New link ready')).toHaveCount(0);
  });

  test('a revoked link shows a real page, not a stack trace', async ({ page }) => {
    await page.goto(SETTINGS);
    await page.click('button:has-text("Create invite link")');

    const href = await page
      .locator('text=/\\/pingpong\\/join\\/[0-9a-f]{32}\\//')
      .innerText();
    const path = new URL(href.trim()).pathname;

    await page.click('button:has-text("Revoke")');
    await page.goto(path);

    await expect(
      page.getByRole('heading', { name: "This invite doesn't work" }),
    ).toBeVisible();
    await expect(page.locator('text=revoked')).toBeVisible();
    await expectNoHorizontalScroll(page);
  });

  test('the dead-link page renders its icon, not an empty box', async ({ page }) => {
    await page.goto('/pingpong/join/00000000000000000000000000000000/');

    // A `<use href="#i-name">` pointing at a symbol that is not in the sprite
    // renders nothing at all, with no error anywhere. The only way to know is
    // to measure it in a browser.
    const icon = page.locator('svg use[href="#i-circle-x"]');
    await expect(icon).toHaveCount(1);

    const box = await page.locator('svg:has(use[href="#i-circle-x"])').boundingBox();
    expect(box.width).toBeGreaterThan(0);
    expect(box.height).toBeGreaterThan(0);
  });

  test('nothing is blocked under the production CSP', async ({ page }) => {
    // Dev sends no CSP at all, which is how the drawer shipped broken while
    // every other check was green.
    const blocked = [];
    page.on('console', (msg) => {
      if (/Content Security Policy/i.test(msg.text())) blocked.push(msg.text());
    });

    await applyProdCSP(page);
    await page.goto('/pingpong/join/00000000000000000000000000000000/');
    await expect(
      page.getByRole('heading', { name: "This invite doesn't work" }),
    ).toBeVisible();

    expect(blocked).toEqual([]);
  });
});
