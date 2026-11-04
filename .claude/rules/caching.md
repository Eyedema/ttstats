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
- **The seasons leaderboard key** is
  `{club}leaderboard_{gen}_season{id|all}_{format}_{top_x}`, built only in
  `_leaderboard_cache_key()`; with `seasons` off the legacy key is unchanged.
- **`warm_cache` scopes by explicit filter, never by faking a request.** A
  command has no user, so managers run unfiltered; a club in thread-local alone
  would write each club's key with the whole database's numbers.
