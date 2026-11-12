# KAN-131: offline-tolerant reading (saved copies of pages)

Status: built and merged 2026-11-12; flag `offline_snapshots` **off** by
default. Staff-first rollout, then the real-iPhone check (T12.8 §5), then
`TTSTATS_FLAGS_ON`.

## The reversal

KAN-76/KAN-82 shipped the worker with "a stale leaderboard is worse than an
honest error": only the live scoreboard (a shell that rebuilds itself) was
cached. At the venue the 4G is mostly lie-fi, so in practice the honest error
was a white screen spinning for 20+ s, and in the standalone PWA there is no
way out of it.

KAN-131 reverses that for read-only pages, on one condition that is enforced
in code, not in convention:

> **Stale data is acceptable only when it is labelled as stale.**

The worker refuses to store or serve a copy that lacks the freshness marker or
the time placeholder, so a saved copy cannot reach the screen without the
banner and its timestamp. The banner is shown by CSS from an attribute only the
worker writes, so it needs no page JS to appear.

## Why HTML snapshots, network-first

- **HTML snapshots in Cache Storage, not IndexedDB + JSON + client rendering.**
  The server-rendered HTML the user last saw *is* the data, already filtered
  to their club and permissions. The alternative is a JSON API for every page
  plus a JS copy of every template: a rewrite.
- **Network first with a timeout, not stale-while-revalidate.** On a healthy
  network nothing is ever served from the cache, so "live" cues stay true.
- **Timeout-based, not `navigator.onLine`.** iOS reports `true` on lie-fi.
  `onLine === false` is used only to skip the wait (the fetch still goes out).

## Final values

| What | Value | Where |
|---|---|---|
| Allowlist | dashboard, leaderboard, match_list, match_detail, player_detail, head_to_head, championship_list, championship_detail, calendar, season_archive, season_archive_detail | `offline.SNAPSHOT_ALLOWLIST` |
| Navigable offline on a saved copy | allowlist + live_scoreboard | `offline.OFFLINE_NAVIGABLE` |
| `NAV_TIMEOUT_MS` (snapshot fallback) | 4000 | `sw.js` |
| `LIVE_TIMEOUT_MS` (scoreboard, unchanged) | 3000 | `sw.js` |
| `NAV_GIVE_UP_MS` (offline page when nothing saved) | 12000 | `sw.js` |
| Snapshot LRU / max age | 60 per partition / 7 days | `SNAP_MAX`, `SNAP_MAX_AGE_MS` |
| `SLOW_MS` / htmx timeout | 2500 / 8000 | `connection.js` |
| Warm-up list | Today, Table, own player page, last 3 confirmed matches (max 6) | `offline.warm_urls` |
| Warm-up rate | once per 30 min per partition; skip copies < 10 min old | `WARM_INTERVAL_MS`, `WARM_SKIP_FRESH_MS` |

All timings are the starting values from the tickets. **They have not been
tuned on a real device yet**; record the tuned values here after T12.8 §5.

## Privacy model

- Partition key: `X-TTStats-Identity` = HMAC(SECRET_KEY, user pk + active club
  pk), 16 hex chars (`offline.identity_fingerprint`, shared with KAN-82).
  Cache `ttstats-snap-<BUILD>-<identity>`.
- Only one identity's copies exist. Every page announces
  `{type:'identity', identity, snapshots}`; a different identity, an empty one
  (logged out) or `snapshots:'off'` deletes the others. Logging out sends
  `purge` before the POST, and that still happens offline (KAN-146), because
  wiping a shared phone needs no server.
- A response is stored only if its identity equals the current partition,
  re-checked after the body is read, so a store racing an identity change is
  dropped.
- What sits on the phone: the HTML of allowlisted pages the user opened (or
  that were warmed), including that session's CSRF token. A saved copy is
  read-only (every submit, htmx request and non-navigable link is blocked),
  so the stale token is never used.
- `BUILD` is in the cache name, so every deploy empties the snapshots. That's
  deliberate: a copy references hashed static files that the new build's
  `activate` deletes.

## Warm-up cost

At most 6 GETs per flag-on user per 30 min, sequential, only on an online,
non-Save-Data connection, never from a saved copy. Building the URL list adds
**one** query to the page that renders it (pinned by
`test_offline_warm.py::test_the_list_itself_is_one_query`). Each warm GET costs
the same as a normal visit to that page. Warm requests are sampled separately
in Sentry (`make_traces_sampler`) and never consume flash messages. A real
production measurement is still to do after rollout.

## Tests

- pytest: `test_offline_snapshots.py` (decorator, allowlist sweep, markers),
  `test_offline_page.py`, `test_offline_warm.py`, `test_sentry.py`,
  `test_prefetch.py`.
- JS: `tests/js/sw_snapshots.test.mjs`, `sw_offline_page.test.mjs`,
  `sw_warm.test.mjs`, `snapshot_mode.test.mjs`, `snapshot_warm.test.mjs`,
  `offline_page.test.mjs`, `connection.test.mjs`.
- e2e (KAN-138), all through `tests/e2e/offline-proxy.js` because
  `context.setOffline()` cannot exercise a WebKit worker:
  `offline-snapshots.pwa.spec.js` (cases 1, 2, 4, 5, 6, 7, 8, 9, 14),
  `offline-snapshot-banner{,.dark,.nojs}.spec.js` (13 and layout),
  `offline-read-only{.pwa,.nojs}.spec.js` (10, 11, 5's recovery),
  `offline-page{.pwa,.nojs}.spec.js` (3), `offline-warm.pwa.spec.js`,
  `connection{,.nojs}.spec.js` (12).

## Still open

- **T12.8 §5, real device:** installed PWA in airplane mode (Today, Table, a
  match); Network Link Conditioner "Very Bad Network" to tune
  `NAV_TIMEOUT_MS`; log out and in as someone else; leave the PWA closed for a
  day and reopen it offline (iOS eviction). Also decide whether the "Slow
  connection" hint is noise.
- No `/healthz` yet (KAN-109): the back-online probe is a `HEAD` of the
  manifest. Switch to `/healthz` when it exists.
