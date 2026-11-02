---
paths:
  - "ttstats/ttstats/sentry.py"
  - "ttstats/pingpong/observability.py"
  - "ttstats/ttstats/settings/prod.py"
  - "ttstats/pingpong/managers.py"
---

# Observability: logs, tenancy canary, Sentry

## Request context on every log line

- **Every prod log record carries `path`, `user` and `club` ids**
  (`observability.RequestContextFilter`, wired into `prod.py`'s `LOGGING`).
  **Ids only, never identities** -- no usernames, emails, tokens or push
  endpoints. The query string is never logged.
- The filter **always sets its three attributes** (so formatters can't fail on a
  management-command record) and swallows every error.

## The tenancy canary reports, it does not enforce

- `check_queryset_scope` never raises -- not on a leak, a sliced queryset or a
  bad lookup. The manager filter is the security boundary.
- **Catches:** a queryset reaching a viewer unfiltered while they had a club.
  **Cannot catch:** a filter against the *wrong* club; only the two-tenant
  isolation suite covers that.
- **`TENANCY_CANARY_SAMPLE_RATE` is checked before the `tenancy_canary` flag**,
  because the flag lookup can cost a query inside `get_queryset()`.
  `test_match_list_performance.py` guards this.
- Intended production sampling is 1-2%; 100% is far too expensive.
- It lives in `ScopedManagerMixin._canary()` and `scope_to_active_club()`.

## Sentry is the error inbox; the logs stay the record

`ttstats/sentry.py` is the transport; `pingpong/observability.py` the vocabulary.
Init runs in `settings/prod.py` at **import time**, to catch boot failures.

- **Off unless `SENTRY_DSN` is set.** Wired in `prod.py` **only** --
  `settings/test.py` imports `dev.py`, so initialising there could ship test
  events to the real project.
- **URLs are bearer credentials.** `scrub_url()` redacts
  `/pingpong/join/<token>/` and `/pingpong/verify-email/<token>/` in request URLs
  and breadcrumbs. **A new token-bearing route must be added to
  `TOKEN_PATH_PREFIXES` in the same commit as `urls.py`.** Query strings are
  dropped wholesale.
- `send_default_pii=False`, `max_request_body_size="never"`; `before_send`
  attaches `user_id` / `club_id` tags.
- **`before_send` reads `_thread_locals` directly, never `get_current_club()`**,
  which could issue a query from inside an exception handler.
- **Telemetry never raises**; `before_send` returns the event unmodified on error.
- `LoggingIntegration`: INFO becomes breadcrumbs, ERROR (including the
  `pingpong.tenancy` canary) becomes an event.
- **4xx are not reported by default.** `SENTRY_CAPTURE_4XX` opts statuses in via
  `SentryHttpErrorMiddleware` (directly below WhiteNoise, fingerprinted by
  status + route). 403 is the one worth setting.
- `DisallowedHost` is in `IGNORED_ERRORS`; `SuspiciousOperation` deliberately isn't.
- **No browser SDK.** Adding one needs `connect-src` in `prod.py` and `PROD_CSP`.
