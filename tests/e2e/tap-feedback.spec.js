const { test, expect } = require('@playwright/test');
const { applyProdCSP } = require('./helpers');

/**
 * Touch acknowledgement and the navigation progress bar.
 *
 * The installed PWA has no browser chrome: no throbber, no URL bar, no native
 * progress line. So the app is the only thing that can tell the user a tap
 * became a request. Without that, measured server times of 15-140ms were being
 * read as "slow and unresponsive" -- the app was not slow, it was silent.
 *
 * Under PROD_CSP throughout: the bar's trigger is an inline <script>, and the
 * one thing this suite exists to prevent is shipping navigation UI that works
 * in dev (no CSP at all) and is dead in production.
 */

const progress = (page) => page.locator('#nav-progress');

test.beforeEach(async ({ page }) => {
  await applyProdCSP(page);
});

test('the progress bar is not visible until something is tapped', async ({ page }) => {
  await page.goto('/pingpong/');

  // Present in the DOM but inert: scaled to nothing and fully transparent.
  await expect(progress(page)).toHaveCount(1);
  await expect(progress(page)).toHaveCSS('opacity', '0');
});

test('the progress bar cannot swallow taps', async ({ page }) => {
  await page.goto('/pingpong/');

  // It is fixed across the full width at the top of the viewport, which is
  // exactly where the mobile header's controls are. pointer-events must stay
  // off or it becomes an invisible tap-blocking strip -- the same failure the
  // drawer scrim once had.
  await expect(progress(page)).toHaveCSS('pointer-events', 'none');
});

test('tapping a nav destination acknowledges the tap immediately', async ({ page }) => {
  await page.goto('/pingpong/');

  // Fail the navigation outright. Merely delaying it does not work: Playwright
  // queues every evaluation in a frame behind that frame's pending navigation,
  // so the assertion below would not run until the new document had already
  // replaced the one holding the armed bar. Aborting keeps this document alive
  // and makes the state readable, while still going through a genuine tap on
  // the real tab bar rather than a synthesised event.
  await page.route('**/pingpong/leaderboard/', (route) => route.abort());

  await page.getByRole('link', { name: 'Table' }).click();

  // Acknowledgement is on screen even though the page never arrived.
  await expect(progress(page)).toHaveClass(/is-loading/);
  await expect(progress(page)).toHaveCSS('opacity', '1');
});

test('the bar arms from a real click event, not just from navigation', async ({ page }) => {
  await page.goto('/pingpong/');

  // Same assertion without the navigation race. The page's own handler is on
  // document in the bubble phase, so a preventDefault listener registered
  // afterwards runs *after* it: the bar arms, then the navigation is
  // cancelled, leaving the state readable synchronously.
  const className = await page.evaluate(() => {
    document.addEventListener('click', (e) => e.preventDefault());
    document.querySelector('a[href="/pingpong/leaderboard/"]').click();
    return document.getElementById('nav-progress').className;
  });

  expect(className).toContain('is-loading');
});

test('the bar does not arm for links that never leave the page', async ({ page }) => {
  await page.goto('/pingpong/');

  // An in-page anchor issues no request, so reporting progress for it would be
  // a lie the user has to watch time out.
  await page.evaluate(() => {
    const a = document.createElement('a');
    a.href = '#somewhere';
    a.id = 'in-page-link';
    a.textContent = 'jump';
    document.body.appendChild(a);
  });
  await page.click('#in-page-link');

  await expect(progress(page)).not.toHaveClass(/is-loading/);
});

test('coming back does not leave the bar stuck mid-load', async ({ page }) => {
  await page.goto('/pingpong/');
  await page.getByRole('link', { name: 'Table' }).click();
  await page.waitForURL(/leaderboard/);

  await page.goBack();
  await page.waitForURL(/\/pingpong\/$/);

  // bfcache restores the document with whatever classes it had when it left.
  // If pageshow does not clear this, "back" looks like it is loading forever.
  await expect(progress(page)).not.toHaveClass(/is-loading/);
  await expect(progress(page)).toHaveCSS('opacity', '0');
});

test('tab bar and menu rows press without moving the bar itself', async ({ page }) => {
  await page.goto('/pingpong/');

  // pressable-flat is the opt-out from the global :active scale. A 56px tab
  // that scales makes the whole fixed bar look like it is wobbling.
  const tab = page.getByRole('link', { name: 'Table' });
  await expect(tab).toHaveClass(/pressable-flat/);

  // Whatever else it does, it must not shrink.
  await expect(tab).toHaveCSS('transform', 'none');
});

test('interactive elements suppress the iOS tap delay and grey flash', async ({ page }) => {
  await page.goto('/pingpong/');

  // touch-action: manipulation removes the ~300ms double-tap-zoom wait, which
  // is on its own longer than any server response this app produces.
  const tab = page.getByRole('link', { name: 'Table' });
  await expect(tab).toHaveCSS('touch-action', 'manipulation');
});

test('the leaderboard Period filter is actually wired up', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));

  await page.goto('/pingpong/leaderboard/');

  // The trigger selector used to be jQuery syntax, which throws when htmx
  // resolves it and leaves this dropdown inert. Nothing on the page should
  // raise at all.
  expect(errors).toEqual([]);

  // The filters live behind a collapsed <details>.
  await page.getByText('Filters', { exact: true }).click();

  const responded = page.waitForResponse(
    (r) => r.url().includes('/pingpong/leaderboard/') && r.request().method() === 'GET'
  );
  await page.locator('#date_filter').selectOption('month');
  await responded;

  // htmx pushes the chosen filter into the URL, so the choice is shareable and
  // survives back.
  await expect(page).toHaveURL(/date_filter=month/);
});
