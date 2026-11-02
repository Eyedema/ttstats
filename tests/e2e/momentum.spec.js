const { test, expect } = require('@playwright/test');
const { applyProdCSP, openMomentumMatch } = require('./helpers');

/**
 * The match-detail momentum chart (KAN-31 / S2.5).
 *
 * pytest can prove the context and the json_script payload are right. It
 * cannot say whether a canvas ever gets drawn on, which is the only thing the
 * feature is for -- so that claim lives here, in real WebKit at 390px.
 *
 * The seeded momentum match (`seed_e2e`) is a completed live-scored best-of-3
 * whose games are a comeback from 5-9 down, a shutout, and a deuce game, so
 * one page covers all three shapes.
 */

/** True once Chart.js has actually painted pixels into this canvas. */
async function canvasHasDrawing(canvas) {
  return canvas.evaluate((el) => {
    const ctx = el.getContext('2d');
    const { data } = ctx.getImageData(0, 0, el.width, el.height);
    for (let i = 3; i < data.length; i += 4) {
      if (data[i] !== 0) return true; // any non-transparent pixel
    }
    return false;
  });
}

test.describe('match detail momentum', () => {
  test.beforeEach(async ({ page }) => {
    await applyProdCSP(page);
  });

  test('a chart is drawn for every game, and nothing errors', async ({ page }) => {
    // The leaderboard once shipped a control that raised on every load and
    // nothing caught it. Collect page errors for the whole spec.
    //
    // One message is filtered, deliberately and narrowly: Chart.js compiles
    // part of its scale code with `new Function()`, so under this suite's
    // PROD_CSP -- which withholds 'unsafe-eval' on purpose, to keep the
    // mobile drawer off framework expressions -- the browser reports one
    // refusal. Production's own policy DOES allow 'unsafe-eval' (Alpine needs
    // it), so this cannot happen there. Keeping the strict policy here and
    // filtering exactly this string is the honest trade: everything below
    // still asserts the canvases are genuinely painted, which is the stronger
    // claim -- the chart draws even under a policy stricter than production's.
    const KNOWN_CHART_EVAL_REFUSAL = /Refused to evaluate a string as JavaScript/;
    const errors = [];
    page.on('pageerror', (e) => {
      if (!KNOWN_CHART_EVAL_REFUSAL.test(e.message)) errors.push(e.message);
    });

    await openMomentumMatch(page);
    await expect(page.getByRole('heading', { name: 'Momentum' })).toBeVisible();

    const charts = page.locator('.point-momentum-chart');
    await expect(charts).toHaveCount(3);

    for (let i = 0; i < 3; i += 1) {
      const canvas = charts.nth(i);
      await expect(canvas).toBeVisible();
      await expect.poll(() => canvasHasDrawing(canvas), { timeout: 5000 }).toBe(true);
    }

    expect(errors).toEqual([]);
  });

  test('the comeback is stated in words, not only drawn', async ({ page }) => {
    await openMomentumMatch(page);
    await expect(page.getByText(/came back from 4 points down/i)).toBeVisible();
  });

  test('the page never scrolls sideways at iPhone width', async ({ page }) => {
    await openMomentumMatch(page);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test('a match that was not scored live shows no chart at all', async ({ page }) => {
    // The live fixture match is not finished, so the *other* seeded match --
    // the plain one -- is reached from the match list. Absence is the
    // assertion: no empty canvas, no "no data" panel taking up space.
    await page.goto('/pingpong/matches/');
    const plain = page.getByRole('link', { name: /E2E Opponent/i }).first();
    if (await plain.count()) {
      await plain.tap();
      await expect(page.locator('.point-momentum-chart')).toHaveCount(0);
      await expect(page.getByRole('heading', { name: 'Momentum' })).toHaveCount(0);
    }
  });
});
