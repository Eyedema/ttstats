const { test, expect } = require('@playwright/test');
const { applyProdCSP, createLiveMatch, finishLiveMatch, goOffline, goOnline } = require('./helpers');

/**
 * The sync UI with Reduce Motion on (KAN-81): nothing it says depends on
 * movement. The indicator is a word plus a glyph, the finish screen is
 * static text, and the toast appears without a transition.
 */

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
});

test('the sync status and the finish screen carry their meaning without motion', async ({ page }) => {
  await applyProdCSP(page);
  const match = await createLiveMatch(page, { bestOf: 3 });
  try {
    await page.goto(match.url);
    expect(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches)).toBe(true);
    await page.getByRole('button', { name: 'E2E Player' }).tap();
    await expect(page.locator('[data-score="team1"]')).toBeVisible();
    await expect.poll(() => page.evaluate(
      () => document.getElementById('scoreboard').scoreboard.session.view().pending,
    )).toBe(0);

    await goOffline(page);
    for (let i = 0; i < 22; i += 1) await page.locator('.tap-zone[data-side="team1"]').tap();

    const indicator = page.locator('[data-sync-indicator]');
    await expect(indicator).toBeHidden(); // the finish screen speaks instead
    const finish = page.locator('[data-sb-finish]');
    await expect(finish.getByRole('heading', { name: 'Match finished' })).toBeVisible();
    await expect(finish.locator('[data-finish-status]')).toContainText('Offline');
    const animation = await finish.evaluate((el) => {
      const cs = getComputedStyle(el);
      return [cs.animationName, cs.transitionDuration];
    });
    expect(animation[0]).toBe('none');
  } finally {
    await page.evaluate(() => document.getElementById('scoreboard').scoreboard.session.stop()).catch(() => {});
    await goOnline(page);
    await finishLiveMatch(page, match.pk);
  }
});
