/**
 * Shared probes for the cross-document view-transition specs (KAN-125).
 *
 * Not a spec file: the projects pick specs by `*.spec.js`, and this is
 * required by the `view-transitions.*.spec.js` files in several of them.
 */

/**
 * Record, on every document, whether it was revealed through a view
 * transition. `pagereveal` fires before the first frame and carries the
 * transition (or null), so an init script -- which runs before any page
 * script and ignores the page's CSP -- is the only reliable place to listen.
 *
 * While the transition is live it also samples the computed background of
 * the `::view-transition` root, which is what shows behind the snapshots.
 */
async function recordPageReveal(page) {
  await page.addInitScript(() => {
    window.__vt = { revealed: 'never', background: null };
    window.addEventListener('pagereveal', (e) => {
      window.__vt.revealed = Boolean(e.viewTransition);
      if (e.viewTransition) {
        window.__vt.background = getComputedStyle(
          document.documentElement,
          '::view-transition'
        ).backgroundColor;
      }
    });
  });
}

/**
 * What the current document's pagereveal recorded. A URL assertion resolves
 * at commit, before the first frame, so wait for the event itself where the
 * engine has it (Firefox does not; there `revealed` stays 'never').
 */
async function pageReveal(page) {
  await page.waitForLoadState('load');
  await page.waitForFunction(
    () => window.__vt && (window.__vt.revealed !== 'never' || !('onpagereveal' in window))
  );
  return page.evaluate(() => window.__vt);
}

/**
 * Every rendered element's computed view-transition-name, excluding `none`.
 * Elements that are display:none (themselves or via an ancestor) are not
 * captured by a transition, so they are left out: that is how the mobile
 * header and the desktop sidebar share a page without clashing.
 */
function renderedTransitionNames(page) {
  return page.evaluate(() =>
    [...document.querySelectorAll('*')]
      .filter((el) => el.getClientRects().length > 0)
      .map((el) => getComputedStyle(el).viewTransitionName)
      .filter((name) => name && name !== 'none')
  );
}

/** Computed view-transition-name of the first element matching `selector`. */
function transitionNameOf(page, selector) {
  return page.locator(selector).first().evaluate((el) => getComputedStyle(el).viewTransitionName);
}

module.exports = { recordPageReveal, pageReveal, renderedTransitionNames, transitionNameOf };
