/* TTStats: warm the core pages in the background (KAN-136 / S12.5).
 *
 * The worker only saves pages the user opened (KAN-133). Arriving at the
 * venue with no signal, Today, the Table, your own page and your last few
 * matches should already be on the phone. So after an ONLINE page load, once
 * the browser is idle, this asks the worker to fetch them:
 *
 *   {type: 'warm', urls: [...]}
 *
 * The URLs come from the server (<script id="snapshot-warm"> via
 * json_script, pingpong/offline.py warm_urls): only rendered for a logged-in
 * viewer with the offline_snapshots flag on, at most six, every one an
 * allowlisted read-only page. The worker fetches them one by one (low
 * priority, header X-TTStats-Warm: 1), stores only what the server opted in
 * for the current partition, and rate-limits itself to once per 30 minutes
 * per partition; see sw.js.
 *
 * Asked only when ALL hold, checked at idle time, not at load:
 *   - <meta name="ttstats-snapshots" content="on">
 *   - this page is not itself a saved copy (html[data-freshness="snapshot"])
 *   - window.TTStatsConnection.state() === 'online' (KAN-135). No
 *     connection.js means no evidence the network is good: don't warm.
 *   - navigator.connection.saveData is not set
 *
 * Plain JS, no inline code (production CSP). UMD: in Node
 * (tests/js/snapshot_warm.test.mjs) it exports the pure parts, no DOM.
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.TTStatsWarm = api;
    api.boot(root);
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var WARM_MAX = 6;
  // requestIdleCallback's deadline, and the delay where it doesn't exist
  // (Safari before 18.x): after the load, never in its way.
  var IDLE_TIMEOUT_MS = 5000;
  var FALLBACK_DELAY_MS = 2000;

  /* The server's list, re-checked: path-only /pingpong/ URLs, no /live/,
   * each once, at most WARM_MAX. The worker checks again. */
  function warmUrls(raw) {
    if (!Array.isArray(raw)) return [];
    var seen = {};
    var urls = [];
    raw.forEach(function (url) {
      if (typeof url !== 'string' || url.indexOf('/pingpong/') !== 0) return;
      if (url.indexOf('/live/') !== -1 || seen[url]) return;
      seen[url] = true;
      urls.push(url);
    });
    return urls.slice(0, WARM_MAX);
  }

  /* env: {connectionState, saveData, freshness} */
  function shouldWarm(env) {
    if (!env) return false;
    if (env.freshness === 'snapshot') return false;
    if (env.connectionState !== 'online') return false;
    if (env.saveData) return false;
    return true;
  }

  function environment(win) {
    var connection = win.TTStatsConnection;
    var info = win.navigator.connection;
    return {
      connectionState:
        connection && typeof connection.state === 'function' ? connection.state() : null,
      saveData: Boolean(info && info.saveData),
      freshness: win.document.documentElement.getAttribute('data-freshness'),
    };
  }

  function post(nav, message) {
    var controller = nav.serviceWorker.controller;
    if (controller) {
      controller.postMessage(message);
      return;
    }
    nav.serviceWorker.ready.then(function (registration) {
      if (registration.active) registration.active.postMessage(message);
    });
  }

  function boot(win) {
    var doc = win.document;
    var nav = win.navigator;
    if (!doc || !nav || !('serviceWorker' in nav)) return;
    var meta = doc.querySelector('meta[name="ttstats-snapshots"]');
    if (!meta || meta.getAttribute('content') !== 'on') return;
    var source = doc.getElementById('snapshot-warm');
    if (!source) return;
    var urls;
    try {
      urls = warmUrls(JSON.parse(source.textContent));
    } catch (err) {
      return;
    }
    if (!urls.length) return;

    var idle = win.requestIdleCallback
      ? function (fn) {
          win.requestIdleCallback(fn, { timeout: IDLE_TIMEOUT_MS });
        }
      : function (fn) {
          win.setTimeout(fn, FALLBACK_DELAY_MS);
        };
    function start() {
      idle(function () {
        if (shouldWarm(environment(win))) post(nav, { type: 'warm', urls: urls });
      });
    }
    if (doc.readyState === 'complete') start();
    else win.addEventListener('load', start, { once: true });
  }

  return {
    WARM_MAX: WARM_MAX,
    warmUrls: warmUrls,
    shouldWarm: shouldWarm,
    boot: boot,
  };
});
