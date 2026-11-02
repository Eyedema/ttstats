const { test, expect } = require('@playwright/test');
const { applyProdCSP, openPlayerByName } = require('./helpers');

/**
 * The player-detail point-level section (KAN-32 / S2.6).
 *
 * pytest proves the markup string is right. Whether the panel is on screen at
 * 390px, and whether its numbers are legible against the court, is only
 * answerable here.
 */

async function openMomentumPlayer(page) {
  await openPlayerByName(page, 'E2E Momentum');
  await expect(page.getByRole('heading', { name: /Serving & receiving/i })).toBeVisible();
}

test.beforeEach(async ({ page }) => {
  await applyProdCSP(page);
});

test('the section is on screen with its counts, not just its rates', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));

  await openMomentumPlayer(page);

  // The counts are the whole point: a rate you cannot check is a rate you
  // have to trust.
  await expect(page.getByText(/points served/i)).toBeVisible();
  await expect(page.getByText(/points received/i)).toBeVisible();
  await expect(page.getByText(/Longest run/i)).toBeVisible();

  expect(errors).toEqual([]);
});

test('a small sample is captioned in words, not signalled by colour alone',
  async ({ page }) => {
    await openMomentumPlayer(page);
    // One seeded match is far under the threshold.
    await expect(page.getByText(/Small sample/i).first()).toBeVisible();
  });

test('the section does not push the page sideways at iPhone width', async ({ page }) => {
  await openMomentumPlayer(page);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
});
