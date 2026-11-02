const { test, expect } = require('@playwright/test');
const { applyProdCSP, openMomentumMatch } = require('./helpers');

/**
 * Reduced motion: the momentum chart must not animate.
 *
 * An animated chart is decoration, and `prefers-reduced-motion` is an OS
 * setting a great many iPhone users actually have on -- app.css already
 * narrows transitions to colour for exactly this reason, but Chart.js draws
 * to a canvas and honours no stylesheet, so it has to be told.
 */

test.beforeEach(async ({ page }) => {
  await applyProdCSP(page);
  await page.emulateMedia({ reducedMotion: 'reduce' });
});

test('the preference actually reached the page', async ({ page }) => {
  // Kept because use.reducedMotion once did not reach matchMedia in WebKit,
  // which would leave this file quietly testing nothing.
  await page.goto('/pingpong/');
  const reduced = await page.evaluate(
    () => window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
  expect(reduced).toBe(true);
});

test('the momentum chart is drawn with animation disabled', async ({ page }) => {
  await openMomentumMatch(page);
  await expect(page.getByRole('heading', { name: 'Momentum' })).toBeVisible();

  const animation = await page.evaluate(async () => {
    const canvas = document.querySelector('.point-momentum-chart');
    if (!canvas || typeof window.Chart === 'undefined') return 'no chart';
    const chart = window.Chart.getChart(canvas);
    return chart ? chart.options.animation : 'no chart';
  });

  expect(animation).toBe(false);
});
