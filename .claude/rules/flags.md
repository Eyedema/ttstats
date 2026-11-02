---
paths:
  - "ttstats/pingpong/flags.py"
  - "ttstats/pingpong/templatetags/feature_tags.py"
  - "ttstats/ttstats/settings/base.py"
---

# Feature flags

### Feature flags fail closed

`pingpong/flags.py` is the whole mechanism: `is_enabled(name, user=None)`,
a `FEATURE_FLAGS` dict in `settings/base.py`, and `FeatureFlagOverride` rows
for per-user overrides. Deliberately **not** django-waffle -- a package for a
dict lookup plus one table is disproportionate on a 3.7GB VPS with a pinned
`requirements.txt`.

- **An undeclared flag name resolves to False and logs a warning.** It never
  raises and never fails open. A typo in a flag name must not silently enable
  a half-finished cutover, so this follows the same convention as `[x-cloak]`,
  the drawer's static `hidden` class and `push.js`.
- **Overrides win in both directions**, which makes one table both the
  staff-first rollout and the per-user kill switch. An override cannot invent
  a flag: an undeclared name is rejected before the override is ever read.
- **Resolution order is memo -> override -> settings.** The memo is opened by
  `CurrentUserMiddleware` via `flags.request_scope()` and torn down in a
  `finally`. It exists because `managers.get_queryset()` checks the `clubs`
  flag and runs constantly -- without it, every query would touch Redis.
- **Propagation delay.** Saving or deleting an override deletes its cache key,
  so with Redis the change lands on the next request. Under the LocMemCache
  fallback each process holds its own copy and the worst case is
  `flags.OVERRIDE_CACHE_TIMEOUT` (30s). Flipping the *settings* default is a
  deploy, hence a restart.
- **A dead cache or a dead DB degrades to the settings value**, never to an
  exception. A flag check is on nearly every request; it must not be able to
  500 the app.
- `TTSTATS_FLAGS_ON` (comma-separated, in `.env.prod`) turns declared flags on
  without a code change -- the intended production flip, picked up on restart.
- Templates use `{% load feature_tags %}{% feature_enabled "x" as on %}` and
  then a plain `{% if %}`, so a guarded block is **absent** from the response
  rather than hidden with CSS. A flag that only hides a control still ships it.
