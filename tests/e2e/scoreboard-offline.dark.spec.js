const { test, expect } = require('@playwright/test');
const { applyProdCSP, createLiveMatch, finishLiveMatch, goOffline, goOnline } = require('./helpers');

/**
 * The sync UI in dark, the palette's base (KAN-81). Amber means "live right
 * now" and nothing else, so neither the indicator nor the sheets may use it;
 * and the state is carried by a word plus a glyph, never colour alone.
 */

const AMBER = 'rgb(245, 158, 11)';

test('the sync indicator and sheets are readable in dark and never amber', async ({ page }) => {
  await applyProdCSP(page);
  const match = await createLiveMatch(page, { bestOf: 3 });
  try {
    await page.goto(match.url);
    expect(await page.evaluate(() => matchMedia('(prefers-color-scheme: dark)').matches)).toBe(true);
    await page.getByRole('button', { name: 'E2E Player' }).tap();
    await expect(page.locator('[data-score="team1"]')).toBeVisible();
    await expect.poll(() => page.evaluate(
      () => document.getElementById('scoreboard').scoreboard.session.view().pending,
    )).toBe(0);

    await goOffline(page);
    await page.locator('.tap-zone[data-side="team1"]').tap();
    const indicator = page.locator('[data-sync-indicator]');
    await expect(indicator).toContainText('Offline');
    await expect(indicator.locator('[data-sync-glyph="offline"] svg')).toBeVisible();

    const colours = await indicator.evaluate((el) => {
      const cs = getComputedStyle(el);
      return [cs.color, cs.backgroundColor, cs.borderTopColor];
    });
    expect(colours).not.toContain(AMBER);
    // Text and surface differ: the word is legible on its pill.
    expect(colours[0]).not.toBe(colours[1]);

    await page.locator('.tap-zone[data-side="team2"]').tap();
    await page.getByRole('link', { name: 'Exit' }).tap();
    const sheet = page.getByRole('dialog', { name: 'Not saved yet' });
    await expect(sheet).toBeVisible();
    const sheetColours = await sheet.evaluate((el) => {
      const cs = getComputedStyle(el);
      return [cs.color, cs.backgroundColor];
    });
    expect(sheetColours).not.toContain(AMBER);
    expect(sheetColours[0]).not.toBe(sheetColours[1]);
    await sheet.getByRole('button', { name: 'Stay' }).tap();
  } finally {
    await page.evaluate(() => document.getElementById('scoreboard').scoreboard.session.stop()).catch(() => {});
    await goOnline(page);
    await finishLiveMatch(page, match.pk);
  }
});
