const { test, expect } = require('@playwright/test');

/**
 * The notification panel with JavaScript disabled.
 *
 * Fail-closed rule (CLAUDE.md): UI whose state is managed by JavaScript must
 * be safe when the script never runs. Here "safe" means the user sees no
 * controls at all rather than an enable button that silently does nothing --
 * a dead button is worse than an absent one, because it teaches the user that
 * notifications are broken rather than unavailable.
 *
 * The preferences form is deliberately NOT in that bucket: it is a plain
 * <form method="post">, so it keeps working with scripting off, and it should.
 */

test('no device controls are shown when push.js cannot run', async ({ page }) => {
  await page.goto('/pingpong/notifications/');

  await expect(page.locator('[data-push-panel]')).toBeVisible();
  await expect(page.locator('[data-push-state]:not(.hidden)')).toHaveCount(0);
  await expect(page.locator('[data-push-enable]')).toBeHidden();
  await expect(page.locator('[data-push-test]')).toBeHidden();
});

test('the preferences form still works without JavaScript', async ({ page }) => {
  await page.goto('/pingpong/notifications/');

  const form = page.locator('form[method="post"]').filter({
    has: page.locator('input[name="match_confirmation"]'),
  });
  await expect(form).toBeVisible();
  await expect(
    form.getByRole('button', { name: 'Save preferences' })
  ).toBeVisible();
});
