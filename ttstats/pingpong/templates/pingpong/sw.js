{% load static %}/* TTStats service worker.
 *
 * Served from / (see ttstats/urls.py) so its scope covers the whole site.
 *
 * Deliberately minimal: it exists to receive push notifications and to make
 * the app installable. There is no offline caching strategy here, and that is
 * a choice -- a stale cached page showing yesterday's leaderboard is worse
 * than an honest browser error, and cache invalidation for an app whose whole
 * content is live data is a real project, not a side effect of adding push.
 *
 * THERE IS DELIBERATELY NO `fetch` HANDLER. Not even an empty one.
 *
 * Registering any fetch listener makes this worker the network path for every
 * request the app makes, which has two consequences and no benefit while
 * there is nothing to cache:
 *
 *   1. Playwright's page.route() stops intercepting anything, because those
 *      requests now originate from the worker rather than the page. The
 *      scoreboard specs that abort /live/point/ to check the error toast
 *      then silently pass their request straight through to the server, see
 *      no error, and fail looking for a toast that was never provoked. This
 *      shipped once and failed in CI while passing locally -- it is a race
 *      against how fast the worker claims the page.
 *   2. Every request in production gains a worker hop for nothing.
 *
 * Chrome dropped the fetch-handler requirement for installability in 89, and
 * iOS never needed a service worker for Add to Home Screen at all. Push works
 * without one. If an offline mode is ever added, adding the handler back is
 * the first step -- and the scoreboard specs are the canary that will tell
 * you the moment it starts intercepting.
 */

const VERSION = 'ttstats-v1';
const FALLBACK_ICON = '{% static "pingpong/icons/app/icon-192.png" %}';

self.addEventListener('install', (event) => {
  // Take over immediately rather than waiting for every tab to close. There
  // is no cached content to invalidate, so there is nothing for the old
  // worker to still be serving correctly.
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
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
