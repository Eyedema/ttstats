---
paths:
  - "ttstats/pingpong/push.py"
  - "ttstats/pingpong/notifications.py"
  - "ttstats/pingpong/notification_limits.py"
  - "ttstats/pingpong/templates/pingpong/sw.js"
  - "tests/js/sw_*.mjs"
  - "ttstats/pingpong/templates/pingpong/manifest.webmanifest"
  - "ttstats/pingpong/templates/pingpong/notification_settings.html"
  - "ttstats/pingpong/static/pingpong/js/push.js"
  - "ttstats/pingpong/static/pingpong/js/offline.js"
  - "ttstats/pingpong/static/pingpong/js/snapshot_mode.js"
  - "ttstats/pingpong/offline.py"
  - "ttstats/pingpong/management/commands/generate_vapid_keys.py"
  - "tests/e2e/pwa*.js"
  - "tests/e2e/service-workers.spec.js"
  - "tests/e2e/*.pwa.spec.js"
  - "tests/e2e/offline-proxy.js"
  - "tests/e2e/offline-snapshot*.js"
  - "tests/e2e/offline-read-only*.js"
---

# PWA and web push

`push.py` is the transport (talks to push services, prunes dead devices, never
raises). `notifications.py` is the vocabulary (what each event says, to whom).

- **Push is off unless `VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY` are set**; every
  send is then a no-op and the app emails as before. Rotating the keys
  unsubscribes every device.
- **`/sw.js` and `/manifest.webmanifest` are Django views at the site root.** A
  worker under `/static/` could only control `/static/`; the manifest needs
  `{% static %}` for hashed icon URLs.
- **Registering the worker and supporting push are different capabilities.**
  iOS exposes `PushManager` only after install. `push.js` splits `swSupported`
  from `pushSupported`; don't re-merge them.
- **Push preferred, email as fallback.** Every `notify_*` returns the Player ids
  it reached; callers email the rest. A muted preference or failed send falls
  back to email.
- **Daily limits are enforced in `push.send_to_user`**, the one path every
  `notify_*` uses (`notification_limits.allow`). The cache holds per-kind and
  overall counters per user per UTC day, and they expire at UTC midnight.
  Defaults: `NOTIFICATION_DAILY_LIMITS = {'season_ended': 1}` and
  `NOTIFICATION_DAILY_CEILING = 50`, so existing kinds are unaffected in
  normal use. The limiter **fails closed**: if the cache errors, no push is
  sent. A suppressed push counts as not reached, so the caller emails instead,
  and the suppression is logged. Limits count only pushes that would really go
  out: muted kinds and users without a device don't count. The test push
  (`respect_preferences=False`) bypasses the limits.
- **A new `NotificationKind` needs a `push_<value>` field and a migration.**
  The settings page renders every kind. `test_push_views` fails for a kind
  with no field, because without the field it could not be muted.
- **`Match.result_notified_at` makes the result push exactly-once**
  (conditional `.update()` claim). Never set it by hand.
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
  `HEAD` of the manifest (`online`, tab shown, every 30 s); never auto-reloads;
  the button is JS-made. Amber is muted in CSS (app.css, `--ball` re-pointed).
  e2e: `offline-read-only.pwa.spec.js` (the proxy's `requests` log) and
  `.nojs`.
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
  `tests/js/sw_*.test.mjs`, `tests/e2e/scoreboard-offline.pwa.spec.js`,
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
- **`push.js` is plain JS and fails closed**: every `[data-push-state]` block
  starts `hidden` and JS reveals exactly one.
- The CSRF cookie is `HttpOnly`, so `push.js` reads its token (and the public
  VAPID key) from the `push_config` json_script blob (`push_context`).
- `CSP_WORKER_SRC` and `CSP_MANIFEST_SRC` are named explicitly in `prod.py` and
  `PROD_CSP`.
- **Icons are committed PNGs** from `python scripts/make_icons.py` (needs
  Pillow, deliberately not in requirements.txt). Keep the filenames.
- `scripts/e2e_server.sh` exports a throwaway, committed VAPID pair so e2e runs
  see a configured server.
