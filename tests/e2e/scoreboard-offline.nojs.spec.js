const { test, expect } = require('@playwright/test');
const { createLiveMatch, finishLiveMatch } = require('./helpers');

/**
 * Fail closed with scripting off (KAN-81). Every scoreboard overlay -- the
 * sync indicator and sheet, the leave sheet, the finish screen, the re-auth
 * prompt, the follow banner, the side-switch prompt, the toast -- starts
 * `hidden` and only scoreboard.js reveals it. With no JS, none may show, and
 * the page says plainly that it needs JavaScript.
 */

const OVERLAYS = [
  '[data-sync-indicator]', '[data-sync-sheet]', '[data-leave-sheet]', '[data-sb-finish]',
  '[data-sb-reauth]', '[data-sb-follow]', '[data-sb-switch]', '[data-sb-toast]',
  '[data-sb-picker]', '[data-sb-board]',
];

test('no scoreboard overlay renders open without JavaScript', async ({ page }) => {
  const match = await createLiveMatch(page, { bestOf: 3 });
  try {
    await page.goto(match.url);
    await expect(page.locator('#scoreboard')).toBeVisible();
    for (const selector of OVERLAYS) {
      await expect(page.locator(selector), selector).toBeHidden();
    }
    // Playwright's WebKit with javaScriptEnabled:false still parses <noscript>
    // as raw text (the parser's scripting flag stays on), so the message is
    // asserted as markup here; a browser with scripting off renders it.
    await expect(page.locator('#scoreboard noscript')).toHaveCount(1);
    expect(await page.locator('#scoreboard noscript').innerHTML()).toContain('needs JavaScript');
  } finally {
    await finishLiveMatch(page, match.pk);
  }
});
