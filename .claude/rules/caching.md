---
paths:
  - "ttstats/pingpong/cache_utils.py"
  - "ttstats/pingpong/management/commands/cache_control.py"
  - "ttstats/pingpong/management/commands/warm_cache.py"
  - "ttstats/ttstats/settings/*.py"
---

# Redis cache

- Falls back to LocMemCache when `REDIS_URL` is not set.
- The leaderboard versions keys with a generation counter (`cache.incr(...)`)
  instead of deleting every filter variant.
- `CacheDebugMiddleware` (dev only) adds `X-Request-Time`, `X-Cache-Hits`,
  `X-Cache-Misses`.
- All key builders live in `cache_utils`. A private second copy of a key name is
  how an entry survives the invalidation meant to clear it.

## Every cache key carries its club

A correctly scoped query served from an entry another club warmed is still a
leak, and a cold-cache test suite never sees it. `cache_utils.club_prefix()` is
the single source of the namespace.

- The club id is a **prefix** (`club7:*`), so a targeted flush is scannable.
- **The generation counter is per club** (`club7:leaderboard_generation`); a match
  in Club A does not invalidate Club B.
- Renaming a key is a **clean break**, never an overlap -- old keys survive a
  deploy in Redis.
- A viewer with no club gets its own `noclub:` namespace.
- **A player's keys are namespaced by that player's club, not the viewer's.**
- **A cross-club match invalidates both clubs.**
- Flag off: `club_prefix()` is `""` and keys keep their pre-tenancy shape.
- **The leaderboard key** is built only in `cache_utils.leaderboard_key()`.
- **`warm_cache` scopes by explicit filter, never by faking a request.** A
  command has no user, so managers run unfiltered; a club in thread-local alone
  would write each club's key with the whole database's numbers.
- **Rating key shapes** all carry `cache_utils.RATING_INFIX` (`g2_`), kept after
  Elo's retirement so a pre-KAN-73 Elo entry still in Redis is never read:
  `leaderboard_key(scope, top_x)` = `{club}leaderboard_{gen}_g2_season{id|all}_{fmt}_{top}`,
  `player_rank_key(player)` = `{club}g2_player_rank_{pk}` (the nav reading),
  `rating_history_key(player)` = `{club}g2_rating_history_{pk}`.
- `cache_control --bump-leaderboard` bumps every club; run it after any deploy
  that changes a cached rating payload's shape.
- A club-on nav key is `club{player.club_id}:g2_player_rank_{pk}`, and the rank
  inside it is counted in that same club.

- **Live long-poll keys** (KAN-168): `live_rev_key(pk)` =
  `{club}live_rev_{pk}` (set on commit by `live_ops.publish_rev`) and
  `live_poll_key(pk, user_pk)` (the one-poll marker). `cache_is_shared()` is
  False for LocMemCache/DummyCache, which switches on the long-poll's DB fallback.

## Sessions (`cached_db` in prod)

- `prod.py` uses `cached_db` **only when `REDIS_URL` is set**. Over per-worker
  LocMemCache a session goes stale across workers and a logout survives in
  the other workers' memory.
- Session keys sit in the `default` alias under Django's own
  `django.contrib.sessions.cached_db` prefix, outside the `club*:` namespaces.
- **`cache_control --clear` (`cache.clear()`, FLUSHDB on Redis) logs nobody
  out**: the next read misses, falls back to the database and re-fills.
  Passkey challenges and the active club survive it too
  (`test_cached_db_sessions.py`).
