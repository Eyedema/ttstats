const { test, expect } = require('@playwright/test');
const {
  drawer,
  drawerScrim,
  expectDrawerClosed,
  waitForDrawerSettled,
} = require('./helpers');

/**
 * The mobile navigation drawer, in real WebKit at a real iPhone viewport.
 *
 * This file exists because a drawer shipped that was permanently open and
 * impossible to close on iOS Safari. pytest could not see it: the markup
 * rendered perfectly, and every assertion about the template string passed.
 * What was wrong was the *resulting state in a browser*.
 */
test.describe('mobile drawer', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/pingpong/players/');
  });

  test('is closed on first paint', async ({ page }) => {
    // The regression, stated directly. The drawer must not be covering the
    // page when you arrive, and the page underneath must be usable.
    await expectDrawerClosed(page);
    await expect(page.getByRole('heading', { name: 'Players' })).toBeVisible();
  });

  test('stays closed when the Alpine bundle fails to load', async ({ page }) => {
    // Half of the real defect: "hidden" depended on Alpine booting. Block the
    // script outright rather than stubbing window.Alpine, which Alpine may
    // route around -- a simulated failure that the framework survives proves
    // nothing.
    await page.route('**/alpine*.js', (route) => route.abort());
    await page.goto('/pingpong/players/');
    await expectDrawerClosed(page);
  });

  test('stays closed when the compiled stylesheet fails to load', async ({ page }) => {
    // The other half, and the reason the cloak rule is inlined in <head>
    // rather than living only in app.css. If the drawer's hidden state can be
    // undone by one stylesheet not arriving -- purged class, bad build, cache
    // miss -- then it will eventually be undone, and the result is an overlay
    // with no way to dismiss it, because the close button is Alpine too.
    await page.route('**/app.css', (route) => route.abort());
    await page.goto('/pingpong/players/');
    await expectDrawerClosed(page);
  });

  test('stays closed when neither Alpine nor the stylesheet loads', async ({ page }) => {
    await page.route('**/alpine*.js', (route) => route.abort());
    await page.route('**/app.css', (route) => route.abort());
    await page.goto('/pingpong/players/');
    await expectDrawerClosed(page);
  });

  test('opens from the menu button and closes again', async ({ page }) => {
    await page.getByRole('button', { name: 'Open menu' }).tap();
    await waitForDrawerSettled(page);

    await page.getByRole('button', { name: 'Close menu' }).tap();
    await expectDrawerClosed(page);
  });

  test('closes by tapping the scrim', async ({ page }) => {
    await page.getByRole('button', { name: 'Open menu' }).tap();
    await waitForDrawerSettled(page);

    // Tap far to the right of the 256px panel, i.e. on the scrim itself.
    await drawerScrim(page).tap({ position: { x: 340, y: 400 } });
    await expectDrawerClosed(page);
  });

  test('enters from the left and leaves to the left', async ({ page }) => {
    // Spatial consistency: a panel that slid in from the edge must go back to
    // that edge, not vanish in place.
    const panel = drawer(page);

    await page.getByRole('button', { name: 'Open menu' }).tap();
    await waitForDrawerSettled(page);

    // Flush against the left edge, at its full width, once settled.
    const open = await panel.boundingBox();
    expect(Math.round(open.x)).toBe(0);
    expect(open.width).toBeGreaterThan(200);

    await page.getByRole('button', { name: 'Close menu' }).tap();
    await expectDrawerClosed(page);
  });

  test('does not leave a tap-blocking scrim behind after closing', async ({ page }) => {
    await page.getByRole('button', { name: 'Open menu' }).tap();
    await waitForDrawerSettled(page);
    await page.getByRole('button', { name: 'Close menu' }).tap();
    await expectDrawerClosed(page);

    // The real test of "closed": something in the page body is reachable
    // again. Scoped to <main>, because the same link text also exists inside
    // the (hidden) drawer and the (hidden) desktop sidebar, and a stray
    // .first() would just pick an invisible one and time out.
    await page.locator('main').getByRole('link', { name: /Record your first match/i })
      .or(page.locator('main').getByRole('link', { name: /View/i }).first())
      .first()
      .tap();
    await expect(page).not.toHaveURL(/players\/$/);
  });
});
