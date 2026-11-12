{% load static %}/* TTStats service worker.
 *
 * Served from / (see ttstats/urls.py) so its scope covers the whole site.
 * It receives push notifications, makes the app installable, keeps the live
 * scoreboard working with no signal (KAN-82), and keeps a LABELLED saved copy
 * of the read-only pages the server opts in (KAN-131 snapshots).
 *
 * WHAT IS CACHED, AND WHY SO LITTLE
 *
 * A stale page passed off as live is worse than an honest browser error. Two
 * kinds of page are kept anyway, each for its own reason:
 *
 *   - The scoreboard is a shell: on load it rebuilds the score from the
 *     bootstrap it was rendered with plus the ops still in its local outbox,
 *     and the server's `applied_seq` reconciles the rest on the next sync. A
 *     stale copy of THAT page is correct.
 *   - A snapshot (dashboard, table, a match, a player...) is stale, so it is
 *     only ever shown LABELLED: the worker rewrites its root element's
 *     data-freshness="network" to "snapshot" and fills the banner's <time>,
 *     and CSS alone then shows the "saved copy" banner. A copy without both
 *     markers is never served. The server decides which pages may be stored
 *     (X-TTStats-Snapshot: 1, pingpong/offline.py); the worker stores nothing
 *     else. Healthy network = always the network's page.
 *
 * `ROUTES` lists the requests this worker answers itself. Only same-origin
 * GETs are ever offered to it; anything no route claims is not intercepted
 * (no respondWith(): the browser fetches it as if there were no worker).
 * First match wins.
 *
 *   staticAssets      GET /static/<name>.<12 hex>.<ext> -- hashed by the
 *                     manifest storage, so a URL's bytes never change. Cache
 *                     first; stored on first use; the scoreboard's boot set
 *                     (PRECACHE, from pingpong/offline.py) is fetched at
 *                     install. Unhashed /static/ (dev) is NOT intercepted, or
 *                     a developer would be served yesterday's CSS.
 *   scoreboardShell   navigations to /pingpong/matches/<pk>/live/ only.
 *                     Network first, LIVE_TIMEOUT_MS to give up on lie-fi.
 *                     A 200 straight from that URL carrying X-TTStats-Identity
 *                     is stored; a redirect (match over, or logged out -> the
 *                     login page) or a 403/404/410 evicts the copy and is
 *                     returned as is, never masked. On no answer, a timeout or
 *                     a 5xx, the stored copy for that match is served; with no
 *                     copy, the network's answer (or error) stands.
 *   snapshotNavigation
 *                     navigations (mode 'navigate') under /pingpong/, except
 *                     the scoreboard (claimed above), anything with /live/ in
 *                     it, and htmx (HX-Request). /static/, /admin/ (whatever
 *                     ADMIN_URL is), /accounts/ and /sw.js are outside
 *                     /pingpong/ and never claimed. Network first; the
 *                     network's answer always wins when it comes in time:
 *                       2xx + X-TTStats-Snapshot + identity == partition
 *                                       -> returned untouched, a copy stored
 *                       2xx otherwise   -> returned; no longer opted in (or
 *                                          the flag is off): copy evicted,
 *                                          unless it is another identity's
 *                       3xx, 401/403/404/410
 *                                       -> returned as is (a login redirect
 *                                          is never masked), copy evicted
 *                       other 4xx       -> returned as is
 *                       5xx             -> the saved copy if any; else the
 *                                          offline page for 502/503/504 (the
 *                                          proxy answered, the app did not),
 *                                          the 5xx itself for anything else
 *                       error, or no answer in NAV_TIMEOUT_MS
 *                                       -> the saved copy if any; else, on an
 *                                          error at once, on no answer at
 *                                          NAV_GIVE_UP_MS (a slow network
 *                                          keeps its chance), the OFFLINE PAGE
 *                     The offline page (KAN-137, /offline/, outside /pingpong/
 *                     so no route claims it) holds no one's data: precached at
 *                     install with no cookies, it lists the saved pages by
 *                     asking {type: 'list-snapshots'}. Shown only while there
 *                     is a snapshot partition (flag on, logged in): the kill
 *                     switch turns it off too. Not precached = the browser's
 *                     error, as before. Never stored as a snapshot.
 *                     navigator.onLine === false skips the wait (a hint only:
 *                     iOS says true on lie-fi, hence the timeout). The fetch
 *                     is never aborted -- a navigate-mode Request cannot be
 *                     re-made with a signal -- so an answer arriving after a
 *                     copy was served is still stored, and the page is told
 *                     {type: 'snapshot-fresh', url}.
 *
 * NOT intercepted, by design: every POST (a scored point), every htmx or
 * fetch() request (prefetch included: only navigations are stored),
 * /live/state/, /live/sync/ and all other JSON, /sw.js itself.
 *
 * CACHES (all names start `ttstats-`; activate deletes any not current)
 *
 *   ttstats-static-<BUILD>            hashed static, capped at STATIC_MAX.
 *   ttstats-live-<BUILD>-<identity>   scoreboard pages, capped at LIVE_MAX,
 *                                     older than LIVE_MAX_AGE_MS ignored.
 *   ttstats-snap-<BUILD>-<identity>   snapshots, keyed by URL with query
 *                                     (no #hash). LRU, SNAP_MAX entries
 *                                     (serving one refreshes it), older than
 *                                     SNAP_MAX_AGE_MS deleted on activate,
 *                                     on every store and when looked up.
 *                                     Each carries X-Snapshot-At (ISO) and
 *                                     X-Snapshot-Title (URI-encoded <title>,
 *                                     for the S12.6 offline page).
 *   ttstats-snapstate-<BUILD>         one entry: the current snapshot
 *                                     partition, so it survives the worker
 *                                     being stopped between events.
 *   ttstats-offline-<BUILD>           one entry: the offline page (/offline/).
 *                                     Its URL has no content hash, so its
 *                                     bytes are part of BUILD (offline.py
 *                                     build_key): an edited page is a new
 *                                     build, and the new worker re-fetches it.
 *
 * BUILD = VERSION + a digest of the precache URLs. Those carry content hashes,
 * so a deploy that changes any of them changes these bytes, the browser
 * installs this worker anew, and activate drops the previous build's caches:
 * a stored page never outlives the static files it was rendered with (a
 * snapshot referencing CSS the static cache no longer holds would open
 * unstyled, banner rules aside). A deploy therefore empties the snapshots;
 * the next online visit refills them.
 *
 * WHOSE PAGE (two people can share a phone)
 *
 * A stored page carries its session's CSRF token and that person's data. It
 * is filed under the X-TTStats-Identity the server sent with it (HMAC of user
 * + active club, pingpong/offline.py).
 *   Scoreboards: at most one identity's copies exist; storing for identity X,
 *   or a page announcing X, deletes every other partition.
 *   Snapshots: stricter. The CURRENT partition is whatever the last page
 *   announced with snapshots 'on'; a response is stored only if its identity
 *   equals it, and only it is ever read. Announcing anything else (another
 *   identity, "" when logged out, snapshots 'off' = the kill switch) deletes
 *   every other snapshot partition first. No announcement yet = no partition
 *   = nothing stored or served.
 *
 * MESSAGES this worker accepts (postMessage from a same-origin page):
 *
 *   {type: 'identity', identity, snapshots}
 *                                 offline.js, every page load (meta
 *                                 ttstats-identity, ttstats-snapshots).
 *                                 Keep only that identity's scoreboards
 *                                 ("" keeps none); snapshots 'on' makes it
 *                                 the snapshot partition, anything else
 *                                 deletes every snapshot. Fetches the
 *                                 offline page if it isn't cached yet.
 *   {type: 'list-snapshots'}      offline_page.js. Answered on the port it
 *                                 sends (else to the page) with {type:
 *                                 'snapshots', snapshots, scoreboards}, each
 *                                 [{url, title, savedAt, shown}] newest
 *                                 first: the CURRENT partition's servable
 *                                 copies, and the stored scoreboards.
 *   {type: 'purge'}               offline.js, on submit of a form marked
 *                                 data-sw-purge (Log out). Drop every copy
 *                                 of every kind; no snapshot partition.
 *   {type: 'live-match-complete', url}
 *                                 The scoreboard, once the match is over
 *                                 (its final op synced). Drop that match's
 *                                 copy so a dead scoreboard can't be reopened
 *                                 offline. `url` is the scoreboard URL,
 *                                 absolute or path-only. Belt and braces: the
 *                                 next online open of a finished match is a
 *                                 redirect, which evicts it anyway.
 *
 * and sends one, to the page a saved copy was served to:
 *
 *   {type: 'snapshot-fresh', url} the network answered after all and a fresh
 *                                 copy is stored (offline.js re-dispatches it
 *                                 as `ttstats:snapshot-fresh`; the
 *                                 "Refresh" prompt is KAN-146's).
 *
 * E2E: a worker that answers requests hides them from Playwright's
 * page.route() -- and so from applyProdCSP(). Every project blocks workers
 * except `iphone-pwa`. Offline there is `tests/e2e/offline-proxy.js`, never
 * context.setOffline(): WebKit fails the loads before the worker runs.
 * Audit: docs/decisions/kan-76-sw-e2e-audit.md. Pure parts of this file are
 * unit-tested in a vm by tests/js/sw_*.test.mjs.
 */

const VERSION = 'ttstats-v5';
const BUILD = VERSION + '-{{ sw_build }}';
const PRECACHE = {{ sw_precache_json|safe }};
const FALLBACK_ICON = '{% static "pingpong/icons/app/icon-192.png" %}';
const OFFLINE_URL = '{% url "offline_page" %}';

const STATIC_CACHE = 'ttstats-static-' + BUILD;
const LIVE_PREFIX = 'ttstats-live-' + BUILD + '-';
const SNAP_PREFIX = 'ttstats-snap-' + BUILD + '-';
const SNAP_STATE_CACHE = 'ttstats-snapstate-' + BUILD;
const SNAP_STATE_KEY = '/__ttstats/snapshot-partition';
const OFFLINE_CACHE = 'ttstats-offline-' + BUILD;
const STATIC_MAX = 120;
const LIVE_MAX = 8;
const LIVE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const SNAP_MAX = 60;
const SNAP_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
// The scoreboard's copy is correct, so give up on the network sooner; a
// snapshot is stale, so the network gets longer to answer with the truth.
const LIVE_TIMEOUT_MS = 3000;
const NAV_TIMEOUT_MS = 4000;
// With no saved copy there is nothing better to show at NAV_TIMEOUT_MS, so a
// slow network keeps its chance; past this the offline page is shown instead
// of a spinner that never ends.
const NAV_GIVE_UP_MS = 12000;
// The proxy answered but the app did not: as good as no network.
const GATEWAY_ERRORS = [502, 503, 504];
const DAY_MS = 24 * 60 * 60 * 1000;
const STORED_AT = 'X-TTStats-Stored-At';
const IDENTITY_HEADER = 'X-TTStats-Identity';
const SNAPSHOT_HEADER = 'X-TTStats-Snapshot';
const SNAPSHOT_AT = 'X-Snapshot-At';
const SNAPSHOT_TITLE = 'X-Snapshot-Title';
const SERVED_FROM = 'X-TTStats-Served-From';
// Rendered once each by base.html; pinned by test_offline_snapshots.py.
const FRESHNESS_MARKER = 'data-freshness="network"';
const TIME_PLACEHOLDER = '<time data-snapshot-time></time>';
// On the offline page's <html>; pinned by test_offline_page.py.
const OFFLINE_REASON_MARKER = 'data-offline-reason="network"';

// Manifest storage names: app.<12 hex>.css. Nothing else is immutable.
const HASHED_STATIC = /^\/static\/.+\.[0-9a-f]{12}\.[A-Za-z0-9]+$/;
const SCOREBOARD = /^\/pingpong\/matches\/\d+\/live\/$/;
const IDENTITY = /^[0-9a-f]{1,64}$/;
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/* Is this cache one the current build still uses? */
function isCurrentCache(name) {
  return (
    name === STATIC_CACHE ||
    name === SNAP_STATE_CACHE ||
    name === OFFLINE_CACHE ||
    name.startsWith(LIVE_PREFIX) ||
    name.startsWith(SNAP_PREFIX)
  );
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
      .then(() => precacheOfflinePage().catch(() => {}))
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
      .then(() => pruneAllSnapshots().catch(() => {}))
      .then(() => self.clients.claim())
  );
});

/* ---- pure helpers (no caches, no network; unit-tested in a vm) ---------- */

/* Does the snapshot route claim this GET? Navigations only: an htmx swap,
 * a prefetch or any other fetch() is never answered from a snapshot. */
function isSnapshotNavigation(url, request) {
  if (request.mode !== 'navigate') return false;
  if (!url.pathname.startsWith('/pingpong/')) return false;
  if (SCOREBOARD.test(url.pathname) || url.pathname.includes('/live/')) return false;
  const headers = request.headers;
  if (headers && headers.get && headers.get('HX-Request')) return false;
  return true;
}

/* The URL including its query (?season= is another page), never the #hash. */
function snapshotKey(url) {
  return url.origin + url.pathname + url.search;
}

/* What a network answer means, given the current snapshot partition.
 *
 *   'error'        no answer at all (rejected fetch)
 *   'redirect'     3xx or an opaque redirect: never mask a login redirect
 *   'gone'         401/403/404/410: this copy must not be shown again
 *   'client-error' any other 4xx
 *   'server-error' 5xx (the proxy's 502/503/504 included)
 *   'snapshot'     2xx opted in, for exactly the current partition
 *   'foreign'      2xx opted in, but for another identity (club switched in
 *                  another tab, a stale partition): never stored
 *   'plain'        any other 2xx (not opted in, or the flag is off)
 */
function classifyAnswer(response, partition) {
  if (!response) return 'error';
  const status = response.status;
  if (response.type === 'opaqueredirect' || response.redirected || (status >= 300 && status < 400)) {
    return 'redirect';
  }
  if (status >= 500) return 'server-error';
  if ([401, 403, 404, 410].includes(status)) return 'gone';
  if (status >= 400) return 'client-error';
  if (status < 200 || status >= 300) return 'plain';
  if (response.type && response.type !== 'basic' && response.type !== 'default') return 'plain';
  if (response.headers.get(SNAPSHOT_HEADER) !== '1') return 'plain';
  const identity = response.headers.get(IDENTITY_HEADER) || '';
  if (partition && IDENTITY.test(identity) && identity === partition) return 'snapshot';
  return 'foreign';
}

/* The decision table: answer kind x "is there a servable copy" -> action.
 * respond: 'network' (what the network said, or its pending answer/error)
 *          or 'copy' (the saved copy, labelled). */
function planNavigation(kind, hasCopy) {
  switch (kind) {
    case 'snapshot':
      return { respond: 'network', store: true, evict: false };
    case 'plain':
    case 'redirect':
    case 'gone':
      return { respond: 'network', store: false, evict: true };
    case 'foreign':
    case 'client-error':
      return { respond: 'network', store: false, evict: false };
    case 'server-error':
    case 'error':
    case 'timeout':
    case 'offline':
      return { respond: hasCopy ? 'copy' : 'network', store: false, evict: false };
    default:
      return { respond: 'network', store: false, evict: false };
  }
}

/* Is a copy stored at `savedAt` (ms) still allowed to be served at `now`? */
function isFresh(savedAt, now) {
  return Number.isFinite(savedAt) && savedAt <= now + 60000 && now - savedAt <= SNAP_MAX_AGE_MS;
}

/* Which keys to delete. `entries` = [{key, savedAt}] least recently used
 * first (Cache keys() order: every store and serve deletes, then puts). */
function snapshotEvictions(entries, now, max) {
  const doomed = [];
  const kept = [];
  for (const entry of entries) {
    if (isFresh(entry.savedAt, now)) kept.push(entry);
    else doomed.push(entry.key);
  }
  const over = Math.max(0, kept.length - max);
  return doomed.concat(kept.slice(0, over).map((entry) => entry.key));
}

/* The current snapshot partition an identity message selects, or null. */
function partitionFromMessage(data) {
  const identity = String((data && data.identity) || '');
  return data && data.snapshots === 'on' && IDENTITY.test(identity) ? identity : null;
}

function pad2(n) {
  return (n < 10 ? '0' : '') + n;
}

/* The banner's absolute time, in the phone's own time zone. Same day:
 * "14:32"; an earlier day within 24 h: "Mon 23:05"; older: "Tue 4 Nov",
 * with `old` set so the banner switches to its "may be out of date" words. */
function formatSavedAt(savedAt, now) {
  const saved = new Date(savedAt);
  const clock = pad2(saved.getHours()) + ':' + pad2(saved.getMinutes());
  if (now - savedAt > DAY_MS) {
    return {
      text: DAYS[saved.getDay()] + ' ' + saved.getDate() + ' ' + MONTHS[saved.getMonth()],
      old: true,
    };
  }
  if (saved.toDateString() === new Date(now).toDateString()) return { text: clock, old: false };
  return { text: DAYS[saved.getDay()] + ' ' + clock, old: false };
}

/* The freshness rewrite (KAN-134): a plain string replace, no DOM parsing.
 * Returns null when either marker is missing: no banner, no copy. */
function markSnapshot(html, savedAt, now) {
  if (typeof html !== 'string') return null;
  if (html.indexOf(FRESHNESS_MARKER) === -1 || html.indexOf(TIME_PLACEHOLDER) === -1) return null;
  const iso = new Date(savedAt).toISOString();
  const shown = formatSavedAt(savedAt, now);
  const root =
    'data-freshness="snapshot" data-snapshot-at="' + iso + '" data-snapshot-age="' +
    (shown.old ? 'old' : 'recent') + '"';
  const time = '<time data-snapshot-time datetime="' + iso + '">' + shown.text + '</time>';
  // Function replacers: a '$' in the page must never be read as a pattern.
  return html.replace(FRESHNESS_MARKER, () => root).replace(TIME_PLACEHOLDER, () => time);
}

/* The page's <title>, entities for the five specials decoded (S12.6 lists it). */
function extractTitle(html) {
  const found = /<title[^>]*>([^<]*)<\/title>/i.exec(html || '');
  if (!found) return '';
  return found[1]
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#x?0*27;/g, "'")
    .replace(/&amp;/g, '&');
}

/* Does a navigation with no saved copy get the offline page, and worded
 * how? 'network' (no answer, timeout, or the phone says offline), 'server'
 * (the proxy's 502/503/504) or null: the network's own answer stands (a
 * Django 500 included: the app answered, and its error page has a way out). */
function offlineReason(kind, status) {
  if (kind === 'error' || kind === 'offline' || kind === 'timeout') return 'network';
  if (kind === 'server-error' && GATEWAY_ERRORS.includes(status)) return 'server';
  return null;
}

/* The offline page, worded for `reason` (a plain string replace). */
function markOfflinePage(html, reason) {
  if (typeof html !== 'string') return null;
  if (reason === 'network') return html;
  return html.replace(OFFLINE_REASON_MARKER, () => 'data-offline-reason="server"');
}

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

/* ---- snapshot partition and store --------------------------------------- */

// A promise of the current partition (identity string or null). Memory is
// lost whenever the browser stops this worker, so it is persisted in
// SNAP_STATE_CACHE and reloaded on first use.
let snapshotPartition = null;

function currentPartition() {
  if (!snapshotPartition) snapshotPartition = loadPartition();
  return snapshotPartition;
}

async function loadPartition() {
  try {
    if (!(await caches.has(SNAP_STATE_CACHE))) return null;
    const hit = await (await caches.open(SNAP_STATE_CACHE)).match(SNAP_STATE_KEY);
    if (!hit) return null;
    const identity = String((await hit.json()).identity || '');
    return IDENTITY.test(identity) ? identity : null;
  } catch (err) {
    return null;
  }
}

async function snapCacheNames() {
  return (await caches.keys()).filter((name) => name.startsWith(SNAP_PREFIX));
}

/* Make `identity` (or null: none) the snapshot partition and delete every
 * other one. The in-memory value changes before anything is awaited, so a
 * store racing this message already sees the new partition. */
async function setPartition(identity) {
  snapshotPartition = Promise.resolve(identity);
  const names = await snapCacheNames();
  await Promise.all(
    names.filter((name) => !identity || name !== SNAP_PREFIX + identity).map((name) => caches.delete(name))
  );
  const state = await caches.open(SNAP_STATE_CACHE);
  await state.put(
    SNAP_STATE_KEY,
    new Response(JSON.stringify({ identity }), { headers: { 'Content-Type': 'application/json' } })
  );
}

async function openSnapshots(partition) {
  if (!partition) return null;
  const name = SNAP_PREFIX + partition;
  // caches.open() would create it; a lookup must never leave an empty one.
  return (await caches.has(name)) ? caches.open(name) : null;
}

async function pruneSnapshots(cache) {
  const keys = await cache.keys();
  const entries = await Promise.all(
    keys.map(async (key) => {
      const hit = await cache.match(key, { ignoreVary: true });
      return { key, savedAt: hit ? Date.parse(hit.headers.get(SNAPSHOT_AT)) : NaN };
    })
  );
  await Promise.all(snapshotEvictions(entries, Date.now(), SNAP_MAX).map((key) => cache.delete(key)));
}

async function pruneAllSnapshots() {
  for (const name of await snapCacheNames()) await pruneSnapshots(await caches.open(name));
}

async function evictSnapshot(key) {
  for (const name of await snapCacheNames()) await (await caches.open(name)).delete(key);
}

/* Store an opted-in answer for `partition`. False if it was not stored. */
async function storeSnapshot(key, response, partition) {
  const html = await response.text();
  const now = Date.now();
  // A page the rewrite could not label would never be served: don't keep it.
  if (markSnapshot(html, now, now) === null) return false;
  // The partition may have changed while the body was read.
  if ((await currentPartition()) !== partition) return false;
  const headers = new Headers(response.headers);
  // The body is stored decoded; Set-Cookie is not readable here anyway.
  ['Content-Length', 'Content-Encoding', 'Set-Cookie'].forEach((name) => headers.delete(name));
  headers.set(SNAPSHOT_AT, new Date(now).toISOString());
  headers.set(SNAPSHOT_TITLE, encodeURIComponent(extractTitle(html)));
  const cache = await caches.open(SNAP_PREFIX + partition);
  await cache.delete(key);
  await cache.put(key, new Response(html, { status: 200, statusText: 'OK', headers }));
  await pruneSnapshots(cache);
  return true;
}

/* The labelled saved copy of `key`, or null. Never another partition's. */
async function servableSnapshot(key, defer) {
  const cache = await openSnapshots(await currentPartition());
  if (!cache) return null;
  const hit = await cache.match(key, { ignoreVary: true });
  if (!hit) return null;
  const now = Date.now();
  const savedAt = Date.parse(hit.headers.get(SNAPSHOT_AT));
  const html = isFresh(savedAt, now) ? markSnapshot(await hit.clone().text(), savedAt, now) : null;
  if (html === null) {
    // Too old, or unlabelable: deleted, never served.
    await cache.delete(key);
    return null;
  }
  // Least recently used: serving it makes it the newest entry.
  defer(cache.delete(key).then(() => cache.put(key, hit)));
  const headers = new Headers(hit.headers);
  headers.set(SERVED_FROM, 'snapshot');
  return new Response(html, { status: 200, statusText: 'OK', headers });
}

function listed(url, title, savedAt, now) {
  return { url, title, savedAt: new Date(savedAt).toISOString(), shown: formatSavedAt(savedAt, now).text };
}

/* For the offline page ({type: 'list-snapshots'}): what the CURRENT
 * partition holds and may still be served, newest first. */
async function listSnapshots() {
  const cache = await openSnapshots(await currentPartition());
  if (!cache) return [];
  const now = Date.now();
  const items = [];
  for (const key of await cache.keys()) {
    const hit = await cache.match(key, { ignoreVary: true });
    if (!hit) continue;
    const savedAt = Date.parse(hit.headers.get(SNAPSHOT_AT));
    if (!isFresh(savedAt, now)) continue;
    let title = '';
    try {
      title = decodeURIComponent(hit.headers.get(SNAPSHOT_TITLE) || '');
    } catch (err) {
      title = '';
    }
    items.push(listed(key.url, title, savedAt, now));
  }
  return items.reverse();
}

/* The stored scoreboards (one identity's at most: see WHOSE PAGE), newest
 * first, for the offline page's "Live scoreboards you can keep scoring". */
async function listScoreboards() {
  const now = Date.now();
  const items = [];
  for (const name of await liveCacheNames()) {
    const cache = await caches.open(name);
    for (const key of await cache.keys()) {
      const hit = await cache.match(key, { ignoreVary: true });
      if (!hit) continue;
      const storedAt = Number(hit.headers.get(STORED_AT)) || 0;
      if (now - storedAt > LIVE_MAX_AGE_MS) continue;
      items.push(listed(key.url, extractTitle(await hit.text()), storedAt, now));
    }
  }
  return items.reverse();
}

/* Fetch the offline page into OFFLINE_CACHE unless it is there. No cookies:
 * it is the same for everyone, and must never be anyone's. */
async function precacheOfflinePage() {
  const cache = await caches.open(OFFLINE_CACHE);
  if (await cache.match(OFFLINE_URL)) return;
  const response = await fetch(OFFLINE_URL, { credentials: 'omit', cache: 'no-cache' });
  if (response.ok && !response.redirected && (!response.type || response.type === 'basic' || response.type === 'default')) {
    await cache.put(OFFLINE_URL, response);
  }
}

/* The offline page for a navigation with no answer and no saved copy, or
 * null (not precached: the browser's own error stands). Never stored as a
 * snapshot: it is answered from here, not from the network. */
async function offlinePage(reason) {
  if (!(await caches.has(OFFLINE_CACHE))) return null;
  const hit = await (await caches.open(OFFLINE_CACHE)).match(OFFLINE_URL, { ignoreVary: true });
  if (!hit) return null;
  const html = markOfflinePage(await hit.text(), reason);
  if (html === null) return null;
  const headers = new Headers(hit.headers);
  ['Content-Length', 'Content-Encoding'].forEach((name) => headers.delete(name));
  headers.set(SERVED_FROM, 'offline-page');
  headers.set('Cache-Control', 'no-store');
  return new Response(html, { status: 200, statusText: 'OK', headers });
}

async function notifySnapshotFresh(clientId, url) {
  if (!clientId) return;
  const client = await self.clients.get(clientId);
  if (client) client.postMessage({ type: 'snapshot-fresh', url: url.href });
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

    const timeout = new Promise((resolve) => setTimeout(resolve, LIVE_TIMEOUT_MS, null));
    const fallback = (answer) =>
      storedScoreboard(key).then((stored) => stored || answer || network);

    return Promise.race([network, timeout]).then(
      (response) => (response && response.status < 500 ? response : fallback(response)),
      () => fallback(null)
    );
  },
};

const TIMED_OUT = { timedOut: true };

const snapshotNavigation = {
  name: 'snapshotNavigation',
  match: (url, request) => isSnapshotNavigation(url, request),
  handle(event, url) {
    const key = snapshotKey(url);
    const defer = (promise) => event.waitUntil(promise.catch(() => {}));
    // Settles once we know whether the saved copy was shown: a late answer
    // only tells the page about itself if it was.
    let decide;
    const servedCopy = new Promise((resolve) => {
      decide = resolve;
    });

    // One fetch, never aborted. It keeps running past the timeout, so an
    // answer that comes late still refreshes (or evicts) the saved copy.
    const network = fetch(event.request).then((response) => {
      const copy = response.clone();
      defer(
        currentPartition().then(async (partition) => {
          const plan = planNavigation(classifyAnswer(copy, partition), false);
          if (plan.evict) await evictSnapshot(key);
          if (plan.store && (await storeSnapshot(key, copy, partition)) && (await servedCopy)) {
            await notifySnapshotFresh(event.resultingClientId || event.clientId, url);
          }
        })
      );
      return response;
    });
    defer(network.then(() => {}));

    // No saved copy: the offline page if the network failed, else (and if
    // the page isn't precached) the network's answer or error. Only with a
    // snapshot partition, i.e. the flag on: the kill switch turns it off too.
    const noCopy = async (kind, answer) => {
      const reason = offlineReason(kind, answer ? answer.status : 0);
      const page =
        reason && (await currentPartition()) ? await offlinePage(reason).catch(() => null) : null;
      return page || answer || network;
    };

    const fallback = async (kind, answer) => {
      const copy = await servableSnapshot(key, defer).catch(() => null);
      const plan = planNavigation(kind, Boolean(copy));
      decide(plan.respond === 'copy');
      if (plan.respond === 'copy') return copy;
      if (kind !== 'timeout') return noCopy(kind, answer);
      // Slow, not dead (yet): the network keeps its chance until NAV_GIVE_UP_MS.
      const giveUp = new Promise((resolve) =>
        setTimeout(resolve, NAV_GIVE_UP_MS - NAV_TIMEOUT_MS, TIMED_OUT)
      );
      return Promise.race([network, giveUp]).then(
        (late) => {
          if (late === TIMED_OUT) return noCopy('timeout', null);
          return late.status >= 500 ? noCopy('server-error', late) : late;
        },
        () => noCopy('error', null)
      );
    };

    // The phone says it has no network: don't wait to find out. (true means
    // nothing: iOS reports it on lie-fi.)
    const offline = Boolean(self.navigator && self.navigator.onLine === false);
    const timeout = new Promise((resolve) =>
      setTimeout(resolve, offline ? 0 : NAV_TIMEOUT_MS, TIMED_OUT)
    );

    return Promise.race([network, timeout]).then(
      (answer) => {
        if (answer === TIMED_OUT) return fallback(offline ? 'offline' : 'timeout', null);
        if (answer.status >= 500) return fallback('server-error', answer);
        decide(false);
        return answer;
      },
      () => fallback('error', null)
    );
  },
};

/* The route table. First match wins; no match means "not intercepted".
 *
 * Each entry is { name, match(url, request) -> boolean,
 * handle(event, url) -> Promise<Response> }. `match` must be cheap and
 * synchronous: it runs for every GET the app makes. `handle` owns its whole
 * strategy and may reject only where the browser would have failed anyway.
 * scoreboardShell stays ahead of snapshotNavigation: its URL is its own.
 */
const ROUTES = [staticAssets, scoreboardShell, snapshotNavigation];

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
    event.waitUntil(
      Promise.all([
        keepOnlyPartition(IDENTITY.test(identity) ? identity : null),
        setPartition(partitionFromMessage(data)),
        // Missed at install (no network then): every online page load retries.
        precacheOfflinePage().catch(() => {}),
      ])
    );
  } else if (data.type === 'list-snapshots') {
    // The offline page asks; it answers over the port it sent (or, without
    // one, to the page itself). Only the current partition, never another.
    const port = event.ports && event.ports[0];
    const reply = (message) => {
      if (port) port.postMessage(message);
      else if (event.source && event.source.postMessage) event.source.postMessage(message);
    };
    event.waitUntil(
      Promise.all([listSnapshots(), listScoreboards()]).then(
        ([snapshots, scoreboards]) => reply({ type: 'snapshots', snapshots, scoreboards }),
        () => reply({ type: 'snapshots', snapshots: [], scoreboards: [] })
      )
    );
  } else if (data.type === 'purge') {
    event.waitUntil(Promise.all([keepOnlyPartition(null), setPartition(null)]));
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
