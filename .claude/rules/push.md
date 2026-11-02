---
paths:
  - "ttstats/pingpong/push.py"
  - "ttstats/pingpong/notifications.py"
  - "ttstats/pingpong/templates/pingpong/sw.js"
  - "ttstats/pingpong/templates/pingpong/manifest.webmanifest"
  - "ttstats/pingpong/templates/pingpong/notification_settings.html"
  - "ttstats/pingpong/static/pingpong/js/push.js"
  - "ttstats/pingpong/management/commands/generate_vapid_keys.py"
  - "tests/e2e/pwa*.js"
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
- **`Match.result_notified_at` makes the result push exactly-once**
  (conditional `.update()` claim). Never set it by hand.
- **`sw.js` has no `fetch` handler**, deliberately.
- **A live service worker disables Playwright's `page.route`.** `iphone-safari`
  blocks workers; PWA specs run in `iphone-pwa`. Any spec using `page.route`
  belongs in a project with workers blocked.
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
