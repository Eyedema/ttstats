---
paths:
  - "ttstats/pingpong/flags.py"
  - "ttstats/pingpong/templatetags/feature_tags.py"
  - "ttstats/ttstats/settings/base.py"
---

# Feature flags fail closed

`pingpong/flags.py`: `is_enabled(name, user=None)`, a `FEATURE_FLAGS` dict in
`settings/base.py`, and `FeatureFlagOverride` rows for per-user overrides.
Deliberately not django-waffle.

- **An undeclared flag resolves to False and logs a warning.** Never raises,
  never fails open.
- **Overrides win in both directions** (staff-first rollout and per-user kill
  switch). An override cannot invent an undeclared flag.
- **Resolution order is memo -> override -> settings.** The memo is opened by
  `CurrentUserMiddleware` via `flags.request_scope()` and torn down in a
  `finally`; managers check `clubs` constantly.
- Saving or deleting an override deletes its cache key. Under LocMemCache the
  worst-case delay is `flags.OVERRIDE_CACHE_TIMEOUT` (30s). Changing the settings
  default needs a restart.
- **A dead cache or DB degrades to the settings value**, never an exception.
- `TTSTATS_FLAGS_ON` (comma-separated, in `.env.prod`) turns declared flags on
  without a code change.
- Templates: `{% load feature_tags %}{% feature_enabled "x" as on %}` then
  `{% if on %}`, so a guarded block is **absent** from the response, not hidden.
