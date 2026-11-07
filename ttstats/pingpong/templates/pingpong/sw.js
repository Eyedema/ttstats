{% load static %}/* TTStats service worker.
 *
 * Served from / (see ttstats/urls.py) so its scope covers the whole site.
 *
 * It exists to receive push notifications and to make the app installable.
 * Nothing is cached, and that is a choice -- a stale cached page showing
 * yesterday's leaderboard is worse than an honest browser error, and cache
 * invalidation for an app whose whole content is live data is a real project,
 * not a side effect of adding push.
 *
 * THE FETCH HANDLER IS A ROUTE TABLE, AND THE TABLE IS EMPTY (KAN-96).
 *
 * `ROUTES` below lists the requests this worker answers itself. A request no
 * route claims -- today, every request -- is not intercepted: the handler
 * returns without calling respondWith() and the browser fetches it exactly as
 * if there were no worker. Only same-origin GETs are ever offered to the
 * table, so a POST (a scored point) can never be answered from here. The
 * offline scoreboard (KAN-82) and offline reading (KAN-131) add entries to
 * the table; neither should need to touch the handler.
 *
 * This file used to have no fetch listener at all, for two reasons:
 *
 *   1. Playwright's page.route() stops seeing requests from a page a worker
 *      with a fetch listener controls. The scoreboard specs that abort
 *      /live/point/ to check the error toast once silently passed that
 *      request to the server, saw no error, and failed looking for a toast
 *      never provoked -- in CI only, as a race against how fast the worker
 *      claims the page. applyProdCSP() is page.route() too, so CSP specs
 *      would quietly check pages served without a policy.
 *      Now: playwright.config.js blocks service workers in EVERY project by
 *      default. Only `iphone-pwa` allows them, and its specs either opt back
 *      out (`test.use({ serviceWorkers: 'block' })`) or assert that the route
 *      really applied. Audit: docs/decisions/kan-76-sw-e2e-audit.md. A new
 *      project is blocked unless it says otherwise.
 *   2. Every request in production gains a worker hop. With an empty table
 *      that hop is one URL parse; it is the price of a cache offline scoring
 *      can extend.
 *
 * Chrome dropped the fetch-handler requirement for installability in 89, and
 * iOS never needed a service worker for Add to Home Screen at all. Push works
 * without one.
 */

const VERSION = 'ttstats-v2';
const FALLBACK_ICON = '{% static "pingpong/icons/app/icon-192.png" %}';

self.addEventListener('install', (event) => {
  // Take over immediately rather than waiting for every tab to close. There
  // is no cached content to invalidate, so there is nothing for the old
  // worker to still be serving correctly. The first route that caches must
  // revisit this, and delete caches not named for VERSION in `activate`.
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

/* The route table. First match wins; no match means "not intercepted".
 *
 * Each entry is { name, match(url, request) -> boolean,
 * handle(event, url) -> Promise<Response> }. `match` must be cheap and
 * synchronous: it runs for every GET the app makes. `handle` owns its whole
 * strategy (network first, cache first, timeouts) and must always resolve to
 * a Response -- including when the network and the cache both fail.
 */
const ROUTES = [];

function routeFor(request) {
  if (request.method !== 'GET') return null;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return null;
  const route = ROUTES.find((candidate) => candidate.match(url, request));
  return route ? { route, url } : null;
}

self.addEventListener('fetch', (event) => {
  const found = routeFor(event.request);
  // Pass-through: without respondWith() the browser performs the request
  // itself, as if this worker did not exist.
  if (!found) return;
  event.respondWith(found.route.handle(event, found.url));
});

self.addEventListener('push', (event) => {
  // A push with no data, or with a body that is not our JSON, still has to
  // produce a visible notification: browsers penalise (and on some platforms
  // forcibly unsubscribe) a worker that receives a push and shows nothing.
  let payload = {};
  if (event.data) {
    try {
      payload = event.data.json();
    } catch (err) {
      payload = { body: event.data.text() };
    }
  }

  const title = payload.title || 'TTStats';
  const options = {
    body: payload.body || '',
    icon: FALLBACK_ICON,
    badge: FALLBACK_ICON,
    tag: payload.tag || 'ttstats',
    // With a tag set, the default is to replace silently. These are match
    // events people are waiting on, so a replacement should still buzz.
    renotify: Boolean(payload.tag),
    data: { url: payload.url || '/pingpong/', kind: payload.kind || '' },
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/pingpong/';

  event.waitUntil(
    self.clients
      .matchAll({ type: 'window', includeUncontrolled: true })
      .then((clientList) => {
        // Prefer focusing a tab that is already open and navigating it, over
        // opening a second copy of the app. Someone who taps a notification
        // while the app is open should not end up with two windows.
        for (const client of clientList) {
          if ('focus' in client) {
            if ('navigate' in client) {
              return client.focus().then((focused) => focused.navigate(target));
            }
            return client.focus();
          }
        }
        return self.clients.openWindow(target);
      })
  );
});
