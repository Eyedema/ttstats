/* TTStats: tell the service worker whose page this is (KAN-82).
 *
 * Loaded on every page, logged in or out (base.html, base_auth.html). The
 * worker keeps copies of the live scoreboard so it opens with no signal, and
 * those copies are one person's: they carry that session's CSRF token and
 * player ids. Two people can share a phone. So:
 *
 *   - Every page load sends {type: 'identity', identity} from
 *     <meta name="ttstats-identity"> (an opaque HMAC of user + active club,
 *     pingpong/offline.py). The worker deletes every copy filed under any
 *     other identity; an empty identity (logged out) deletes them all.
 *   - Submitting a form marked data-sw-purge (Log out) sends {type: 'purge'}
 *     first, so the copies go even if the logout POST never reaches the
 *     server. The login page's empty identity is the backstop.
 *
 * Plain JS, no inline code, no framework: it must run under the production
 * CSP and before anything else could fail. It does nothing without a worker.
 */
(function () {
  'use strict';

  if (!('serviceWorker' in navigator)) return;
  var meta = document.querySelector('meta[name="ttstats-identity"]');
  if (!meta) return;

  function post(message) {
    var controller = navigator.serviceWorker.controller;
    if (controller) {
      controller.postMessage(message);
      return;
    }
    // Not controlled yet (first visit, or a hard reload): the registration's
    // active worker still owns the caches. `ready` never settles when no
    // worker is registered, which is the correct outcome: nothing to clear.
    navigator.serviceWorker.ready.then(function (registration) {
      if (registration.active) registration.active.postMessage(message);
    });
  }

  post({ type: 'identity', identity: meta.getAttribute('content') || '' });

  document.addEventListener(
    'submit',
    function (event) {
      var form = event.target;
      if (form && form.hasAttribute && form.hasAttribute('data-sw-purge')) {
        post({ type: 'purge' });
      }
    },
    true
  );
})();
