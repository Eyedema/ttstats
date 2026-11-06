const { expect } = require('@playwright/test');

/**
 * Helpers for the pull-to-refresh specs (KAN-126). Not a spec: no project
 * matches this file name.
 */

/** Make the page believe it is the installed app, on every navigation
 *  (including the reload a pull triggers). iOS reports standalone through
 *  navigator.standalone; other engines through the display-mode query. */
async function emulateStandalone(page) {
  await page.addInitScript(() => {
    const real = window.matchMedia.bind(window);
    window.matchMedia = (q) =>
      /display-mode:\s*standalone/.test(q)
        ? { matches: true, media: q, onchange: null, addEventListener() {}, removeEventListener() {},
            addListener() {}, removeListener() {}, dispatchEvent() { return false; } }
        : real(q);
    Object.defineProperty(navigator, 'standalone', { configurable: true, get: () => true });
  });
}

/** Where every synthetic drag starts: mid-screen, just under the header. */
const ORIGIN = { x: 195, y: 220 };

/** Dispatch one touch event `dy`/`dx` px from ORIGIN, in the page.
 *  Uses a real TouchEvent where the engine can construct one, else an Event
 *  carrying the same `touches` shape; the script reads nothing else.
 *  Returns whether a listener called preventDefault(). */
function fireInPage({ type, dx, dy, origin }) {
  const x = origin.x + dx;
  const y = origin.y + dy;
  if (type === 'touchstart') {
    window.__ptrTarget = document.elementFromPoint(origin.x, origin.y) || document.body;
  }
  const target = window.__ptrTarget || document.body;
  const ending = type === 'touchend' || type === 'touchcancel';
  let ev;
  try {
    const t = new Touch({ identifier: 1, target, clientX: x, clientY: y, pageX: x, pageY: y });
    ev = new TouchEvent(type, {
      bubbles: true, cancelable: true,
      touches: ending ? [] : [t], targetTouches: ending ? [] : [t], changedTouches: [t],
    });
  } catch (e) {
    const t = { identifier: 1, target, clientX: x, clientY: y, pageX: x, pageY: y };
    ev = new Event(type, { bubbles: true, cancelable: true });
    Object.defineProperty(ev, 'touches', { value: ending ? [] : [t] });
    Object.defineProperty(ev, 'changedTouches', { value: [t] });
  }
  target.dispatchEvent(ev);
  return ev.defaultPrevented;
}

/** One touch event, for specs that inspect the indicator mid-gesture. */
async function touch(page, type, dy, dx = 0) {
  try {
    return await page.evaluate(fireInPage, { type, dx, dy, origin: ORIGIN });
  } catch (err) {
    // The release can start the reload before evaluate() returns.
    if (type === 'touchend' && /context|navigat|destroyed/i.test(String(err))) return false;
    throw err;
  }
}

/** A whole drag: start, eight moves, end. Resolves to whether any move was
 *  cancelled (i.e. whether the script took the gesture over). One evaluate
 *  per event: rebuilding the helper in-page would need new Function(), which
 *  the production CSP refuses. */
async function pull(page, { dx = 0, dy }) {
  let prevented = false;
  await touch(page, 'touchstart', 0);
  for (let i = 1; i <= 8; i++) {
    prevented = (await touch(page, 'touchmove', (dy * i) / 8, (dx * i) / 8)) || prevented;
  }
  await touch(page, 'touchend', dy, dx);
  return prevented;
}

/** Mark the current document, and count main-frame navigations from now on. */
async function watchForReload(page) {
  await page.evaluate(() => { window.__ptrMarker = true; });
  let navigations = 0;
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) navigations += 1;
  });
  return {
    async happened() {
      await expect.poll(() => navigations, { timeout: 5000 }).toBeGreaterThan(0);
      await page.waitForLoadState('load');
      expect(await page.evaluate(() => window.__ptrMarker === true)).toBe(false);
    },
    async didNotHappen() {
      await page.waitForTimeout(800);
      expect(navigations).toBe(0);
      expect(await page.evaluate(() => window.__ptrMarker === true)).toBe(true);
    },
  };
}

function indicator(page) {
  return page.locator('#pull-refresh');
}

module.exports = { emulateStandalone, pull, touch, watchForReload, indicator };
