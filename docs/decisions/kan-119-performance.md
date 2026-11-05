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
- Only gzip: `brotli` is not installed, so WhiteNoise writes no `.br` files.

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
