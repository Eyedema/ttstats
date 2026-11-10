/*
 * live_lobby.js -- the host's shared-scoring lobby (KAN-151 / KAN-164).
 *
 * Plain JS, loaded `defer`, config from the "live-lobby-config" json_script.
 * Nothing inline, no eval: CSP-clean.
 *
 * - Polls the status URL every interval_ms while the page is visible, and
 *   stops while it is hidden. "joined" -> go to the scoreboard; "expired" /
 *   "cancelled" -> show the reason and stop.
 * - Share (navigator.share) and Copy link (Clipboard API) start `hidden` and
 *   are revealed only when the browser has them: they fail closed. Without
 *   this script the page still has the QR code, the plain URL and
 *   "Check again".
 *
 * Loads as a classic script (window.LiveLobby, auto-inits on the page) or via
 * require() in Node for tests/js/live_lobby.test.mjs.
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.LiveLobby = api;
    if (root.document) {
      if (root.document.readyState === 'loading') {
        root.document.addEventListener('DOMContentLoaded', function () { api.init(root); });
      } else {
        api.init(root);
      }
    }
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  // What a status payload means for the page. Pure, so Node can test it.
  function decide(payload) {
    if (!payload || typeof payload !== 'object') return { action: 'wait' };
    if (payload.status === 'joined' && payload.match_url) {
      return { action: 'navigate', url: payload.match_url };
    }
    if (payload.status === 'expired' || payload.status === 'cancelled') {
      return { action: 'ended', reason: payload.reason || 'This code no longer works.' };
    }
    return { action: 'wait' };
  }

  // The poll loop: tick() once per interval while visible. Injected timers
  // and fetch keep it testable. Returns {start, stop, tick}.
  function createPoller(opts) {
    var timer = null;
    var stopped = false;
    var inFlight = false;

    function schedule() {
      if (stopped || timer !== null || !opts.isVisible()) return;
      timer = opts.setTimeout(function () {
        timer = null;
        tick();
      }, opts.interval);
    }

    function tick() {
      if (stopped || inFlight) return Promise.resolve();
      inFlight = true;
      return opts.fetchStatus().then(function (payload) {
        var verdict = decide(payload);
        if (verdict.action === 'wait') return;
        stop();
        opts.onVerdict(verdict);
      }, function () {
        // Offline or a blip: keep waiting, the next tick retries.
      }).then(function () {
        inFlight = false;
        schedule();
      });
    }

    function stop() {
      stopped = true;
      if (timer !== null) opts.clearTimeout(timer);
      timer = null;
    }

    function pause() {
      if (timer !== null) opts.clearTimeout(timer);
      timer = null;
    }

    return { start: schedule, stop: stop, pause: pause, tick: tick };
  }

  function init(win) {
    var doc = win.document;
    var root = doc.querySelector('[data-live-lobby]');
    var configEl = doc.getElementById('live-lobby-config');
    if (!root || !configEl) return;
    var config;
    try {
      config = JSON.parse(configEl.textContent);
    } catch (e) {
      return;
    }

    var shareBtn = root.querySelector('[data-lobby-share]');
    var copyBtn = root.querySelector('[data-lobby-copy]');
    var nav = win.navigator;

    if (shareBtn && nav && typeof nav.share === 'function') {
      shareBtn.hidden = false;
      shareBtn.addEventListener('click', function () {
        nav.share({ title: config.share_title, text: config.share_text, url: config.join_url })
          .catch(function () { /* dismissed */ });
      });
    }

    if (copyBtn && nav && nav.clipboard && typeof nav.clipboard.writeText === 'function') {
      var label = copyBtn.querySelector('[data-copy-label]');
      var copied = copyBtn.querySelector('[data-copied]');
      var resetTimer = null;
      copyBtn.hidden = false;
      copyBtn.addEventListener('click', function () {
        nav.clipboard.writeText(config.join_url).then(function () {
          if (label) label.hidden = true;
          if (copied) copied.hidden = false;
          if (resetTimer) win.clearTimeout(resetTimer);
          resetTimer = win.setTimeout(function () {
            if (label) label.hidden = false;
            if (copied) copied.hidden = true;
          }, 2500);
        }, function () { /* permission refused: the URL text is still there */ });
      });
    }

    var statusEl = root.querySelector('[data-lobby-status]');
    var endedEl = root.querySelector('[data-lobby-ended]');
    var reasonEl = root.querySelector('[data-lobby-reason]');

    var poller = createPoller({
      interval: config.interval_ms || 2000,
      isVisible: function () { return doc.visibilityState !== 'hidden'; },
      setTimeout: function (fn, ms) { return win.setTimeout(fn, ms); },
      clearTimeout: function (t) { win.clearTimeout(t); },
      fetchStatus: function () {
        return win.fetch(config.status_url, {
          credentials: 'same-origin',
          headers: { Accept: 'application/json' },
          cache: 'no-store',
        }).then(function (resp) {
          if (!resp.ok) throw new Error('status ' + resp.status);
          return resp.json();
        });
      },
      onVerdict: function (verdict) {
        if (verdict.action === 'navigate') {
          if (statusEl) statusEl.textContent = 'Your opponent joined. Opening the scoreboard…';
          win.location.assign(verdict.url);
        } else if (verdict.action === 'ended') {
          if (reasonEl) reasonEl.textContent = verdict.reason;
          if (endedEl) endedEl.hidden = false;
          if (statusEl) statusEl.textContent = verdict.reason;
        }
      },
    });

    doc.addEventListener('visibilitychange', function () {
      if (doc.visibilityState === 'hidden') {
        poller.pause();
      } else {
        // Back in view: ask at once rather than after a full interval.
        poller.tick();
      }
    });
    poller.start();
  }

  return Object.freeze({ decide: decide, createPoller: createPoller, init: init });
});
