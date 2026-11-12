/* Read-only mode on a saved copy (KAN-146, S12.3 / KAN-134 section 4).
 *
 * The service worker serves a saved copy of a read-only page when the
 * network is gone or crawling, and marks it <html data-freshness="snapshot">
 * (sw.js). CSS alone shows the "saved copy" banner and mutes every amber
 * cue (app.css). This file is the JS half, and does nothing at all on a
 * page the network served:
 *
 *   1. Nothing on a saved copy reaches the server by accident. Capture-phase
 *      `submit` and `click` handlers stop form submits, htmx triggers and
 *      links to pages that cannot open offline, and say "Needs a connection"
 *      (TTStatsToast, connection.js) instead: no request goes out. Links to
 *      pages that can (offline.OFFLINE_NAVIGABLE, rendered as regex sources
 *      by `json_script` id "snapshot-paths") are left alone; the worker tries
 *      the network, then its copy. htmx requests that no click starts (a
 *      `load` or polling trigger) are cancelled silently in htmx:confirm and
 *      htmx:beforeRequest, so they can neither hang nor storm toasts.
 *      Log out: the POST cannot reach the server, so the session stays and
 *      the toast says so; offline.js's own capture listener still sends the
 *      worker {type:'purge'} (this one never stops it), so the saved pages
 *      leave the phone anyway. Wiping local copies needs no server, and on a
 *      shared phone that is what "log out" was for.
 *   2. "You're back online · Refresh": on the worker's `snapshot-fresh`
 *      (offline.js re-dispatches it as ttstats:snapshot-fresh), or when a
 *      cheap probe gets a 2xx: HEAD of /healthz (KAN-109: answered ahead of
 *      the middleware stack, never cached, never answered by the worker,
 *      which only takes GETs under /pingpong/ and /static/; a 503 means the
 *      server is up but cannot serve, so it is not "back"),
 *      `cache: 'no-store'`, on the `online` event, on return to the tab and
 *      every PROBE_MS while visible. Never an automatic reload:
 *      the user may be reading. The button is created here, so without JS
 *      it does not exist.
 *   3. The banner's absolute time ("14:32", written by the worker, the only
 *      thing shown without JS) gains a relative age, "(12 min ago)",
 *      refreshed every TICK_MS.
 *
 * Plain JS, no inline code, no eval: runs under the production CSP. UMD: in
 * Node (tests/js/snapshot_mode.test.mjs) it exports the pure helpers and
 * touches no DOM.
 */
(function (root, factory) {
  'use strict';
  var core = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = core;
  } else if (root && root.document) {
    core.boot(root);
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var TICK_MS = 30000;      // relative time refresh
  var PROBE_MS = 30000;     // "are we back?" while the page is visible
  var PROBE_TIMEOUT_MS = 5000;
  var PROBE_URL = '/healthz';
  var NEEDS = 'Needs a connection';
  var HTMX_VERBS = ['get', 'post', 'put', 'patch', 'delete'];
  var HTMX_SELECTOR = HTMX_VERBS.map(function (v) {
    return '[hx-' + v + '],[data-hx-' + v + ']';
  }).join(',');
  // htmx triggers nobody tapped: never worth a toast.
  var UNPROMPTED = ['load', 'revealed', 'intersect'];

  /* The regex sources from json_script, compiled; a bad one is skipped
   * (it can only make a link "needs a connection", never let one through). */
  function compilePatterns(sources) {
    var out = [];
    (sources || []).forEach(function (source) {
      try { out.push(new RegExp(String(source))); } catch (e) { /* skipped */ }
    });
    return out;
  }

  /* 'pass' (leave the click alone) or 'block' (needs a connection).
   *   href      the link's resolved href
   *   current   the page's own URL
   *   patterns  compilePatterns() output */
  function classifyLink(href, current, patterns) {
    var url, here;
    try {
      here = new URL(current);
      url = new URL(href, here);
    } catch (e) {
      return 'pass';
    }
    // mailto:, tel:, javascript: ... nothing of ours goes to the network.
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return 'pass';
    if (url.origin !== here.origin) return 'block';
    // Same document, another fragment: no request at all.
    if (url.pathname === here.pathname && url.search === here.search && url.hash) return 'pass';
    for (var i = 0; i < patterns.length; i++) {
      if (patterns[i].test(url.pathname)) return 'pass';
    }
    return 'block';
  }

  /* "just now", "1 min ago", "12 min ago", "3 h ago", "1 day ago", "8 days ago" */
  function relativeAge(savedAt, now) {
    var minutes = Math.floor(Math.max(0, now - savedAt) / 60000);
    if (minutes < 1) return 'just now';
    if (minutes < 60) return minutes + ' min ago';
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + ' h ago';
    var days = Math.floor(hours / 24);
    return days + (days === 1 ? ' day ago' : ' days ago');
  }

  /* Does an htmx:confirm / beforeRequest come from something the user did? */
  function userTriggered(event) {
    return !!(event && event.type && UNPROMPTED.indexOf(event.type) === -1);
  }

  // --- Browser wiring ------------------------------------------------------

  function boot(win) {
    var doc = win.document;
    var html = doc.documentElement;
    if (html.getAttribute('data-freshness') !== 'snapshot') return;

    var patterns = [];
    var blob = doc.getElementById('snapshot-paths');
    if (blob) {
      try { patterns = compilePatterns(JSON.parse(blob.textContent)); } catch (e) { patterns = []; }
    }
    var online = false;

    function toast(message) {
      var toasts = win.TTStatsToast;
      if (!toasts || typeof toasts.show !== 'function') return;
      if (online) {
        toasts.show('Refresh to get the live page first', {
          icon: 'refresh-cw',
          action: { label: 'Refresh', onClick: function () { win.location.reload(); } },
        });
      } else {
        toasts.show(message || NEEDS, { icon: 'wifi-off' });
      }
    }

    function stop(event, message) {
      event.preventDefault();
      // Not stopImmediatePropagation: offline.js's capture listener (logout
      // purge) must still run. stopPropagation keeps htmx's own listeners on
      // the element from ever seeing the event.
      event.stopPropagation();
      toast(message);
    }

    doc.addEventListener('submit', function (event) {
      var form = event.target;
      if (form && form.hasAttribute && form.hasAttribute('data-sw-purge')) {
        stop(event, 'Needs a connection to log out. Saved pages were removed from this phone.');
        return;
      }
      stop(event);
    }, true);

    doc.addEventListener('click', function (event) {
      if (event.defaultPrevented || event.button !== 0) return;
      var t = event.target;
      if (!t || !t.closest) return;
      // An htmx trigger that a click fires. A form (or a control inside one)
      // fires on submit/change, which the submit handler and the htmx guard
      // below cover; blocking the click would stop a <select> from opening.
      var hx = t.closest(HTMX_SELECTOR);
      if (hx && !/^(FORM|INPUT|SELECT|TEXTAREA|OPTION)$/.test(hx.tagName)) {
        stop(event);
        return;
      }
      var a = t.closest('a[href]');
      if (a && classifyLink(a.href, win.location.href, patterns) === 'block') stop(event);
    }, true);

    // htmx: cancel every request on a saved copy. Capture phase, so it runs
    // before connection.js's bubble-phase bookkeeping, which then never
    // counts a request that was never sent.
    function cancelHtmx(event) {
      event.preventDefault();
      event.stopPropagation();
      var detail = event.detail || {};
      if (userTriggered(detail.triggeringEvent || (detail.requestConfig && detail.requestConfig.triggeringEvent))) {
        toast();
      }
    }
    doc.addEventListener('htmx:confirm', cancelHtmx, true);
    doc.addEventListener('htmx:beforeRequest', cancelHtmx, true);

    // --- the banner: relative time and "back online" ---------------------

    var banner = doc.querySelector('[data-snapshot-banner]');
    var time = doc.querySelector('time[data-snapshot-time]');
    var savedAt = Date.parse(html.getAttribute('data-snapshot-at') || (time && time.getAttribute('datetime')) || '');
    var relative = null;

    function tick() {
      if (!time || isNaN(savedAt)) return;
      if (!relative) {
        relative = doc.createElement('span');
        relative.setAttribute('data-snapshot-relative', '');
        time.parentNode.insertBefore(relative, time.nextSibling);
      }
      relative.textContent = ' (' + relativeAge(savedAt, Date.now()) + ')';
    }

    function backOnline() {
      if (online) return;
      online = true;
      html.setAttribute('data-snapshot-online', '');
      if (!banner) return;
      var text = banner.querySelector('[data-snapshot-text]');
      if (!text) return;
      text.hidden = true;
      var use = banner.querySelector('svg use');
      if (use) use.setAttribute('href', '#i-refresh-cw');
      var words = doc.createElement('span');
      words.setAttribute('data-snapshot-online-text', '');
      words.textContent = 'You’re back online';
      var button = doc.createElement('button');
      button.type = 'button';
      button.className = 'snapshot-refresh';
      button.setAttribute('data-snapshot-refresh', '');
      button.textContent = 'Refresh';
      button.addEventListener('click', function () { win.location.reload(); });
      text.parentNode.insertBefore(words, text.nextSibling);
      words.parentNode.insertBefore(button, words.nextSibling);
    }

    var probing = false;
    function probe() {
      if (online || probing || !win.fetch) return;
      if (doc.visibilityState === 'hidden') return;
      if (win.navigator && win.navigator.onLine === false) return;
      probing = true;
      var controller = win.AbortController ? new win.AbortController() : null;
      var timer = setTimeout(function () { if (controller) controller.abort(); }, PROBE_TIMEOUT_MS);
      win.fetch(PROBE_URL, {
        method: 'HEAD',
        cache: 'no-store',
        credentials: 'same-origin',
        signal: controller ? controller.signal : undefined,
      })
        .then(function (response) { if (response.ok) backOnline(); })
        .catch(function () { /* still offline */ })
        .then(function () { clearTimeout(timer); probing = false; });
    }

    doc.addEventListener('ttstats:snapshot-fresh', function (event) {
      var url = event.detail && event.detail.url;
      if (url && url.split('#')[0] !== win.location.href.split('#')[0]) return;
      backOnline();
    });
    // The message may have landed before this script ran (offline.js marks
    // the root either way).
    if (html.hasAttribute('data-snapshot-fresh')) backOnline();

    win.addEventListener('online', probe);
    doc.addEventListener('visibilitychange', probe);

    tick();
    setInterval(function () { tick(); probe(); }, Math.min(TICK_MS, PROBE_MS));
  }

  return {
    TICK_MS: TICK_MS,
    PROBE_MS: PROBE_MS,
    PROBE_URL: PROBE_URL,
    compilePatterns: compilePatterns,
    classifyLink: classifyLink,
    relativeAge: relativeAge,
    userTriggered: userTriggered,
    boot: boot,
  };
});
