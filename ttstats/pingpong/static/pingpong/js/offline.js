/* TTStats: tell the service worker whose page this is (KAN-82, KAN-133).
 *
 * Loaded on every page, logged in or out (base.html, base_auth.html). The
 * worker keeps copies of the live scoreboard so it opens with no signal, and
 * saved copies of read-only pages (snapshots, KAN-131). Those copies are one
 * person's: they carry that session's CSRF token and data. Two people can
 * share a phone. So:
 *
 *   - Every page load sends {type: 'identity', identity, snapshots} from
 *     <meta name="ttstats-identity"> (an opaque HMAC of user + active club,
 *     pingpong/offline.py) and <meta name="ttstats-snapshots"> ('on' when the
 *     offline_snapshots flag is on for this viewer). The worker deletes every
 *     copy filed under any other identity; an empty identity (logged out)
 *     deletes them all; snapshots 'off' (the kill switch) deletes every
 *     snapshot.
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

  var snapshotsMeta = document.querySelector('meta[name="ttstats-snapshots"]');
  var snapshots =
    snapshotsMeta && snapshotsMeta.getAttribute('content') === 'on' ? 'on' : 'off';

  post({
    type: 'identity',
    identity: meta.getAttribute('content') || '',
    snapshots: snapshots,
  });

  // Saved copies are worth keeping through storage pressure. Best effort:
  // the browser may say no, and nothing depends on the answer.
  if (snapshots === 'on' && navigator.storage && navigator.storage.persist) {
    navigator.storage
      .persisted()
      .then(function (persisted) {
        return persisted || navigator.storage.persist();
      })
      .catch(function () {});
  }

  // The worker served a saved copy, then the network answered after all and
  // a fresh copy is stored. Re-dispatched for the page (the "Refresh"
  // prompt is KAN-146's); the attribute lets CSS react without JS state.
  navigator.serviceWorker.addEventListener('message', function (event) {
    var data = event.data || {};
    if (data.type !== 'snapshot-fresh') return;
    document.documentElement.setAttribute('data-snapshot-fresh', '');
    document.dispatchEvent(
      new CustomEvent('ttstats:snapshot-fresh', { detail: { url: data.url } })
    );
  });
  if (navigator.serviceWorker.startMessages) navigator.serviceWorker.startMessages();

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
