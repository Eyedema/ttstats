# KAN-119: page speed — baseline, method, budgets

Epic KAN-119 makes the app feel faster on a phone. Every story in it claims a
speed-up; this record holds the numbers that claim is checked against.
S10.1 (KAN-121) added the tooling and the baseline below. Each later story
appends its "after" row and tightens `tests/e2e/budgets.spec.js`.

## Method

**Two tools, two jobs.**

| | `npm run perf` (`scripts/perf.mjs`) | `tests/e2e/budgets.spec.js` |
|---|---|---|
| Measures | bytes, requests, TTFB, FCP, LCP, DCL, load, `Server-Timing` | bytes and requests only |
| Engine | Chromium (only it has CDP throttling) | WebKit, `iphone-safari` project |
| Runs | locally, on demand; median of 5, cold + warm | every CI e2e run, cold |
| Fails on | nothing (it's a report) | any page over its budget |

**Server: dev settings plus prod's static pipeline** (`ttstats.settings.e2e`,
started by `scripts/e2e_server.sh` for both the e2e suite and perf).
`collectstatic` runs through WhiteNoise's `CompressedManifestStaticFilesStorage`
into a throwaway `STATIC_ROOT`; `runserver --nostatic` leaves `/static/` to
WhiteNoise, with autorefresh and finders off, so it serves exactly what prod
serves: hashed names, `Content-Encoding: gzip`, `Cache-Control: max-age=315360000,
immutable`. Django returns unhashed URLs under `DEBUG=True`, so the e2e
storage forces `url(..., force=True)`; `DEBUG` stays on because `seed_e2e`
requires it. HTML is gzipped by `GZipMiddleware` as in prod. Perf uses its own
port (8126), SQLite file and `STATIC_ROOT`, so it can run beside the suite.

**Perf profile.** iPhone 13 viewport in Chromium, CDP
`Network.emulateNetworkConditions` at 400 kbit/s down, 400 kbit/s up, 400 ms
latency. Service workers blocked (`sw.js` only receives push). Logged in as the
seeded `e2e` user; the server's LocMemCache is warmed by one unrecorded load of
each page. Runs interleave pages and modes. Cold = fresh browser context;
warm = same context, page loaded once, then `about:blank`, then the measured
navigation (a navigation, not a reload, so `immutable` assets aren't revalidated).

**Metrics.** Bytes = encoded body + headers (CDP `encodedDataLength`;
WebKit `request.sizes()` in the budgets), KB = 1024 B. TTFB = the document's
`receiveHeadersEnd`, which includes the emulated RTT (Navigation Timing's
`responseStart` does not under CDP emulation). "Server ms" = `responseStart −
requestStart`: the server's own time, unthrottled. LCP from a buffered
`PerformanceObserver`. `Server-Timing` entries are printed when present
(absent at baseline; S10.3 adds them).

**Stability.** Two full 5-run reports, back to back: every byte, TTFB, FCP,
LCP, DCL and load median agreed within 1.6 %; LCP spread within a single run
was ≤ 4 %. "Server ms" is single-digit milliseconds on loopback, so its
relative jitter is large (±3 ms); compare it in absolute terms.

**Known gaps vs. prod.** `runserver` speaks HTTP/1.1 (prod's proxy likely
HTTP/2 — six-connection limit here, multiplexed there); SQLite with seeded
data, not Postgres with real volume; loopback has no TLS handshake; no CPU
throttling. Treat absolute times as a model and the deltas between reports
as the result.

## Baseline

Revision `3437323` (tooling changes only on top — no app code changed),
2026-11-06, Chromium 151.0.7922.34. Median of 5 runs.

### Cold cache

| Page | Reqs | HTML KB | JS KB | CSS KB | Font KB | Total KB | Server ms | TTFB ms | FCP ms | LCP ms | DCL ms | Load ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| dashboard | 8 | 10.6 | 37.3 | 8.6 | 43.1 | 99.6 | 13 | 421 | 2068 | 2068 | 3071 | 3126 |
| play | 8 | 10.1 | 37.3 | 8.6 | 43.1 | 99.1 | 6 | 421 | 2060 | 2060 | 3100 | 3125 |
| leaderboard | 8 | 11.7 | 37.3 | 8.6 | 43.1 | 100.7 | 8 | 421 | 2072 | 2072 | 3073 | 3124 |
| match list | 8 | 12.5 | 37.3 | 8.6 | 43.1 | 101.5 | 11 | 419 | 2064 | 2064 | 3101 | 3125 |
| match detail (momentum) | 9 | 12.5 | 106.7 | 8.6 | 43.1 | 170.9 | 18 | 420 | 2364 | 2364 | 4271 | 4271 |
| player detail | 9 | 16.6 | 106.7 | 8.6 | 43.1 | 174.9 | 15 | 420 | 2372 | 2372 | 4264 | 4264 |
| head-to-head | 9 | 14.8 | 106.7 | 8.6 | 43.1 | 173.2 | 9 | 420 | 2368 | 2368 | 4256 | 4257 |

### Warm cache

Every static asset comes from the HTTP cache (hashed + immutable); only the
document crosses the network.

| Page | Reqs (net/cache) | Total KB | Server ms | TTFB ms | FCP ms | LCP ms | DCL ms | Load ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| dashboard | 1/7 | 10.6 | 12 | 420 | 616 | 616 | 636 | 637 |
| play | 1/7 | 10.1 | 9 | 419 | 608 | 616 | 609 | 615 |
| leaderboard | 1/7 | 11.7 | 7 | 418 | 620 | 620 | 636 | 637 |
| match list | 1/7 | 12.5 | 15 | 419 | 616 | 616 | 669 | 670 |
| match detail (momentum) | 1/8 | 12.5 | 18 | 418 | 620 | 620 | 706 | 706 |
| player detail | 1/8 | 16.6 | 15 | 419 | 620 | 620 | 755 | 755 |
| head-to-head | 1/8 | 14.8 | 10 | 419 | 624 | 624 | 725 | 726 |

### What the baseline says

- **Cold, the network is the whole story**: ~100 KB at 50 KB/s is ~2 s before
  first paint. Server time is ≤ 20 ms everywhere.
- **Fonts (43 KB, three Archivo weights) outweigh all the JS** on the
  non-chart pages (htmx 17 KB + Alpine 17 KB + push.js 3.6 KB).
- **Chart.js (69 KB compressed) costs ~1.2 s of DCL** on the three chart pages
  and ~0.3 s of FCP (bandwidth contention).
- **DCL trails FCP by ~1 s cold**: scripts still downloading after first paint.
- Warm, every page paints in ~620 ms: one 400 ms RTT plus the HTML. Only
  fewer round trips or less HTML moves that number.
- Only gzip: `brotli` is not installed, so WhiteNoise writes no `.br` files
  (fixed by S10.2).

## Budgets (CI)

`tests/e2e/budgets.spec.js`, cold, WebKit (bytes from `request.sizes()`,
which reports the compressed body; WebKit also fetches `manifest.webmanifest`,
hence one more request than the Chromium table).

| Page | WebKit baseline (JS / CSS / total KB, reqs) | Budget (JS / CSS / total KB, reqs) |
|---|---|---|
| dashboard | 37.3 / 8.6 / 100.5, 9 | 40 / 10 / 106, 10 |
| play | 37.3 / 8.6 / 100.1, 9 | 40 / 10 / 106, 10 |
| leaderboard | 37.3 / 8.6 / 101.7, 9 | 40 / 10 / 108, 10 |
| match list | 37.3 / 8.6 / 102.5, 9 | 40 / 10 / 108, 10 |
| match detail | 106.7 / 8.6 / 171.8, 10 | 112 / 10 / 180, 11 |
| player detail | 106.7 / 8.6 / 175.8, 10 | 112 / 10 / 184, 11 |
| head-to-head | 106.7 / 8.6 / 174.1, 10 | 112 / 10 / 182, 11 |

Proven once: adding Chart.js to the dashboard (`{% block extra_js %}`) failed
the dashboard row at JS 106.7 > 40 and total 169.9 > 106; reverted.

## After

One subsection per story: revision, the perf table (or the rows that moved),
and the budget rows tightened.

### After S10.2 (KAN-122): Brotli, deferred htmx, no vendored maps

Revision: `feature/kan-122` on top of `0010354`, 2026-11-06, same Chromium,
profile and method as the baseline. Median of 5.

**What changed.** `brotli==1.2.1` in `requirements.txt`, so WhiteNoise writes
a `.br` beside every `.gz` at collectstatic and serves it to
`Accept-Encoding: br`. htmx in `<head>` became `defer` (still before Alpine).
`npm run vendor` no longer copies `.map` files and strips the
`sourceMappingURL` comments (and deletes stale maps); browsers never fetched
them, so this saves image size and collectstatic time, not page bytes.

**Cold cache** (Δ vs. baseline):

| Page | Reqs | JS KB | CSS KB | Total KB | FCP ms | LCP ms | DCL ms | Load ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| dashboard | 8 | 33.7 (−3.6) | 7.5 (−1.1) | 94.9 (−4.7) | 1504 (−564) | 1504 (−564) | 2795 (−276) | 2822 (−304) |
| play | 8 | 33.7 (−3.6) | 7.5 (−1.1) | 94.3 (−4.8) | 1496 (−564) | 1496 (−564) | 2797 (−303) | 2823 (−302) |
| leaderboard | 8 | 33.7 (−3.6) | 7.5 (−1.1) | 95.9 (−4.8) | 1484 (−588) | 1484 (−588) | 2776 (−297) | 2796 (−328) |
| match list | 8 | 33.7 (−3.6) | 7.5 (−1.1) | 96.8 (−4.7) | 1500 (−564) | 1500 (−564) | 2792 (−309) | 2819 (−306) |
| match detail (momentum) | 9 | 93.7 (−13.0) | 7.5 (−1.1) | 156.9 (−14.0) | 1524 (−840) | 1524 (−840) | 4025 (−246) | 4027 (−244) |
| player detail | 9 | 93.7 (−13.0) | 7.5 (−1.1) | 160.9 (−14.0) | 1536 (−836) | 1536 (−836) | 4020 (−244) | 4021 (−243) |
| head-to-head | 9 | 93.7 (−13.0) | 7.5 (−1.1) | 159.2 (−14.0) | 1528 (−840) | 1528 (−840) | 4017 (−239) | 4019 (−238) |

HTML, fonts, TTFB and server ms did not move (HTML is still gzip; woff2 is
already compressed). **Warm cache** did not move either (FCP 612–620 ms):
only the document crosses the network there.

**Which change did what.** One extra run (dashboard and match detail only)
with Brotli on but htmx still blocking:

| Page | FCP: baseline → Brotli only → + defer | DCL: baseline → Brotli only → + defer |
|---|---|---|
| dashboard | 2068 → 2028 → 1504 | 3071 → 3066 → 2795 |
| match detail | 2364 → 2300 → 1524 | 4271 → 4023 → 4025 |

- **Deferring htmx is the paint win** (−520 to −780 ms FCP): the parser no
  longer stops to fetch 15 KB before rendering the body.
- **Brotli is the byte win** (−4.7 KB on every page, −14 KB on chart pages;
  Chart.js 69 → 60 KB) and, on the chart pages, the DCL win (−250 ms), since
  DCL there waits on Chart.js's bytes.
- **Dynamic Brotli for HTML: skipped.** Dashboard HTML is 10.0 KB gzip-6 vs.
  9.1 KB Brotli q5 (8.2 KB q11): ~1 KB, ~20 ms at this profile, for a custom
  middleware on every response. Not worth it.

**WebKit and Brotli over HTTP.** WebKit (like Safari) only sends
`Accept-Encoding: br` over HTTPS, and the e2e server is plain HTTP, so the
suite's WebKit kept getting gzip. Prod is HTTPS, so iPhones get `.br`.
`budgets.spec.js` therefore sends `Accept-Encoding: br, gzip, deflate`
explicitly (WebKit decodes it fine) so the budgets weigh what a phone gets.

**Budgets tightened** (WebKit, cold, Brotli; budget ≈ measured + 5 %):

| Page | Measured (JS / CSS / total KB, reqs) | Budget (JS / CSS / total KB, reqs) |
|---|---|---|
| dashboard | 33.7 / 7.5 / 95.7, 9 | 36 / 8 / 101, 10 |
| play | 33.7 / 7.5 / 95.3, 9 | 36 / 8 / 100, 10 |
| leaderboard | 33.7 / 7.5 / 96.8, 9 | 36 / 8 / 102, 10 |
| match list | 33.7 / 7.5 / 97.6, 9 | 36 / 8 / 103, 10 |
| match detail | 93.7 / 7.5 / 157.8, 10 | 99 / 8 / 166, 11 |
| player detail | 93.7 / 7.5 / 161.7, 10 | 99 / 8 / 170, 11 |
| head-to-head | 93.7 / 7.5 / 160.1, 10 | 99 / 8 / 169, 11 |

**Prod check pending deploy.** Before this story,
`curl -sI -H 'Accept-Encoding: br, gzip' https://pingpong.ubaldopuocci.org/static/pingpong/css/app.d453d2f8b455.css`
answered `content-encoding: gzip` (HTTP/2, gunicorn, 8312 B,
`max-age=315360000, public, immutable`); with `br` alone, identity (41803 B).
The proxy passes gzip through untouched; after deploy the same request should
show `content-encoding: br` (7234 B).

_Pending: CONN_MAX_AGE / cached sessions / Server-Timing, prefetch, view
transitions, pull-to-refresh._

### After S10.3 (KAN-123): Server-Timing, persistent connections, cached sessions

2026-11-06, Chromium 151.0.7922.34, median of 5. "Before" is `0010354`
measured the same day on the same machine; "after" is `563e0c6`.

**What changed.** `ServerTimingMiddleware` in every environment
(`app;dur=`, `db;dur=;desc="N queries"`). In prod only: `CONN_MAX_AGE = 60`
(`DB_CONN_MAX_AGE`) with `CONN_HEALTH_CHECKS`, and `cached_db` sessions when
`REDIS_URL` is set.

**What this server can't show.** The perf server is `settings.e2e`: SQLite
over loopback, LocMemCache, no `prod.py`. Persistent connections and
`cached_db` are prod-only, so they are not exercised here. On SQLite a
"connection" is opening a file, and that cost is what `CONN_MAX_AGE` would
remove on Postgres anyway. No local Postgres was available to measure it
(and the Supabase dev clone is prod data, so it is off limits). What they save
in prod: a TCP + auth + `SET` round trip to the `db` container per request,
and the `django_session` SELECT on every authenticated request. Check it on
prod with the new header: `db;desc` should fall by one query per logged-in
page once Redis is serving sessions.

**TTFB: unchanged, as expected.** The table is warm cache (cold rows moved the
same way). Server ms is single-digit on loopback, and ±3 ms is noise (see
Stability).

| Page | Server ms before → after | TTFB ms before → after | Server-Timing after (ms) |
|---|---:|---:|---|
| dashboard | 11 → 12 | 418 → 421 | app=11 db=1 |
| play | 5 → 6 | 419 → 421 | app=5 db=0 |
| leaderboard | 7 → 8 | 421 → 419 | app=8 db=0 |
| match list | 12 → 11 | 418 → 419 | app=11 db=1 |
| match detail | 17 → 21 | 419 → 418 | app=20 db=2 |
| player detail | 17 → 16 | 417 → 419 | app=15 db=1 |
| head-to-head | 9 → 10 | 421 → 417 | app=9 db=1 |

**What the header says.** Server time is ≤ 20 ms and **the database is ≤ 2 ms
of it** on every key page. The rest is Python: views, template rendering,
gzip. On this data the server is not where the page's time goes. Chart pages
spend the most, and it isn't in queries.

**Overhead** (microbenchmark, `RequestFactory`, in-memory SQLite, 20k
requests): ~2.6 µs per request plus ~0.3 µs per query, i.e. ~8 µs on a
20-query page, < 0.1 % of the ~10 ms server time. It is too small to show in
the table above.

**Bytes.** The "after" run shows JS 33.7 / CSS 7.5 KB instead of 37.3 / 8.6.
That is not this story. `brotli` was installed in the shared virtualenv
between the two runs (S10.2's work), so WhiteNoise started serving `.br`.
S10.3 changes no bytes, so no budget row moves.

_Pending: Brotli, defer htmx, prefetch, view transitions, pull-to-refresh._

### After S10.6 (KAN-126): pull to refresh in the installed app

2026-11-06, WebKit `budgets.spec.js`, cold, Brotli. Bytes only; S10.6 is not
a speed change.

| Item | Bytes |
|---|---:|
| `pull_to_refresh.js` raw | 5,788 |
| ... Brotli body (gzip: 2,487) | 2,038 |
| ... on the wire, headers included | 2,506 |
| `app.css` (`.ptr`, `html.ptr-on`), measured CSS 7.5 → 7.7 KB | ~200 |
| `refresh-cw` symbol in the inlined sprite (every HTML page, raw) | ~360 |

Every page: JS +2.4 KB (33.7 → 36.1, chart pages 93.7 → 96.2), +1 request.
`budgets.spec.js` rows raised by exactly that (+3 KB js/total, +1 request).

**Engines.** Active only where there's no browser chrome to refresh with:
`display-mode: standalone`/`fullscreen` (the manifest says `standalone`, no
`display_override`) or iOS `navigator.standalone`. While active,
`html.ptr-on` sets `overscroll-behavior-y: contain`, which both Chrome and
Firefox honour as "no native pull-to-refresh", so ours never doubles up.

- **iOS Safari, installed:** no native pull-to-refresh and no reload button.
  The case the story exists for.
- **Chrome for Android, installed (WebAPK):** no Chrome documentation says
  standalone disables native pull-to-refresh, and developers report it still
  firing in installed apps (Framework7 forum, 2020). We contain it rather
  than rely on its absence. A Chrome *tab* is never touched.
- **Firefox for Android:** native pull-to-refresh is a browser-wide setting,
  default off for new installs since August 2023, and installed apps
  have no refresh UI (Mozilla support); Mozilla closed a PWA-specific one as
  WONTFIX (bug 1162150). Firefox honours `overscroll-behavior-y: contain`
  (bug 1830981). Unverified: whether Fenix's installed web apps report
  `display-mode: standalone`; if one reports `browser`, ours stays off and
  the app is no worse than before.

**Verified in e2e** (production CSP, synthesised touch): WebKit/iPhone 13
(`iphone-pwa`, plus `iphone-reduced`, `iphone-no-js`), Chromium/Pixel 7
(`android-chrome`) and Gecko at 412×915 with touch (`android-firefox`, an
approximation: Playwright's Firefox has no mobile emulation). Each covers
reload on a long pull, no-ops on a short or horizontal drag, the scoreboard,
a dirty form, the open drawer, offline, and a non-installed tab.

### After S10.4 (KAN-124): tap-intent prefetch

2026-11-06, `feature/kan-124`. Chromium 151.0.7922.34, WebKit 26.5 and
Firefox (Playwright 1.62.1 builds).

**What shipped.** `pingpong/js/prefetch.js` (deferred, plain JS, no inline
script) fetches an `a[data-prefetch]` page on `touchstart` (or `mousedown`)
with `Purpose: prefetch`. `ttstats/prefetch.py` (`PrefetchCacheMiddleware`)
marks only that response `Cache-Control: private, max-age=5`; every other
HTML response stays uncacheable, and a view's own `Cache-Control` wins. The
click's navigation is then answered from the HTTP cache. Allowlist: the four
tabs and the menu rows (all idempotent GET pages; Admin opts out). Code
denylist on top (`/live/`, `confirm/`, `join/`, logout, admin, `/accounts/`,
`/webauthn/`, edit/delete/start/register/convert/switch), plus Save-Data.

#### Spike: a / b / c in each engine

Method: a proxy in front of the e2e server adding 400 ms per request and
logging every document request; page = leaderboard, logged in; the prefetch
response given `private, max-age=10` by the proxy (the speculation-rule and
link rows without it). "Hits" = server requests for the target URL.
Scripts were throwaway (not committed); the lasting checks are the e2e specs.

| | WebKit (iOS Safari) | Chromium (Chrome Android) | Gecko (Firefox Android) |
|---|---|---|---|
| a) `<script type=speculationrules>` prefetch | `HTMLScriptElement.supports()` says **true**, but **nothing is fetched** (1 hit, the navigation). In WebKit trunk, disabled again in April 2026 for regressions; not on by default in any shipping Safari | works: 1 hit, `Sec-Purpose: prefetch`, nav commits in ~40 ms | not fetched (Mozilla: intent to prototype, May 2026) |
| b1) `<link rel=prefetch>` | `relList.supports` false, not fetched | works, reused | works, reused |
| b2) `fetch()` + `private, max-age`, **finished**, then tap | **reused only with a disk cache** (Playwright's ephemeral context has none: 2 hits; a persistent profile, as Safari and the PWA run: 1 hit) | reused | reused (memory cache, ephemeral too) |
| b3) `fetch()` **in flight** (100 ms) then tap | **not joined**: 2 hits, slower than no prefetch (666 vs ~430 ms to commit) | **joined**: 1 hit, commits when the prefetch's headers land | **not joined**: 2 hits (561 ms) |
| c) `hx-boost` + htmx `preload` | not tried (see below) | | |

**Per engine, what ships:**

- **iOS Safari / installed PWA (WebKit):** (b) with the held click. Reuse
  proven in a persistent profile; tap → FCP gain modelled at −44 to −103 ms
  (WebKit can't be throttled). Safari private browsing has no HTTP cache:
  the first held tap costs the prefetch's duration, then it turns itself off.
- **Chrome for Android (Chromium):** (b), click never held; the navigation
  joins the in-flight prefetch. −114 to −119 ms measured on slow 4G.
- **Firefox for Android (Gecko):** (b) with the held click, as WebKit (Gecko
  doesn't join in flight either). Reuse proven at a phone viewport; gain as
  the held model. Gecko does honour `<link rel=prefetch>`, but it is the same
  finished-only reuse, so it would buy nothing over `fetch()`.

**Why (b), and the shape of it.** (a) does nothing on iOS, the main target,
and nothing on Firefox. (b) works in all three once the prefetch has
finished. On slow 4G it never has: a tap's touchstart → click is ~100 ms and
a page is ≥ 400 ms RTT + transfer, so the click always lands mid-prefetch. In
Chromium that's fine (joined). In WebKit and Gecko it would be a double
download, so `prefetch.js` holds that click (`preventDefault`) until the
prefetch is in the cache, then `location.assign()`s: one request, and the
navigation is a cache hit. Chromium is recognised by `navigator.userAgentData`
(the only engine with it) and is never held. If a held navigation turns out
not to be a cache hit (an engine with no HTTP cache: Safari private browsing,
Playwright's default WebKit context), prefetching switches itself off for
the tab's session (`sessionStorage`), so that user pays the extra wait once.
Hit test: `deliveryType === 'cache'` (WebKit), `transferSize === 0`
(Chromium, Gecko) or `encodedBodySize === 0` (WebKit reports ~300 B of
cached headers as transferSize).

A layered "speculation rules where supported + JS elsewhere" adds a header or
rules file for an engine (Chromium) where the JS path already reuses in
flight, and WebKit's `supports()` returning true while doing nothing would
make the feature test lie. One mechanism everywhere; no double fetch anywhere.

**(c) rejected without building it.** Boosted tabs swap `<body>` in place:
inline scripts, Alpine init on swapped markup, Chart.js pages and history
all need re-verifying, and KAN-125's cross-document view transitions only
fire on real navigations, so every tab would lose them. (b) keeps real
navigations, so transitions compose with it.

**No hover prefetch.** A 65 ms mouseover prefetch would help desktop, which
is on fast links, and it widens the one stale case: a prefetch, then within
the 5 s a POST whose redirect lands on that URL (answered from the cache).
With touchstart/mousedown the gap to the navigation is ~100 ms.

#### Tap → FCP, slow 4G (Chromium, `npm run perf -- --nav`)

iPhone 13 viewport, 400 kbit/s / 400 ms RTT, warm static cache, median of 5.
Tap = CDP touchStart, 100 ms, touchEnd; FCP measured from the touchstart.
Off = Save-Data (prefetch.js stands down); join = Chromium as shipped; hold =
`navigator.userAgentData` hidden, so Chromium takes the WebKit/Gecko path:
the closest model of an iPhone, since WebKit can't be throttled.

| Hop | FCP off ms | join ms (Δ) | hold ms (Δ) | Navigation hit the network off/join/hold |
|---|---:|---:|---:|---:|
| Today → Table | 737 | 617 (−119) | 692 (−44) | 1/0/0 |
| Table → Cups | 734 | 618 (−116) | 659 (−76) | 1/0/0 |
| Cups → Play | 733 | 619 (−114) | 631 (−103) | 1/0/0 |
| Play → Today | 735 | 619 (−116) | 654 (−81) | 1/0/0 |

- **Chromium (Android Chrome): −114 to −119 ms**, i.e. the whole tap
  duration, on every tab.
- **Held path (model of iOS / Firefox): −44 to −103 ms.** Less than the tap,
  because a held navigation starts only once the prefetch's whole body is in
  the cache, where a joined one streams. Smaller pages gain more (Play, the
  smallest page, nearly the full tap; Table, the largest, least).
- Same request count as before: the click's navigation never reached the
  network (0) with prefetch on.

**Functional reuse in e2e** (one network fetch per navigation: the shown
document carries `private, max-age=5`, which only a prefetch response has):
WebKit/iPhone 13 in a persistent context (`prefetch.spec.js`), Chromium/Pixel
7 and Gecko 412×915+touch (`prefetch.android.spec.js`, an approximation of
Firefox Android: Playwright's Firefox has no mobile mode), desktop Chromium
with mousedown (`prefetch.desktop.spec.js`). Proven to fail when the hold is
removed (WebKit and Gecko then show a second, uncached render).

**Bytes.** `prefetch.js` is 2.6 KB on the wire (2,197 B Brotli body),
+1 request on every page; `budgets.spec.js` rows raised by exactly that
(+3 KB js/total, +1 request). Measured after: 38.7 / 7.7 / 101.2 KB, 11
requests on the dashboard; chart pages 98.8 KB JS, 12 requests.

**Not verified:** a real iPhone or Android device; Firefox for Android's
shell (only Gecko on desktop); prod's HTTP/2 (both requests share one
connection there, so the unheld double-fetch would have been cheaper, but
still a second download).

_Pending: view transitions._
