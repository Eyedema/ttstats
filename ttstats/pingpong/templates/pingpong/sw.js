{% load static %}/* TTStats service worker.
 *
 * Served from / (see ttstats/urls.py) so its scope covers the whole site.
 * It receives push notifications, makes the app installable, and keeps ONE
 * page working with no signal: the live scoreboard (KAN-82).
 *
 * WHAT IS CACHED, AND WHY SO LITTLE
 *
 * A stale cached page showing yesterday's leaderboard is worse than an honest
 * browser error, so almost nothing is cached. The scoreboard is the exception
 * because its page is a shell: on load it rebuilds the score from the
 * bootstrap it was rendered with plus the ops still in its local outbox, and
 * the server's `applied_seq` reconciles the rest on the next sync. A stale copy
 * of THAT page is correct; a stale copy of any other page is a lie.
 *
 * `ROUTES` lists the requests this worker answers itself. Only same-origin
 * GETs are ever offered to it; anything no route claims is not intercepted
 * (no respondWith(): the browser fetches it as if there were no worker).
 *
 *   staticAssets      GET /static/<name>.<12 hex>.<ext> -- hashed by the
 *                     manifest storage, so a URL's bytes never change. Cache
 *                     first; stored on first use; the scoreboard's boot set
 *                     (PRECACHE, from pingpong/offline.py) is fetched at
 *                     install. Unhashed /static/ (dev) is NOT intercepted, or
 *                     a developer would be served yesterday's CSS.
 *   scoreboardShell   navigations to /pingpong/matches/<pk>/live/ only.
 *                     Network first, NAV_TIMEOUT_MS to give up on lie-fi.
 *                     A 200 straight from that URL carrying X-TTStats-Identity
 *                     is stored; a redirect (match over, or logged out -> the
 *                     login page) or a 403/404/410 evicts the copy and is
 *                     returned as is, never masked. On no answer, a timeout or
 *                     a 5xx, the stored copy for that match is served; with no
 *                     copy, the network's answer (or error) stands.
 *
 * NOT intercepted, by design: every POST (a scored point), /live/state/,
 * /live/sync/ and all other JSON, every other page, /sw.js itself.
 * KAN-131's snapshot route goes in ROUTES *after* scoreboardShell, so the
 * scoreboard keeps its own rules for its own URL.
 *
 * CACHES (all names start `ttstats-`; activate deletes any not current)
 *
 *   ttstats-static-<BUILD>            hashed static, capped at STATIC_MAX.
 *   ttstats-live-<BUILD>-<identity>   scoreboard pages, capped at LIVE_MAX,
 *                                     older than LIVE_MAX_AGE_MS ignored.
 *
 * BUILD = VERSION + a digest of the precache URLs. Those carry content hashes,
 * so a deploy that changes any of them changes these bytes, the browser
 * installs this worker anew, and activate drops the previous build's caches:
 * a stored scoreboard never outlives the static files it was rendered with.
 *
 * WHOSE PAGE (two people can share a phone)
 *
 * A stored scoreboard carries its session's CSRF token and player ids. It is
 * filed under the X-TTStats-Identity the server sent with it (HMAC of user +
 * active club, pingpong/offline.py), and at most one identity's copies exist:
 * storing for identity X, or a page announcing X, deletes every other
 * partition. Logged-out pages announce "" and so delete them all.
 *
 * MESSAGES this worker accepts (postMessage from a same-origin page):
 *
 *   {type: 'identity', identity}  offline.js, every page load (meta
 *                                 ttstats-identity). Keep only that
 *                                 partition; "" keeps none.
 *   {type: 'purge'}               offline.js, on submit of a form marked
 *                                 data-sw-purge (Log out). Drop every copy.
 *   {type: 'live-match-complete', url}
 *                                 The scoreboard, once the match is over
 *                                 (its final op synced). Drop that match's
 *                                 copy so a dead scoreboard can't be reopened
 *                                 offline. `url` is the scoreboard URL,
 *                                 absolute or path-only. Belt and braces: the
 *                                 next online open of a finished match is a
 *                                 redirect, which evicts it anyway.
 *
 * E2E: a worker that answers requests hides them from Playwright's
 * page.route() -- and so from applyProdCSP(). Every project blocks workers
 * except `iphone-pwa`, where offline specs drive the network with
 * context.setOffline(). Audit: docs/decisions/kan-76-sw-e2e-audit.md.
 */

const VERSION = 'ttstats-v3';
const BUILD = VERSION + '-{{ sw_build }}';
const PRECACHE = {{ sw_precache_json|safe }};
const FALLBACK_ICON = '{% static "pingpong/icons/app/icon-192.png" %}';

const STATIC_CACHE = 'ttstats-static-' + BUILD;
const LIVE_PREFIX = 'ttstats-live-' + BUILD + '-';
const STATIC_MAX = 120;
const LIVE_MAX = 8;
const LIVE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const NAV_TIMEOUT_MS = 3000;
const STORED_AT = 'X-TTStats-Stored-At';
const IDENTITY_HEADER = 'X-TTStats-Identity';

// Manifest storage names: app.<12 hex>.css. Nothing else is immutable.
const HASHED_STATIC = /^\/static\/.+\.[0-9a-f]{12}\.[A-Za-z0-9]+$/;
const SCOREBOARD = /^\/pingpong\/matches\/\d+\/live\/$/;
const IDENTITY = /^[0-9a-f]{1,64}$/;

/* Is this cache one the current build still uses? KAN-131 adds its prefix. */
function isCurrentCache(name) {
  return name === STATIC_CACHE || name.startsWith(LIVE_PREFIX);
}

self.addEventListener('install', (event) => {
  // Precache file by file: one missing asset must not fail the install and
  // leave the previous build (and its push handling) in charge. Whatever is
  // missed is cached the first time a controlled page loads it.
  event.waitUntil(
    caches
      .open(STATIC_CACHE)
      .then((cache) =>
        Promise.all(
          PRECACHE.filter((path) => HASHED_STATIC.test(path)).map((path) =>
            cache.match(path).then((hit) => hit || cache.add(path)).catch(() => {})
          )
        )
      )
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names
            .filter((name) => name.startsWith('ttstats-') && !isCurrentCache(name))
            .map((name) => caches.delete(name))
        )
      )
      .then(() => self.clients.claim())
  );
});

/* ---- cache helpers ------------------------------------------------------ */

/* Keep a cache to `max` entries, oldest first (keys() is insertion order and
 * every store deletes before it puts, so the order is least recently stored). */
async function trim(cache, max) {
  const keys = await cache.keys();
  await Promise.all(keys.slice(0, Math.max(0, keys.length - max)).map((key) => cache.delete(key)));
}

async function liveCacheNames() {
  return (await caches.keys()).filter((name) => name.startsWith(LIVE_PREFIX));
}

/* Delete every scoreboard partition except `keep` ("" or null keeps none). */
async function keepOnlyPartition(keep) {
  const names = await liveCacheNames();
  await Promise.all(
    names.filter((name) => !keep || name !== LIVE_PREFIX + keep).map((name) => caches.delete(name))
  );
}

function scoreboardKey(url) {
  return url.origin + url.pathname;
}

async function evictScoreboard(key) {
  const names = await liveCacheNames();
  await Promise.all(names.map((name) => caches.open(name).then((cache) => cache.delete(key))));
}

async function storeScoreboard(key, response, identity) {
  // Re-wrapped to stamp when it was stored; status and every header,
  // including the page's own CSP, travel with it.
  const headers = new Headers(response.headers);
  headers.set(STORED_AT, String(Date.now()));
  const copy = new Response(await response.blob(), {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
  await keepOnlyPartition(identity);
  const cache = await caches.open(LIVE_PREFIX + identity);
  await cache.delete(key);
  await cache.put(key, copy);
  await trim(cache, LIVE_MAX);
}

async function storedScoreboard(key) {
  for (const name of await liveCacheNames()) {
    const cache = await caches.open(name);
    const hit = await cache.match(key, { ignoreVary: true });
    if (!hit) continue;
    const storedAt = Number(hit.headers.get(STORED_AT)) || 0;
    if (Date.now() - storedAt <= LIVE_MAX_AGE_MS) return hit;
    await cache.delete(key);
  }
  return null;
}

/* What a network answer to a scoreboard navigation means for the cache. */
async function rememberScoreboard(key, url, response) {
  if (
    response.type === 'opaqueredirect' ||
    response.redirected ||
    (response.status >= 300 && response.status < 400) ||
    [403, 404, 410].includes(response.status)
  ) {
    // The match is over, the session is gone, or this isn't our match.
    await evictScoreboard(key);
    return;
  }
  const identity = response.headers.get(IDENTITY_HEADER) || '';
  if (
    response.status === 200 &&
    response.type === 'basic' &&
    new URL(response.url).pathname === url.pathname &&
    IDENTITY.test(identity)
  ) {
    await storeScoreboard(key, response, identity);
  }
}

/* ---- routes ------------------------------------------------------------- */

const staticAssets = {
  name: 'staticAssets',
  match: (url) => HASHED_STATIC.test(url.pathname),
  handle(event, url) {
    return caches.open(STATIC_CACHE).then((cache) =>
      cache.match(url.pathname, { ignoreVary: true }).then((hit) => {
        if (hit) return hit;
        return fetch(event.request).then((response) => {
          if (response.ok && response.type === 'basic') {
            const copy = response.clone();
            event.waitUntil(
              cache.put(url.pathname, copy).then(() => trim(cache, STATIC_MAX)).catch(() => {})
            );
          }
          return response;
        });
      })
    );
  },
};

const scoreboardShell = {
  name: 'scoreboardShell',
  match: (url, request) => request.mode === 'navigate' && SCOREBOARD.test(url.pathname),
  handle(event, url) {
    const key = scoreboardKey(url);
    // One fetch. It keeps running after a timeout so a late answer still
    // refreshes (or evicts) the stored copy.
    const network = fetch(event.request).then((response) => {
      const copy = response.clone();
      event.waitUntil(rememberScoreboard(key, url, copy).catch(() => {}));
      return response;
    });
    event.waitUntil(network.then(() => {}, () => {}));

    const timeout = new Promise((resolve) => setTimeout(resolve, NAV_TIMEOUT_MS, null));
    const fallback = (answer) =>
      storedScoreboard(key).then((stored) => stored || answer || network);

    return Promise.race([network, timeout]).then(
      (response) => (response && response.status < 500 ? response : fallback(response)),
      () => fallback(null)
    );
  },
};

/* The route table. First match wins; no match means "not intercepted".
 *
 * Each entry is { name, match(url, request) -> boolean,
 * handle(event, url) -> Promise<Response> }. `match` must be cheap and
 * synchronous: it runs for every GET the app makes. `handle` owns its whole
 * strategy and may reject only where the browser would have failed anyway.
 */
const ROUTES = [staticAssets, scoreboardShell];

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

self.addEventListener('message', (event) => {
  const data = event.data || {};
  if (data.type === 'identity') {
    const identity = String(data.identity || '');
    event.waitUntil(keepOnlyPartition(IDENTITY.test(identity) ? identity : null));
  } else if (data.type === 'purge') {
    event.waitUntil(keepOnlyPartition(null));
  } else if (data.type === 'live-match-complete' && typeof data.url === 'string') {
    let url;
    try {
      url = new URL(data.url, self.location.origin);
    } catch (err) {
      return;
    }
    if (url.origin === self.location.origin && SCOREBOARD.test(url.pathname)) {
      event.waitUntil(evictScoreboard(scoreboardKey(url)));
    }
  }
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
