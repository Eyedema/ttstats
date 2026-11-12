/* Poor-signal detection for in-page requests (KAN-135 / S12.4).
 *
 * At the venue the 4G is very slow or gone, and an htmx swap (a leaderboard
 * filter, a season tab) used to hang with no feedback: the screen looked
 * frozen. This file does three things:
 *
 *   1. A connection state, `online | degraded | offline`, decided from what
 *      in-page requests actually did. `navigator.onLine` is only a hint:
 *      iOS says `true` on lie-fi (a mast, no throughput).
 *        offline   navigator.onLine === false, or the last OFFLINE_AFTER (2)
 *                  requests failed with a network error
 *        degraded  the last request took SLOW_MS (2500) or more, is still
 *                  in flight after SLOW_MS, timed out, or failed once
 *        online    a request completed in under SLOW_MS (or the browser
 *                  reported it came back online)
 *      Exposed as html[data-connection], a `ttstats:connection` CustomEvent
 *      on document ({detail: {state, previous}}) and
 *      window.TTStatsConnection.state(), which prefetch.js and
 *      pull_to_refresh.js read.
 *   2. htmx: requests time out after TIMEOUT_MS (8000) instead of never.
 *      A send error, a timeout or a 5xx gets an honest toast ("No connection
 *      -- try again"), and the trigger is restored (no stuck spinner, nothing
 *      left disabled). Never an automatic retry; a GET's toast offers one
 *      "Try again" button.
 *   3. A reusable toast, window.TTStatsToast.show(message, options), which
 *      S12.3 also uses ("Needs a connection").
 *
 * The live scoreboard and the shared-scoring pages (anything under /live/)
 * are EXEMPT: KAN-76 owns their sync and their own error UI, so here they get
 * no htmx hooks, no timeout and no toasts.
 *
 * Fails closed: the toast region and the "Slow connection" line are created
 * by this script, so with no JS there is nothing to show. The line is also
 * off unless <meta name="ttstats-snapshots" content="on"> (KAN-132's flag).
 *
 * Plain JS, no inline code, no eval: runs under the production CSP. UMD: in
 * Node (tests/js/connection.test.mjs) it exports the pure state machine and
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

  var SLOW_MS = 2500;      // a request this slow means the link is struggling
  var TIMEOUT_MS = 8000;   // htmx.config.timeout: past this, give up honestly
  var OFFLINE_AFTER = 2;   // consecutive network errors that mean "offline"
  var TOAST_MS = 6000;     // a toast's life; one with an action gets longer
  var TOAST_ACTION_MS = 10000;

  var ONLINE = 'online', DEGRADED = 'degraded', OFFLINE = 'offline';

  /* The pure state machine. No DOM, no timers: the caller passes a clock and
   * reports outcomes. Every method returns the state after it.
   *
   *   begin(key)          a request started
   *   end(key, outcome)   it finished: 'response' (any HTTP status: the
   *                       network worked), 'network', 'timeout' or 'abort'
   *   tick()              re-check in-flight requests against SLOW_MS
   *   setOnLine(bool)     navigator.onLine / the online and offline events
   */
  function createMachine(options) {
    options = options || {};
    var now = options.now || function () { return Date.now(); };
    var slowMs = options.slowMs || SLOW_MS;
    var onLine = options.onLine !== false;
    var fromRequests = ONLINE;
    var failures = 0;
    var inFlight = new Map();

    function state() {
      if (!onLine || failures >= OFFLINE_AFTER) return OFFLINE;
      return fromRequests;
    }

    function begin(key) {
      inFlight.set(key, now());
      return state();
    }

    function end(key, outcome) {
      if (!inFlight.has(key)) return state(); // already counted (htmx re-fires afterRequest)
      var took = now() - inFlight.get(key);
      inFlight.delete(key);
      if (outcome === 'abort') return state(); // the page cancelled it: no evidence
      if (outcome === 'network') {
        failures += 1;
        fromRequests = DEGRADED;
      } else if (outcome === 'timeout') {
        failures = 0;
        fromRequests = DEGRADED;
      } else {
        failures = 0;
        fromRequests = took >= slowMs ? DEGRADED : ONLINE;
      }
      return state();
    }

    function tick() {
      var t = now();
      inFlight.forEach(function (started) {
        if (t - started >= slowMs) fromRequests = DEGRADED;
      });
      return state();
    }

    function setOnLine(value) {
      var was = onLine;
      onLine = value !== false;
      // A real offline -> online transition is fresh evidence; iOS lie-fi
      // never fires it, so this cannot mask a hanging link.
      if (onLine && !was) {
        failures = 0;
        fromRequests = ONLINE;
      }
      return state();
    }

    return {
      state: state,
      begin: begin,
      end: end,
      tick: tick,
      setOnLine: setOnLine,
      inFlight: function () { return inFlight.size; },
    };
  }

  /* The toast message for a failed htmx request, or null for none. */
  function messageFor(kind, status) {
    if (kind === 'network') return { text: 'No connection — try again', icon: 'wifi-off' };
    if (kind === 'timeout') return { text: 'Connection is very slow — try again', icon: 'clock' };
    if (kind === 'response' && status >= 500) {
      return { text: 'Something went wrong on our side — try again', icon: 'circle-alert' };
    }
    return null;
  }

  /* Pages KAN-76 owns: the scoreboard, the lobby, the join link. */
  function isExempt(pathname, doc) {
    if (/\/live\//.test(pathname || '')) return true;
    return !!(doc && (doc.getElementById('scoreboard') || doc.querySelector('[data-live-lobby]')));
  }

  // --- Browser wiring ------------------------------------------------------

  function createToasts(win) {
    var doc = win.document;
    var region = null, current = null, timer = null;

    function ensureRegion() {
      if (region && region.isConnected) return region;
      region = doc.createElement('div');
      region.id = 'ttstats-toasts';
      region.className = 'toast-region';
      region.setAttribute('role', 'status');
      region.setAttribute('aria-live', 'polite');
      doc.body.appendChild(region);
      return region;
    }

    function dismiss() {
      clearTimeout(timer);
      if (current) current.remove();
      current = null;
    }

    function svg(icon, cls) {
      var ns = 'http://www.w3.org/2000/svg';
      var s = doc.createElementNS(ns, 'svg');
      s.setAttribute('aria-hidden', 'true');
      s.setAttribute('class', cls);
      var use = doc.createElementNS(ns, 'use');
      use.setAttribute('href', '#i-' + icon);
      s.appendChild(use);
      return s;
    }

    /* show(message, {icon, action: {label, onClick}, duration}) -> {dismiss}
     * One toast at a time: a new one replaces the last. */
    function show(message, opts) {
      opts = opts || {};
      dismiss();
      ensureRegion();
      var el = doc.createElement('div');
      el.className = 'toast';
      el.setAttribute('data-toast', '');
      if (opts.icon !== null) el.appendChild(svg(opts.icon || 'info', 'toast-icon'));
      var text = doc.createElement('span');
      text.className = 'toast-text';
      text.textContent = message;
      el.appendChild(text);
      if (opts.action && opts.action.label) {
        var act = doc.createElement('button');
        act.type = 'button';
        act.className = 'toast-action';
        act.textContent = opts.action.label;
        act.addEventListener('click', function () {
          dismiss();
          if (typeof opts.action.onClick === 'function') opts.action.onClick();
        });
        el.appendChild(act);
      }
      var close = doc.createElement('button');
      close.type = 'button';
      close.className = 'toast-close';
      close.setAttribute('aria-label', 'Dismiss');
      close.appendChild(svg('x', 'toast-icon'));
      close.addEventListener('click', dismiss);
      el.appendChild(close);
      region.appendChild(el);
      current = el;
      var ms = opts.duration || (opts.action ? TOAST_ACTION_MS : TOAST_MS);
      timer = setTimeout(dismiss, ms);
      return { dismiss: function () { if (current === el) dismiss(); } };
    }

    return { show: show, dismiss: dismiss };
  }

  function boot(win) {
    var doc = win.document;
    var html = doc.documentElement;
    var nav = win.navigator || {};
    var machine = createMachine({ onLine: nav.onLine !== false });
    var exempt = isExempt(win.location && win.location.pathname, doc);
    var current = machine.state();
    var hint = null;

    var snapshotsMeta = doc.querySelector('meta[name="ttstats-snapshots"]');
    var hintOn = !exempt && !!snapshotsMeta && snapshotsMeta.getAttribute('content') === 'on';

    function renderHint() {
      if (!hintOn) return;
      if (!hint) {
        hint = doc.createElement('div');
        hint.id = 'connection-hint';
        hint.className = 'connection-hint';
        hint.setAttribute('role', 'status');
        hint.hidden = true;
        hint.textContent = 'Slow connection';
        doc.body.appendChild(hint);
      }
      hint.hidden = current !== DEGRADED;
    }

    function apply(next) {
      if (next === current && html.getAttribute('data-connection') === next) return;
      var previous = current;
      current = next;
      html.setAttribute('data-connection', next);
      renderHint();
      if (previous !== next) {
        var ev;
        try {
          ev = new win.CustomEvent('ttstats:connection', { detail: { state: next, previous: previous } });
        } catch (e) { ev = null; }
        if (ev) doc.dispatchEvent(ev);
      }
    }

    var toasts = createToasts(win);
    win.TTStatsToast = toasts;
    win.TTStatsConnection = {
      SLOW_MS: SLOW_MS,
      TIMEOUT_MS: TIMEOUT_MS,
      exempt: exempt,
      // onLine === false is trustworthy (only `true` lies), and the browser
      // does not always fire `offline` for it: re-read it on every ask.
      state: function () {
        if (nav.onLine === false && current !== OFFLINE) apply(machine.setOnLine(false));
        return current;
      },
    };

    apply(current);
    win.addEventListener('online', function () { apply(machine.setOnLine(true)); });
    win.addEventListener('offline', function () { apply(machine.setOnLine(false)); });

    if (exempt) return;

    function setTimeoutConfig() {
      if (win.htmx && win.htmx.config && !win.htmx.config.timeout) win.htmx.config.timeout = TIMEOUT_MS;
    }
    // Never touch window.htmx at parse time (frontend.md, KAN-122).
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', setTimeoutConfig);
    else setTimeoutConfig();

    doc.addEventListener('htmx:beforeRequest', function (e) {
      var xhr = e.detail && e.detail.xhr;
      if (!xhr) return;
      apply(machine.begin(xhr));
      setTimeout(function () { apply(machine.tick()); }, SLOW_MS);
    });

    // afterRequest fires for every outcome; a status of 0 means no response
    // came back, and the specific error event below says why.
    doc.addEventListener('htmx:afterRequest', function (e) {
      var xhr = e.detail && e.detail.xhr;
      if (xhr && xhr.status) apply(machine.end(xhr, 'response'));
    });

    // htmx already restores its own indicators; this is the belt and braces
    // for anything left over once nothing is in flight.
    function restore() {
      setTimeout(function () {
        if (machine.inFlight()) return;
        doc.querySelectorAll('.htmx-request').forEach(function (n) { n.classList.remove('htmx-request'); });
        doc.querySelectorAll('[data-disabled-by-htmx]').forEach(function (n) {
          n.removeAttribute('disabled');
          n.removeAttribute('data-disabled-by-htmx');
        });
      }, 0);
    }

    function fail(kind) {
      return function (e) {
        var d = e.detail || {};
        var xhr = d.xhr;
        if (kind !== 'response' && xhr) apply(machine.end(xhr, kind));
        if (kind === 'abort') return;
        var msg = messageFor(kind, xhr ? xhr.status : 0);
        restore();
        if (!msg) return;
        var verb = d.requestConfig && d.requestConfig.verb;
        var path = d.pathInfo && d.pathInfo.requestPath;
        var source = d.elt;
        var action = null;
        if (String(verb).toLowerCase() === 'get' && path && source && source.isConnected && win.htmx) {
          action = {
            label: 'Try again',
            onClick: function () {
              if (source.isConnected) win.htmx.ajax('GET', path, { source: source });
            },
          };
        }
        toasts.show(msg.text, { icon: msg.icon, action: action });
      };
    }

    doc.addEventListener('htmx:sendError', fail('network'));
    doc.addEventListener('htmx:timeout', fail('timeout'));
    doc.addEventListener('htmx:sendAbort', fail('abort'));
    doc.addEventListener('htmx:responseError', fail('response'));
  }

  return {
    SLOW_MS: SLOW_MS,
    TIMEOUT_MS: TIMEOUT_MS,
    OFFLINE_AFTER: OFFLINE_AFTER,
    ONLINE: ONLINE,
    DEGRADED: DEGRADED,
    OFFLINE: OFFLINE,
    createMachine: createMachine,
    messageFor: messageFor,
    isExempt: isExempt,
    boot: boot,
  };
});
