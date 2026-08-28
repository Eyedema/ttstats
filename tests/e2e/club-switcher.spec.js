const { test, expect } = require('@playwright/test');
const { applyProdCSP, waitForDrawerSettled } = require('./helpers');

/**
 * The club switcher, at iPhone width in real WebKit (KAN-19 / S1.8).
 *
 * pytest asserts the switcher is in the markup. It cannot tell you whether the
 * control is on screen, tappable, or legible -- the rendered string was
 * perfect while the drawer was shipping permanently open on iOS Safari, which
 * is the whole reason this file is mandatory rather than nice to have.
 *
 * The seeded e2e user is a member of two clubs (see `manage.py seed_e2e`), so
 * the switcher renders. A single-club user gets no switcher by design.
 */

// By role, like the other drawer specs: the spec should not depend on an id
// that exists for the plain-JS handler's benefit.
const drawerToggle = (page) => page.getByRole('button', { name: 'Open menu' });
const switcher = (page) => page.locator('#mobile-menu-panel [data-club-switcher]');

async function openDrawer(page) {
  await drawerToggle(page).tap();
  await waitForDrawerSettled(page);
}

test.describe('club switcher', () => {
  test.beforeEach(async ({ page }) => {
    await applyProdCSP(page);
    await page.goto('/pingpong/');
  });

  test('the active club is legible without opening the drawer', async ({ page }) => {
    // Otherwise every screen is ambiguous for a multi-club user, which is the
    // problem the switcher exists to solve.
    const label = page.locator('header [data-active-club]');
    await expect(label).toBeVisible();
    await expect(label).not.toBeEmpty();
  });

  test('the drawer lists both clubs and marks the active one', async ({ page }) => {
    await openDrawer(page);

    const rows = switcher(page).locator('button[type="submit"]');
    await expect(rows).toHaveCount(2);

    // aria-current, not colour alone -- colour is never the only signal here.
    await expect(switcher(page).locator('[aria-current="true"]')).toHaveCount(1);
  });

  test('every row is a real tap target', async ({ page }) => {
    await openDrawer(page);

    for (const row of await switcher(page).locator('button').all()) {
      const box = await row.boundingBox();
      expect(box.height).toBeGreaterThanOrEqual(44);
    }
  });

  test('a long club name does not push the drawer sideways', async ({ page }) => {
    // The seed deliberately names the second club something very long.
    await openDrawer(page);

    const panel = page.locator('#mobile-menu-panel');
    const overflow = await panel.evaluate(
      (el) => el.scrollWidth - el.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);

    const pageOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(pageOverflow).toBeLessThanOrEqual(1);
  });

  test('switching lands on Today showing the other club', async ({ page }) => {
    await openDrawer(page);

    const before = await page.locator('header [data-active-club]').innerText();
    const inactive = switcher(page)
      .locator('button[type="submit"]:not([aria-current="true"])')
      .first();

    await Promise.all([page.waitForURL(/\/pingpong\/$/), inactive.click()]);

    const after = await page.locator('header [data-active-club]').innerText();
    expect(after).not.toBe(before);
  });

  test('nothing is blocked under the production CSP', async ({ page }) => {
    // The switcher is deliberately plain forms -- no Alpine, no eval. If that
    // ever stops being true it shows up here rather than in production.
    const blocked = [];
    page.on('console', (msg) => {
      if (/Content Security Policy/i.test(msg.text())) blocked.push(msg.text());
    });

    await openDrawer(page);
    await expect(switcher(page)).toBeVisible();

    expect(blocked).toEqual([]);
  });
});
