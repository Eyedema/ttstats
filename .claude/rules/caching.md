---
paths:
  - "ttstats/pingpong/cache_utils.py"
  - "ttstats/pingpong/management/commands/cache_control.py"
  - "ttstats/pingpong/management/commands/warm_cache.py"
  - "ttstats/ttstats/settings/*.py"
---

# Redis cache

### Redis Cache
- Falls back to LocMemCache when `REDIS_URL` is not set
- Leaderboard uses generation counter pattern: `cache.incr('leaderboard_generation')` versions keys instead of deleting all filter variants
- `CacheDebugMiddleware` (dev only) adds `X-Request-Time`, `X-Cache-Hits`, `X-Cache-Misses` headers

#### Every cache key carries its club

**A correctly-scoped query served from a cache entry another club warmed is
still a leak** -- and it is the one kind of leak a suite running against a cold
cache will never see. `cache_utils.club_prefix()` is the single source of the
namespace.

- The club id is a key **prefix**, not a suffix, so `cache_control --stats` can
  group by namespace and a targeted flush stays possible: `club7:*` is a
  scannable pattern, `*:club7` is not.
- **The generation counter is per club** (`club7:leaderboard_generation`). The
  pattern itself is kept -- versioning keys beats deleting the cross-product of
  every filter variant -- but a match in Club A no longer invalidates Club B's.
- The new key name is a **clean break** from `leaderboard_generation`, never an
  overlap. A deploy leaves old keys in Redis, and an ambiguous key is how one
  club is served another's payload for a whole TTL.
- **A viewer with no club gets its own `noclub:` namespace**, which collides
  with neither a real club nor the flag-off (empty) namespace.
- **A player's keys are namespaced by that player's club, not the viewer's.**
  They summarise that player; a viewer-dependent key means one entry per viewer
  and a hit rate near zero.
- **A cross-club match invalidates both clubs.** They are representable by
  design, and half-invalidating leaves one side reading a stale table.
- With the flag off, `club_prefix()` is `""` and every key is byte-identical to
  its pre-tenancy shape.
- **`warm_cache` scopes by explicit filter, never by faking a request.** The
  managers' contract is "no user means unfiltered" and a command has no user, so
  a club in thread-local context alone leaves every query unscoped and writes
  each club's key with the whole database's numbers. A populated cache that is
  wrong is worse than a cold one.
