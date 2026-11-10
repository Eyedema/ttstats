const { test, expect } = require('@playwright/test');
const { applyProdCSP, createLiveMatch, finishLiveMatch } = require('./helpers');
const {
  expectSynced, openSharedScoreboard, recordStateRequests, startIfNeeded,
} = require('./shared-scoreboard-helpers');

/**
 * The co-scoring scoreboard client (KAN-153) on an iPhone-sized WebKit,
 * under the production CSP.
 *
 *   - A single-scorer scoreboard is unchanged: it never long-polls.
 *   - A shared one renders "my side": my "+1" zone at the bottom, the
 *     opponent's score read-only above it (no button, not focusable, a tap
 *     there does nothing), and keeps one long-poll in flight.
 *
 * Two phones scoring each other's points is KAN-155's two-context spec.
 */

const myZone = (page) => page.locator('.tap-zone[data-side="team1"]');
const theirZone = (page) => page.locator('[data-zone="theirs"]');
const score = (page, side) => page.locator(`[data-score="${side}"]`);
const read = async (page, side) => Number(await score(page, side).innerText());

test.describe('single-scorer scoreboard', () => {
  test('sends no long-poll request and keeps scoring', async ({ page }) => {
    await applyProdCSP(page);
    const seen = recordStateRequests(page);
    const match = await createLiveMatch(page, { bestOf: 3 });
    try {
      await page.goto(match.url);
      await page.getByRole('button', { name: 'E2E Player', exact: true }).tap();
      await expect(score(page, 'team1')).toBeVisible();
      expect(await page.locator('#scoreboard').getAttribute('data-shared')).toBeNull();
      for (let i = 0; i < 3; i += 1) await page.locator('.tap-zone[data-side="team1"]').tap();
      await page.locator('.tap-zone[data-side="team2"]').tap();
      await expect(score(page, 'team1')).toHaveText('3');
      await expect(score(page, 'team2')).toHaveText('1');
      await expectSynced(page);
      // Visible again: the single-scorer nudge, which may re-read the state
      // but never with ?since= (that is the shared client's long-poll).
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      await page.waitForTimeout(1500);
      expect(seen.filter((r) => r.since !== null)).toEqual([]);
    } finally {
      await finishLiveMatch(page, match.pk);
    }
  });
});

test.describe('shared scoreboard: my side', () => {
  test.beforeEach(async ({ page }) => {
    await applyProdCSP(page);
  });

  test('my +1 zone fills the bottom, the opponent score sits above it', async ({ page }) => {
    await openSharedScoreboard(page);
    await startIfNeeded(page);
    const root = page.locator('#scoreboard');
    await expect(root).toHaveAttribute('data-shared', '');
    await expect(root).toHaveAttribute('data-my-side', 'team1');
    await expect(myZone(page)).toContainText('+1 E2E Player');

    const mine = await myZone(page).boundingBox();
    const theirs = await theirZone(page).boundingBox();
    expect(theirs.y + theirs.height).toBeLessThanOrEqual(mine.y + 1); // stacked, mine below
    expect(mine.height).toBeGreaterThan(theirs.height);
    expect(mine.width).toBeGreaterThan(300); // full width, not half
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
    await page.screenshot({ path: test.info().outputPath('shared-my-side.png') });
  });

  test('the opponent zone is not a button, not focusable and not tappable', async ({ page }) => {
    await openSharedScoreboard(page);
    await startIfNeeded(page);
    await expect(page.getByRole('button', { name: /E2E Partner/ })).toHaveCount(0);
    const facts = await theirZone(page).evaluate((el) => {
      el.focus();
      return {
        tag: el.tagName, role: el.getAttribute('role'), tabIndex: el.tabIndex,
        focused: document.activeElement === el,
      };
    });
    expect(facts).toEqual({ tag: 'DIV', role: null, tabIndex: -1, focused: false });

    const before = [await read(page, 'team1'), await read(page, 'team2')];
    const applied = await page.evaluate(
      () => document.getElementById('scoreboard').scoreboard.session.view().appliedSeq,
    );
    await theirZone(page).tap();
    await page.waitForTimeout(400);
    expect([await read(page, 'team1'), await read(page, 'team2')]).toEqual(before);
    const view = await page.evaluate(() => {
      const v = document.getElementById('scoreboard').scoreboard.session.view();
      return { pending: v.pending, appliedSeq: v.appliedSeq };
    });
    expect(view).toEqual({ pending: 0, appliedSeq: applied });
  });

  test('my tap scores, Undo names whose point it removes, and one long-poll stays open', async ({ page }) => {
    const seen = recordStateRequests(page);
    await openSharedScoreboard(page);
    await startIfNeeded(page);
    const before = await read(page, 'team1');
    await myZone(page).tap();
    await expect(score(page, 'team1')).toHaveText(String(before + 1));
    await expectSynced(page);

    const undo = page.locator('[data-action="undo"]');
    await expect(undo).toContainText(/Undo E2E Player.s point \(\d+–\d+\)/);
    await undo.tap();
    await expect(score(page, 'team1')).toHaveText(String(before));
    await expectSynced(page);

    // wait=0 on load, then the held wait=1 loop. A previous spec's held poll
    // may still own the server's slot for up to 20 s (`busy`): it backs off
    // a second at a time, so a held request shows up within that window.
    await expect.poll(() => seen.some((r) => r.since !== null && r.wait === null)).toBe(true);
    await expect.poll(() => seen.some((r) => r.wait === '1'), { timeout: 25000 }).toBe(true);
  });
});
