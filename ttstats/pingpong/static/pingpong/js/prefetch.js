/* Tap-intent prefetch (KAN-124 / S10.4).
 *
 * A tap's `touchstart` arrives ~100 ms before its `click`. On a 400 ms RTT
 * link that is 100 ms of request we can start early: fetch the page on
 * touchstart (mousedown for a mouse) and let the click's navigation be
 * answered from the HTTP cache. The server marks a request carrying
 * `Purpose: prefetch` as `private, max-age=5` (ttstats/prefetch.py); nothing
 * else it renders is cacheable, so a normal navigation is unchanged.
 *
 * Measured reuse (docs/decisions/kan-119-performance.md, "After S10.4"):
 * - Chromium joins the navigation to the prefetch even while it is still in
 *   flight, so the click is left alone.
 * - WebKit and Gecko reuse a prefetch only once it has finished; a navigation
 *   started while it is in flight is a second request on the same slow link.
 *   There, a click that lands mid-prefetch is held until the prefetch is in
 *   the cache, then navigates (a cache hit).
 * - An engine with no HTTP cache (Safari private browsing) cannot reuse at
 *   all. The first held navigation checks it really was a cache hit; if not,
 *   prefetching is switched off for the rest of the tab's session.
 *
 * ALLOWLIST, not a sweep of every link: only `a[data-prefetch]` (the tab bar
 * and the menu rows, every one an idempotent GET page). Belt and braces, a
 * code-level denylist refuses anything that mutates on GET or isn't a page.
 *
 * Plain JS, no inline script, no eval: works under prod's CSP. With JS off
 * nothing happens and the links are ordinary links.
 */
(function () {
  'use strict';

  var OFF_KEY = 'ttstats-prefetch-off';
  var HELD_KEY = 'ttstats-prefetch-held';
  var store;
  try { store = window.sessionStorage; } catch (e) { store = null; }

  // Did the last held navigation land here, and was it really a cache hit?
  // A hit reads deliveryType 'cache' (WebKit), transferSize 0 (Chromium,
  // Gecko) or encodedBodySize 0 (WebKit counts the cached headers in
  // transferSize). A response that crossed the network has none of these.
  if (store) {
    var held = store.getItem(HELD_KEY);
    if (held) {
      store.removeItem(HELD_KEY);
      var nav = performance.getEntriesByType && performance.getEntriesByType('navigation')[0];
      var hit = !nav || nav.deliveryType === 'cache' || nav.transferSize === 0 ||
        nav.encodedBodySize === 0;
      if (held === location.href.split('#')[0] && !hit) store.setItem(OFF_KEY, '1');
    }
    if (store.getItem(OFF_KEY)) return;
  }

  if (!window.fetch || !window.URL) return;
  var conn = navigator.connection;
  if (conn && conn.saveData) return;

  // Chromium (the only engine with navigator.userAgentData) joins a
  // navigation to an in-flight prefetch by itself.
  var JOINS_IN_FLIGHT = !!navigator.userAgentData;

  // Never fetched early, whatever the markup says. Matched on the pathname.
  var DENY = [
    /\/live\//,          // scoring: every GET under it belongs to a live flow
    /\/confirm\/?$/,     // match_confirm writes on GET
    /\/join\//,          // club invites: join/<token>
    /logout/,
    /\/admin\//,
    /^\/accounts\//,
    /^\/webauthn\//,
    /\/(edit|delete|start|register|unregister|convert|switch)\/?$/,
  ];

  // The server's max-age: past it the entry is stale, so prefetch again.
  var TTL_MS = 5000;
  // A held click navigates anyway after this, cache or not.
  var HOLD_CAP_MS = 8000;
  var pending = {}; // href -> { t, settled, done }
  var holding = false;

  function target(a) {
    if (!a || !a.hasAttribute('data-prefetch') || a.hasAttribute('data-no-prefetch')) return null;
    if (a.hasAttribute('download')) return null;
    if (a.target && a.target !== '_self') return null;
    var url;
    try { url = new URL(a.href, location.href); } catch (e) { return null; }
    if (url.origin !== location.origin) return null;
    if (url.pathname.indexOf('/pingpong/') !== 0) return null;
    for (var i = 0; i < DENY.length; i++) if (DENY[i].test(url.pathname)) return null;
    url.hash = '';
    // The page we are on: there is nothing to start early.
    if (url.href === location.href.split('#')[0]) return null;
    return url.href;
  }

  // Not on a struggling link (KAN-135): a prefetch would only spend the
  // scarce bandwidth the tapped page itself needs. Without connection.js,
  // carry on as before.
  function linkIsPoor() {
    var c = window.TTStatsConnection;
    return !!(c && typeof c.state === 'function' && c.state() !== 'online');
  }

  function prefetch(href) {
    if (linkIsPoor()) return;
    var p = pending[href];
    if (p && Date.now() - p.t < TTL_MS) return;
    var entry = { t: Date.now(), settled: false, done: null };
    entry.done = fetch(href, {
      credentials: 'same-origin',
      headers: { Purpose: 'prefetch' },
    })
      // Read the body: the cache entry is complete only once it has arrived.
      .then(function (r) { return r.ok ? r.arrayBuffer() : null; })
      .catch(function () { /* the click just navigates normally */ })
      .then(function () { entry.settled = true; });
    pending[href] = entry;
  }

  function link(e) {
    return e.target && e.target.closest && e.target.closest('a[href]');
  }

  function onIntent(e) {
    if (e.type === 'mousedown' && e.button !== 0) return;
    var href = target(link(e));
    if (href) prefetch(href);
  }

  function onClick(e) {
    if (holding) { e.preventDefault(); return; }
    if (JOINS_IN_FLIGHT || e.defaultPrevented || e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var href = target(link(e));
    var entry = href && pending[href];
    if (!entry || entry.settled || Date.now() - entry.t >= TTL_MS) return;

    e.preventDefault();
    holding = true;
    var gone = false;
    function go() {
      if (gone) return;
      gone = true;
      if (store) { try { store.setItem(HELD_KEY, href); } catch (err) { /* full */ } }
      location.assign(href);
    }
    entry.done.then(go);
    setTimeout(go, HOLD_CAP_MS);
  }

  document.addEventListener('touchstart', onIntent, { passive: true, capture: true });
  document.addEventListener('mousedown', onIntent, true);
  // Bubble phase on document, registered after base.html's inline progress
  // handler, so the progress bar has already armed when a click is held.
  document.addEventListener('click', onClick, false);
  // A page restored from the back/forward cache must accept taps again.
  window.addEventListener('pageshow', function () { holding = false; });
})();
