const { test, expect } = require('@playwright/test');
const { defineBannerCases } = require('./offline-snapshot-banner-cases');

// KAN-145 in the dark palette (the base). Prove the preference landed first.
test.beforeEach(async ({ page }) => {
  await page.goto('/pingpong/');
  expect(await page.evaluate(() => matchMedia('(prefers-color-scheme: dark)').matches)).toBe(true);
});

defineBannerCases(test);
