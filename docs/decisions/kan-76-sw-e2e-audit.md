# KAN-96: e2e audit for service-worker interception

Part of KAN-76 (offline scoring), before S7.6 (KAN-82) gives `sw.js` routes
that answer requests. To be folded into the KAN-76 decision record.

## The question

Once a service worker controls a page, does Playwright's `page.route()` still
see that page's requests? Every abort/delay rule in the suite, and
`applyProdCSP()` in `tests/e2e/helpers.js`, is built on it. If the rule is
skipped, the request goes to the server without it: a spec that aborts
`/live/point/` fails looking for an error it never provoked, and a CSP spec
passes against a page that never had a policy.

## Measured (2026-11-07, Playwright 1.62)

The probe: let the worker claim the page (`navigator.serviceWorker.controller`
set), install a route, then make a request. "Seen" = the route handler ran.

| Request from a controlled page | WebKit | Chromium | Gecko |
| --- | --- | --- | --- |
| **No fetch handler** (sw.js before KAN-96) | | | |
| subresource `fetch()` (GET) | **missed** | seen | seen |
| POST | **missed** | seen | seen |
| navigation (`page.goto`) | seen | seen | seen |
| **Pass-through handler** (empty route table, KAN-96) | | | |
| subresource `fetch()` (GET) | **missed** | seen | seen |
| POST | **missed** | seen | seen |
| navigation | seen | seen | seen |
| **A route calling `respondWith(fetch(req))`** for every GET | | | |
| subresource GET | **missed** | **missed** | **missed** |
| POST (the GET-only guard keeps it out) | **missed** | seen | seen |
| navigation | seen | **missed** | **missed** |
| **Same, with `context.route()` instead** | | | |
| subresource GET | **missed** | seen | **missed** |
| POST | **missed** | seen | seen |
| navigation | seen | seen | **missed** |

What follows from it:

1. **In WebKit any controlling worker hides subresources and POSTs from
   `page.route()`, fetch handler or not.** The handler-less `sw.js` was never
   what kept the suite honest; blocking the worker in `iphone-safari` was. The
   `iphone-dark` and `iphone-reduced` projects (WebKit, workers allowed) were
   exposed already and passed by winning the race against `clients.claim()`.
2. **The pass-through handler changes nothing in any engine.** That is the
   canary: with it in place and workers allowed in *every* project (iphone-safari
   included; only the spec-level blocks left), the full suite still passed --
   194 passed, 2 skipped, the same as the base branch. Given point 1, that
   is luck in the claim race, not safety.
3. **A route that calls `respondWith()` breaks Chromium and Gecko too**,
   including for navigations, which is what `applyProdCSP` relies on. KAN-82's
   scoreboard-HTML route is exactly that.
4. **`context.route()` is not a fix**: it only reaches worker requests in
   Chromium. Blocking the worker is the only cross-engine answer.
5. The worker's GET-only guard is load-bearing: it keeps every POST (a scored
   point) out of the route table, and so visible to `page.route()` in Chromium
   and Gecko even when a catch-all GET route exists.

## Projects

| Project | Engine | Runs | Workers before | Workers after |
| --- | --- | --- | --- | --- |
| `setup` | WebKit | `auth.setup.js` | allowed | blocked |
| `iphone-safari` | WebKit | plain `*.spec.js` | blocked | blocked |
| `iphone-pwa` | WebKit | `*pwa.spec.js` | allowed | **allowed (the only one)** |
| `iphone-no-js` | WebKit, no JS | `*.nojs.spec.js` | allowed (moot: no JS, no registration) | blocked |
| `iphone-reduced` | WebKit | `*.reduced.spec.js` | allowed | blocked |
| `iphone-dark` | WebKit | `*.dark.spec.js` | allowed | blocked |
| `android-chrome` | Chromium | `*.android.spec.js` | allowed | blocked |
| `android-firefox` | Gecko | `*.android.spec.js` | allowed | blocked |
| `desktop-chrome` | Chromium | `*.desktop.spec.js` | allowed | blocked |

`serviceWorkers: 'block'` now sits in the top-level `use`; `iphone-pwa` alone
sets `'allow'`. A new project is blocked unless it opts in, and
`tests/e2e/service-workers.spec.js` pins that.

## Specs using `page.route` or `applyProdCSP`

| Spec | Project | How | Before | Exposed? |
| --- | --- | --- | --- | --- |
| `scoreboard.spec.js` | iphone-safari | `page.route` delay/abort `/live/point/` (POST) | blocked | no |
| `mobile-drawer.spec.js` | iphone-safari | `page.route` abort Alpine, `app.css` | blocked | no |
| `tap-feedback.spec.js` | iphone-safari | `applyProdCSP` + abort a navigation | blocked | no |
| `club-invite`, `club-switcher`, `csp`, `glicko-cutover`, `momentum`, `navigation`, `point-stats`, `point-stats.h2h`, `season-archive`, `season-leaderboard`, `view-transitions` | iphone-safari | `applyProdCSP` | blocked | no |
| `prefetch.spec.js` | iphone-safari | `applyProdCSP`; own persistent contexts set `serviceWorkers: 'block'` | blocked | no |
| `momentum.reduced`, `view-transitions.reduced` | iphone-reduced | `applyProdCSP` | **allowed** | **yes** -> fixed by project block |
| `pull-to-refresh.reduced` | iphone-reduced | `applyProdCSP` | blocked in spec | no |
| `glicko-cutover.dark`, `momentum.dark`, `palette.dark`, `point-stats.dark`, `season-leaderboard.dark`, `view-transitions.dark` | iphone-dark | `applyProdCSP` | **allowed** | **yes** -> fixed by project block |
| `glicko-cutover.desktop`, `view-transitions.desktop` | desktop-chrome | `applyProdCSP` | **allowed** | yes, once a route uses `respondWith` -> fixed |
| `prefetch.desktop`, `prefetch.android` | desktop-chrome, android-* | `page.on('request')` + navigation headers (no route) | **allowed** | yes, once a route uses `respondWith` (the prefetch would be the worker's) -> fixed |
| `view-transitions.android`, `pull-to-refresh.android` | android-* | `applyProdCSP` | blocked in spec | no |
| `pull-to-refresh.pwa` | iphone-pwa | `applyProdCSP` | blocked in spec (`pull-to-refresh-cases.js`) | no |
| `pwa.spec.js` "under the production CSP" | iphone-pwa | `applyProdCSP` | **allowed (by design)** | navigations only, fresh context each test -> now **asserts** the document carries the CSP |

The spec-level `test.use({ serviceWorkers: 'block' })` lines outside
`iphone-pwa` are now redundant but harmless; the one in
`pull-to-refresh-cases.js` is still needed, because `pull-to-refresh.pwa`
runs in `iphone-pwa`.

## Rules for KAN-82 / KAN-131

- Offline specs go in `iphone-pwa` (`*pwa.spec.js`). Drive the network with
  `context.setOffline()`, not `page.route()`. (Superseded by the KAN-82
  addendum below: in WebKit, use `offline-proxy.js`, not `setOffline()`.)
- A `iphone-pwa` spec that needs `page.route()` either blocks workers for
  itself or asserts the route applied (`gotoUnderProdCSP` in `pwa.spec.js`).
- Keep the worker's GET-only, same-origin guard in front of the route table.
- `pwa.spec.js` "the fetch handler" asserts that a page no route claims is not
  served from cache offline; keep it green as routes are added.

## KAN-82 addendum: `context.setOffline()` cannot test a worker in WebKit

Measured 2026-11-07 while adding the scoreboard route. With the worker
controlling the page and `context.setOffline(true)`:

- a `fetch()` of a hashed stylesheet the worker had **in its cache** (cache
  first, no network involved) failed with `TypeError: Load failed`;
- a reload of the stored scoreboard failed with "WebKit encountered an
  internal error".

WebKit's offline emulation fails the page's loads before the worker's fetch
handler runs. So in WebKit a spec using `setOffline()` can never see a page
served from cache, and an assertion that a page is *not* served from cache
passes whatever the worker does (the KAN-96 canary in `pwa.spec.js` was
vacuous in this sense).

Offline specs therefore cut the network for real: `tests/e2e/offline-proxy.js`
sits between the browser and the e2e server on its own port (cookies are per
host, so the session applies; the worker registers afresh per test) and can
be switched `online` / `offline` (connections destroyed) / `hang` (accepted,
never answered: lie-fi). Every "not served offline" assertion is paired with a
scoreboard that *is* served over the same dead network, so it cannot pass
vacuously.
