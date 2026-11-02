const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');

/**
 * The head-to-head point comparison (KAN-33 / S2.7).
 *
 * Named `.h2h.spec.js` so it runs under `iphone-safari` like the other plain
 * specs -- the projects match on `.nojs`/`.reduced`/`.dark`/`.desktop`
 * suffixes only.
 */

async function openHeadToHead(page) {
  await page.goto('/pingpong/head-to-head/');
  // The two selects are Tom Select-enhanced; set the underlying <select> and
  // submit the real form rather than driving the widget.
  const ids = await page.evaluate(() => {
    const opts = Array.from(document.querySelectorAll('select option'));
    const find = (name) => opts.find((o) => o.textContent.trim().includes(name));
    return {
      one: find('E2E Player') && find('E2E Player').value,
      two: find('E2E Momentum') && find('E2E Momentum').value,
    };
  });
  expect(ids.one).toBeTruthy();
  expect(ids.two).toBeTruthy();
  await page.goto(`/pingpong/head-to-head/?player1=${ids.one}&player2=${ids.two}`);
}

test.beforeEach(async ({ page }) => {
  await applyProdCSP(page);
});

test('both players are compared side by side, with their sample sizes',
  async ({ page }) => {
    const errors = [];
    page.on('pageerror', (e) => {
      // Chart.js needs eval for part of its scale code and this suite's CSP
      // withholds it on purpose; production's own policy allows it. See
      // momentum.spec.js for the full reasoning.
      if (!/Refused to evaluate a string as JavaScript/.test(e.message)) {
        errors.push(e.message);
      }
    });

    await openHeadToHead(page);

    const panel = page.getByRole('heading', {
      name: /Serving & receiving, head to head/i,
    });
    await expect(panel).toBeVisible();

    // Scoped to the panel. Both player names also appear in the sidebar and
    // the drawer, and a bare .first() grabs an invisible node -- the trap the
    // frontend-verification skill already records for nav links.
    const card = panel.locator(
      'xpath=ancestor::div[contains(@class,"bg-card")][1]'
    );
    await expect(card.getByText('E2E Player', { exact: true })).toBeVisible();
    await expect(card.getByText('E2E Momentum', { exact: true })).toBeVisible();
    // Every rate carries its sample size, in parentheses beside it.
    await expect(card.getByText(/\(\s*\d+\s*\/\s*\d+\s*\)/).first()).toBeVisible();
    await expect(card.getByText(/Deuce games/i).first()).toBeVisible();

    expect(errors).toEqual([]);
  });

test('the page does not scroll sideways at iPhone width', async ({ page }) => {
  await openHeadToHead(page);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
});
