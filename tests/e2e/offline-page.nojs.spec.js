const { test, expect } = require('@playwright/test');

/**
 * KAN-137: the offline page with no JavaScript. No worker runs here (this
 * project blocks them and has scripting off), so the page is opened at its
 * own URL, as the worker precaches it: static text, a Try again link to the
 * same URL, and links to Today and the Table. The JS-built list stays hidden.
 */
test('no JS: the static text and the two links, nothing JS-managed shown', async ({ page }) => {
  await page.goto('/offline/');
  await expect(page.locator('h1')).toContainText(
    "No connection, and this page isn't saved on your phone yet.",
    { useInnerText: true }
  );
  await expect(page.locator('h1')).not.toContainText("isn't answering", { useInnerText: true });

  const retry = page.locator('[data-offline-retry]');
  await expect(retry).toBeVisible();
  expect(await retry.getAttribute('href')).toBe('');

  const fallback = page.locator('[data-offline-static]');
  await expect(fallback.getByRole('link', { name: 'Today' })).toHaveAttribute('href', '/pingpong/');
  await expect(fallback.getByRole('link', { name: 'The Table' })).toHaveAttribute(
    'href',
    '/pingpong/leaderboard/'
  );
  await expect(page.locator('[data-offline-saved]')).toBeHidden();
  await expect(page.locator('[data-offline-empty]')).toBeHidden();

  // Same for a logged-in viewer: nothing of theirs on it.
  expect(await page.content()).not.toContain('csrfmiddlewaretoken');
});
