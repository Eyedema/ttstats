---
paths:
  - "ttstats/pingpong/templates/pingpong/sw.js"
  - "ttstats/pingpong/offline.py"
  - "ttstats/pingpong/static/pingpong/js/offline.js"
  - "ttstats/pingpong/static/pingpong/js/snapshot_mode.js"
  - "ttstats/pingpong/static/pingpong/js/snapshot_warm.js"
  - "ttstats/pingpong/static/pingpong/js/offline_page.js"
  - "ttstats/pingpong/static/pingpong/js/connection.js"
  - "ttstats/pingpong/templates/pingpong/offline.html"
  - "tests/js/sw_*.mjs"
  - "tests/js/snapshot_*.mjs"
  - "tests/e2e/offline*.js"
  - "tests/e2e/service-workers.spec.js"
  - "tests/e2e/*.pwa.spec.js"
---

# Offline: the service worker's caches

Two caches answer navigations with no signal: the live scoreboard (KAN-82,
writing offline) and read-only snapshots of other pages (KAN-131, reading
offline). Why each is shaped as it is: `docs/decisions/kan-76-offline-scoring.md`
and `docs/decisions/kan-131-offline-snapshots.md`.

## Invariants (KAN-131)

- **Stale only when labelled.** A snapshot is never served without the
  freshness rewrite; a copy missing `FRESHNESS_MARKER` or
  `SNAPSHOT_TIME_PLACEHOLDER` is neither stored nor served.
- **The server opts in.** A page is snapshot-able only if its view carries
  `@offline_snapshot` *and* its URL name is in `offline.SNAPSHOT_ALLOWLIST`;
  `test_offline_snapshots.py`'s URL sweep fails until both agree. Never opt in
  a form, `confirm/`, `join/`, `/live/`, `/accounts/` or anything that writes on GET.
- **Default route = pass-through.** Never `respondWith` a POST, htmx,
  `fetch()` or JSON.
- **One identity's copies exist at a time** (HMAC of user + active club);
  a mismatch, logout or `snapshots:'off'` wipes the others.
- **The banner is shown by CSS from an attribute only the worker writes**; not
  amber, not red. The `offline_snapshots` flag is the kill switch.

- **`sw.js` has three routes and nothing else.** `ROUTES` =
  `staticAssets` (cache-first, only `/static/` names carrying a 12-hex
  manifest hash, so dev's unhashed files are never cached), then
  `scoreboardShell` (KAN-82: navigations to `/pingpong/matches/<pk>/live/`
  only: network first, 3 s timeout, stored copy on no answer/timeout/5xx),
  then `snapshotNavigation` (KAN-133: other navigations under `/pingpong/`,
  never `/live/` or `HX-Request`). Anything else, every POST, htmx and
  `fetch()` (prefetch included), `/live/state/` and `/live/sync/`, is not
  intercepted; only same-origin GETs reach the table. The scoreboard is a
  shell that rebuilds from bootstrap + outbox; a snapshot is stale and is
  only ever served labelled.
- **Snapshots (KAN-131): the server opts in, the worker labels.** Stored only
  from a 2xx navigation carrying `X-TTStats-Snapshot: 1` whose identity equals
  the current partition, and only if it holds both `FRESHNESS_MARKER` and
  `SNAPSHOT_TIME_PLACEHOLDER` (`offline.py`). Network first, 4 s
  (`NAV_TIMEOUT_MS`; skipped when `navigator.onLine === false`): the copy is
  served only on no answer, timeout or 5xx, rewritten so CSS shows the
  banner, with `X-TTStats-Served-From: snapshot`. 3xx/401/403/404/410 and an
  un-opted 2xx evict it. The fetch is never aborted (a navigate-mode Request
  can't take a signal): a late opted-in answer is stored and the page gets
  `{type:'snapshot-fresh', url}`. `ttstats-snap-<BUILD>-<identity>`, LRU 60,
  7 days. The current partition is set only by `{type:'identity', identity,
  snapshots:'on'}` (persisted in `ttstats-snapstate-<BUILD>`); anything else
  (other identity, logged out, `snapshots:'off'` = kill switch, `purge`)
  deletes every other snapshot partition. Pure helpers are unit-tested in
  `tests/js/sw_snapshots.test.mjs` (vm harness `sw_harness.mjs`).
- **A saved copy is read-only** (KAN-146, `snapshot_mode.js`, loaded only
  with the flag on, after `offline.js`; inert unless
  `html[data-freshness="snapshot"]`). Capture-phase `submit`/`click` stop
  forms, htmx triggers and links outside `offline.OFFLINE_NAVIGABLE` (the
  allowlist + the scoreboard), rendered as anchored regex sources by
  `json_script` `snapshot-paths` (`offline_navigation_paths()`, pinned to
  `tests/fixtures/snapshot_paths.json`, which `tests/js/snapshot_mode.test.mjs`
  uses): toast "Needs a connection", nothing sent. Every htmx request is
  cancelled in `htmx:confirm`/`beforeRequest` (capture; toast only for a user
  trigger). Log out offline: toast, but `offline.js` still purges (it is never
  stopped). "You're back online · Refresh" on `snapshot-fresh` or a 2xx from a
  `HEAD /healthz` (KAN-109; a 503 is not "back") (`online`, tab shown, every 30 s); never auto-reloads;
  the button is JS-made. Amber is muted in CSS (app.css, `--ball` re-pointed).
  e2e: `offline-read-only.pwa.spec.js` (the proxy's `requests` log) and
  `.nojs`.
- **The offline page (KAN-137) is `/offline/`, outside `/pingpong/`** so no
  route claims it. Rendered without a request (`offline.offline_page_html`):
  the same bytes for everyone, no CSRF, no identity meta (an empty one would
  purge every copy). Precached into `ttstats-offline-<BUILD>` with no cookies;
  its HTML is digested into `BUILD`, so editing the template reinstalls the
  worker. Shown for a `/pingpong/` navigation with no saved copy only on a
  network error, the proxy's 502/503/504, or no answer by `NAV_GIVE_UP_MS`
  (12 s), and only while a snapshot partition exists (kill switch = off).
  Never for a 2xx/3xx/4xx/500. `offline_page.js` lists the current partition
  via `{type:'list-snapshots'}`.
- **Background warm-up (KAN-136)**: `base.html` renders `snapshot-warm`
  (`offline.warm_urls`: Today, the Table, own player page, last 3 confirmed
  matches; max 6, all allowlisted; one query, flag on only).
  `snapshot_warm.js` posts `{type:'warm', urls}` when idle, only if
  `TTStatsConnection.state()==='online'`, no Save-Data, not a saved copy.
  The worker fetches one at a time with `X-TTStats-Warm: 1`, stores by the
  navigation rules, at most once per 30 min per partition (state in
  `ttstats-snapstate-<BUILD>`, claimed before fetching), skips copies under
  10 min old, and stops on a partition change, no network, redirect,
  foreign identity or 5xx. Warm requests must stay side-effect free: the
  context processor empties `messages` for them (a flash would be consumed).
  Offline specs that count saved pages call `noWarming(page)`
  (`offline-warm-helpers.js`); the proxy counts `net.warmRequests`.
- **A stored scoreboard is one person's** (CSRF token, player ids). Stored only
  from a 200 carrying `X-TTStats-Identity` (`offline.request_identity`: HMAC of
  user + active club, KAN-132's fingerprint), filed in
  `ttstats-live-<BUILD>-<identity>`; at most one identity's partition exists.
  `offline.js` (every page, `base.html` and `base_auth.html`) posts
  `{type:'identity'}` from `<meta name="ttstats-identity">` (empty when logged
  out: drop all) and `{type:'purge'}` on submit of a `data-sw-purge` form (Log
  out). A redirect/403/404/410 for a scoreboard evicts it, as does
  `{type:'live-match-complete', url}` from the page. Switching club drops the
  copy until the next online open. Not `Clear-Site-Data`: its `"storage"` type
  would also wipe the outbox and the worker.
- **Cache names carry `BUILD` = `VERSION` + a digest of the precache URLs**
  (`offline.SW_PRECACHE`, content-hashed), so any deploy changing them
  reinstalls the worker and `activate` drops every `ttstats-*` cache not
  current; stored pages never outlive their assets (a deploy empties the
  snapshots too). Bump `VERSION` when the worker's logic changes. Bounds:
  static 120 entries, scoreboards 8 and 7 days, snapshots 60 and 7 days.
- New offline behaviour is a new `ROUTES` entry. Tests: `test_push_views.py`
  (rendered worker), `test_offline.py`, `test_offline_snapshots.py`,
  `test_offline_page.py`, `test_offline_warm.py`, `tests/js/sw_*.test.mjs`,
  `tests/js/snapshot_warm.test.mjs`, `tests/e2e/offline-warm.pwa.spec.js`,
  `tests/js/offline_page.test.mjs`, `tests/e2e/scoreboard-offline.pwa.spec.js`,
  `tests/e2e/offline-page.{pwa,nojs}.spec.js`,
  `tests/e2e/offline-snapshots.pwa.spec.js` (users `e2e-reader`,
  `e2e-reader-b`, `e2e-reader-off`: only they have the flag on).
- **Never test the worker offline with `context.setOffline()`**: WebKit fails
  the loads before the worker runs, so nothing is ever served from cache and
  "not cached" assertions pass vacuously. Cut the network with
  `tests/e2e/offline-proxy.js` (`online`/`offline`/`hang`), and pair every
  "not served" check with one that is served over the same dead network.
- **A live service worker disables Playwright's `page.route`** (WebKit: always;
  Chromium/Gecko: once a route calls `respondWith()`). Workers are blocked in
  the top-level `use` of `playwright.config.js`; only `iphone-pwa` allows them
  (pinned by `tests/e2e/service-workers.spec.js`). A spec there needing
  `page.route` blocks workers itself or asserts the route applied. Per-engine
  measurements: `docs/decisions/kan-76-sw-e2e-audit.md`.
