---
paths:
  - "ttstats/pingpong/push.py"
  - "ttstats/pingpong/notifications.py"
  - "ttstats/pingpong/notification_limits.py"
  - "ttstats/pingpong/templates/pingpong/sw.js"
  - "ttstats/pingpong/templates/pingpong/manifest.webmanifest"
  - "ttstats/pingpong/templates/pingpong/notification_settings.html"
  - "ttstats/pingpong/static/pingpong/js/push.js"
  - "ttstats/pingpong/static/pingpong/js/offline.js"
  - "ttstats/pingpong/offline.py"
  - "ttstats/pingpong/management/commands/generate_vapid_keys.py"
  - "tests/e2e/pwa*.js"
  - "tests/e2e/service-workers.spec.js"
  - "tests/e2e/*.pwa.spec.js"
  - "tests/e2e/offline-proxy.js"
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
- **`sw.js` caches two things and nothing else (KAN-82).** `ROUTES` =
  `staticAssets` (cache-first, only `/static/` names carrying a 12-hex
  manifest hash, so dev's unhashed files are never cached) then
  `scoreboardShell` (navigations to `/pingpong/matches/<pk>/live/` only:
  network first, 3 s timeout, stored copy on no answer/timeout/5xx). Anything
  else, every POST, `/live/state/` and `/live/sync/` included, is not
  intercepted; only same-origin GETs reach the table. A stale copy of any
  other page is a lie; the scoreboard rebuilds from bootstrap + outbox.
  A later route (KAN-131 snapshots) goes **after** `scoreboardShell`.
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
  current; stored pages never outlive their assets. Bump `VERSION` when the
  worker's logic changes. Bounds: static 120 entries, scoreboards 8 and 7 days.
- New offline behaviour is a new `ROUTES` entry. Tests: `test_push_views.py`
  (rendered worker), `test_offline.py`, `tests/e2e/scoreboard-offline.pwa.spec.js`.
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
