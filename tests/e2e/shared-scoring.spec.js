const { test, expect } = require('@playwright/test');
const { applyProdCSP, releaseProdCSP } = require('./helpers');

/**
 * The host's shared-scoring lobby (KAN-151 / KAN-164), in WebKit at iPhone
 * width under the production CSP.
 *
 * pytest asserts the QR payload, the no-store headers and every join branch.
 * What only a browser can answer: is the code big enough to scan, does the
 * page stay inside the screen, do Share/Copy appear only where they work,
 * and does the static poll script run under the CSP and move the host on.
 *
 * The seeded e2e user has the `shared_scoring` override (seed_e2e). The
 * two-phone scoring e2e comes with the scoreboard work (KAN-148, later).
 */

async function openLobby(page) {
  await page.goto('/pingpong/matches/add/');
  await page.getByTestId('score-shared').click();
  // applyProdCSP's route.fetch follows the POST's redirect itself, so the
  // address bar keeps /live/lobby/new/. Land on the lobby's own URL.
  const again = page.getByTestId('check-again');
  await expect(again).toHaveAttribute('href', /\/pingpong\/live\/lobby\/\d+\/$/);
  await page.goto(await again.getAttribute('href'));
  await expect(page).toHaveURL(/\/pingpong\/live\/lobby\/\d+\/$/);
}

/** A fixed tab bar can sit over the bottom of the page at phone height. */
async function tap(locator) {
  await locator.evaluate((el) => el.scrollIntoView({ block: 'center' }));
  await locator.click();
}

async function expectNoHorizontalScroll(page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
}

test.describe('shared scoring lobby', () => {
  test.beforeEach(async ({ page }) => {
    await applyProdCSP(page);
  });

  test('the QR code fills most of the phone width and the link is readable', async ({ page }) => {
    const blocked = [];
    page.on('console', (msg) => {
      if (/Content Security Policy|Refused to/.test(msg.text())) blocked.push(msg.text());
    });
    await openLobby(page);

    const qr = page.getByTestId('lobby-qr').locator('svg.qr');
    await expect(qr).toBeVisible();
    const box = await qr.boundingBox();
    const width = page.viewportSize().width;
    expect(box.width / width).toBeGreaterThan(0.6);
    expect(box.width / width).toBeLessThan(0.8);
    expect(Math.abs(box.width - box.height)).toBeLessThan(2);

    const url = page.getByTestId('lobby-url');
    await expect(url).toBeVisible();
    await expect(url).toHaveText(/\/pingpong\/live\/join\/[0-9a-f]{32}\/$/);
    await expect(page.getByText(/Expires at \d\d:\d\d/)).toBeVisible();
    await expectNoHorizontalScroll(page);
    expect(blocked.filter((t) => /live_lobby/.test(t))).toEqual([]);
  });

  test('Share and Copy appear only where the browser supports them', async ({ page }) => {
    await openLobby(page);
    const support = await page.evaluate(() => ({
      share: typeof navigator.share === 'function',
      copy: !!(navigator.clipboard && navigator.clipboard.writeText),
    }));
    const share = page.locator('[data-lobby-share]');
    const copy = page.locator('[data-lobby-copy]');
    await expect(share).toBeVisible({ visible: support.share });
    await expect(copy).toBeVisible({ visible: support.copy });
  });

  test('Copy link shows a Copied state', async ({ page }) => {
    await openLobby(page);
    // Stub the clipboard: WebKit's real one needs a user permission
    // Playwright cannot grant there. The script only needs writeText, and
    // wires the button at load, so the stub goes in before a reload.
    await page.addInitScript(() => {
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: { writeText: (t) => { window.__copied = t; return Promise.resolve(); } },
      });
    });
    await page.reload();
    const copy = page.locator('[data-lobby-copy]');
    await expect(copy).toBeVisible();
    await tap(copy);
    await expect(copy.locator('[data-copied]')).toBeVisible();
    await expect(copy.locator('[data-copied]')).toHaveText(/Copied/);
    const copied = await page.evaluate(() => window.__copied);
    expect(copied).toMatch(/\/pingpong\/live\/join\/[0-9a-f]{32}\/$/);
  });

  test('the poll moves the host to the scoreboard when someone joins', async ({ page }) => {
    await openLobby(page);
    // Stand in for the opponent: the status endpoint says "joined". The
    // target is Today, which exists for this user; the real one is the
    // scoreboard (asserted in pytest).
    await page.route('**/live/lobby/*/status/', (route) =>
      route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ status: 'joined', match_url: '/pingpong/' }),
      }),
    );
    await page.waitForURL(/\/pingpong\/$/, { timeout: 6000 });
  });

  test('an expired code says so and stops polling', async ({ page }) => {
    await openLobby(page);
    let polls = 0;
    await page.route('**/live/lobby/*/status/', (route) => {
      polls += 1;
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ status: 'expired', reason: 'This code has expired.' }),
      });
    });
    const ended = page.locator('[data-lobby-ended]');
    await expect(ended).toBeVisible({ timeout: 6000 });
    await expect(ended).toContainText('This code has expired.');
    const seen = polls;
    await page.waitForTimeout(4500);
    expect(polls).toBe(seen);
  });

  test('Cancel closes the lobby', async ({ page }) => {
    await openLobby(page);
    const lobbyUrl = page.url();
    await releaseProdCSP(page); // the POST redirects; see openLobby
    await tap(page.getByRole('button', { name: 'Cancel', exact: true }));
    await expect(page).toHaveURL(/\/pingpong\/play\/$/);
    await page.goto(lobbyUrl);
    await expect(page.locator('[data-lobby-ended]')).toBeVisible();
    await expect(page.locator('svg.qr')).toHaveCount(0);
  });

  test('Pick opponent instead returns to the match form', async ({ page }) => {
    await openLobby(page);
    await releaseProdCSP(page);
    await tap(page.getByRole('button', { name: 'Pick opponent instead' }));
    await expect(page).toHaveURL(/\/pingpong\/matches\/add\/\?best_of=\d/);
  });

  test('"Let anyone in the club join" is on by default and reaches the lobby', async ({ page }) => {
    // KAN-157. The Today card itself is server-rendered (pytest covers it).
    await page.goto('/pingpong/matches/add/');
    const box = page.getByTestId('lobby-open-to-club').locator('input[type=checkbox]');
    await expect(box).toBeVisible();
    await expect(box).toBeChecked();
    await expectNoHorizontalScroll(page);
    await openLobby(page);
    await expect(page.getByTestId('lobby-visibility')).toHaveText(
      'Clubmates can also join from Today.',
    );
  });

  test('unticking it keeps the lobby to code holders', async ({ page }) => {
    await page.goto('/pingpong/matches/add/');
    await page.getByTestId('lobby-open-to-club').locator('input[type=checkbox]').uncheck();
    await page.getByTestId('score-shared').click();
    const again = page.getByTestId('check-again');
    await page.goto(await again.getAttribute('href'));
    await expect(page.getByTestId('lobby-visibility')).toHaveText(
      'Only someone with this code can join.',
    );
  });
});
