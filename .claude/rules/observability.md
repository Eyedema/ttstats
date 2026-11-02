---
paths:
  - "ttstats/ttstats/sentry.py"
  - "ttstats/pingpong/observability.py"
  - "ttstats/ttstats/settings/prod.py"
  - "ttstats/pingpong/managers.py"
---

# Observability: logs, tenancy canary, Sentry

### Tenancy observability: context on every log line, plus a canary

Production ran one console handler at INFO with no request context. For a
change whose failure mode is *silent*, that means the first signal would be a
user asking "who is Marco?".

- **Every prod log record carries `path`, `user` and `club` ids**
  (`observability.RequestContextFilter`, wired into `prod.py`'s `LOGGING`).
  **Ids only, never identities** -- no usernames, emails, tokens or push
  endpoints. A log aggregator is a copy of your data in someone else's system,
  and an id answers "which tenant" while a username does not. The query string
  is never logged; the email-verification flow puts a token in a URL.
- The filter **always sets its three attributes**, so a formatter naming them
  cannot blow up on a record from a management command or a startup exception.
  It also swallows everything: logging must never be the thing that fails.
- **The canary reports, it does not enforce.** The manager filter is the
  security boundary. Turning a silent leak into a loud outage is the worse
  trade, so `check_queryset_scope` never raises -- not on a leak, not on a
  sliced queryset, not on a bad lookup.
- **What it catches:** a queryset reaching a viewer *unfiltered* while that
  viewer had a club -- a flag misread, a stale memo, an `all_objects` path that
  skipped `scope_to_active_club`. **What it cannot:** a filter applied against
  the *wrong* club, since it compares against the same `get_current_club()` the
  filter used. Only Q1.11, asserting from outside with two real tenants, covers
  that.
- **Two gates, and the order is load-bearing.** `TENANCY_CANARY_SAMPLE_RATE`
  is checked *before* the `tenancy_canary` flag, because a flag lookup for an
  authenticated user costs a DB query on first use in a request and this runs
  inside `get_queryset()`. Flag-first added that query to every page for a
  feature that is off by default -- `test_match_list_performance.py` caught it.
- **Measured overhead:** +30-40% at 100% sampling, **+2-3% at 1-2%**, which is
  the intended production setting and inside the 5% budget. Sampling is not a
  nicety here; it is what makes the canary affordable.
- It lives in `ScopedManagerMixin._canary()` and in `scope_to_active_club()` --
  one place to arm and disarm, which was half the point of the mixin.

### Sentry is the error inbox; the logs stay the record

`ttstats/sentry.py` is the transport, the way `push.py` is for notifications.
`pingpong/observability.py` remains the vocabulary. Init happens in
`settings/prod.py` at **import time** -- an AppConfig.ready or a middleware is
too late to catch an exception raised while Django is still booting, which is
the outage nobody is watching a console for.

- **Off unless `SENTRY_DSN` is set.** Same contract as VAPID: unset (dev, CI,
  the suite, a fresh clone) every send is a no-op and the console handler
  behaves exactly as before. Deliberately wired in `prod.py` **only** --
  `settings/test.py` imports from `dev.py`, so initialising there would let a
  `SENTRY_DSN` exported in a shell ship test events to the real project, the
  same footgun `DATABASE_URL` is already guarded against.
- **URLs in this app are bearer credentials.** `/pingpong/join/<token>/` and
  `/pingpong/verify-email/<token>/` carry secrets in the *path*, so dropping
  the query string is not enough. `scrub_url()` redacts them in the request URL
  and in every breadcrumb, and `TOKEN_PATH_PREFIXES` is the entire defence --
  **a new token-bearing route must be added there in the same commit it is
  added to `urls.py`.** The query string goes wholesale rather than
  per-parameter: an allowlist's forgotten entry is the one that leaks.
- **Ids only, never identities**, matching `prod.py`'s log formatter.
  `send_default_pii=False`, `max_request_body_size="never"`, and `before_send`
  attaches `user_id` / `club_id` tags -- so an issue answers "which tenant"
  without putting a username or an email in someone else's system.
- **`before_send` reads `_thread_locals` directly and never calls
  `get_current_club()`.** That resolver is lazy: on a request that touched no
  scoped manager it would issue a membership query *from inside an exception
  handler*, quite possibly on the connection that just failed, while
  re-entering the logging stack trying to report it. An unresolved club is
  worth no tag.
- **Telemetry never raises.** Every hook is wrapped and swallows; `before_send`
  returns the event unmodified rather than dropping it. Same trade as the
  tenancy canary -- a monitor that can 500 the app is worse than no monitor.
- **The `LoggingIntegration` is what makes the leak canary visible.**
  `pingpong.tenancy` logs at ERROR and raises nothing by design; before this it
  went to a console handler on a VPS nobody tails. INFO becomes breadcrumbs,
  ERROR becomes an event.
- **404s and 400s are deliberately NOT reported.** Django raises nothing for a
  4xx it produced itself, so none reach Sentry by default -- and that is
  correct. A 404 is what the internet does to every public host; routing
  scanner traffic into the same inbox as a 500 does not add coverage, it buries
  the one issue meaning a real user saw a broken page. `SENTRY_CAPTURE_4XX`
  (empty by default) opts specific statuses in via
  `SentryHttpErrorMiddleware`, which sits directly below WhiteNoise so a
  missing-asset 404 cannot drown out an application one, and fingerprints by
  status + resolved route so one broken URL is one issue. **403 is the value
  worth setting**: no template renders a control that produces one, so it means
  a forged POST or a permissions bug.
- `DisallowedHost` is in `IGNORED_ERRORS` -- it is `ALLOWED_HOSTS` working, and
  it would be the loudest issue in the project within a day.
  `SuspiciousOperation` is deliberately *not*, since that is the tampering one.
- **No browser SDK.** Adding one means `connect-src` in `prod.py`'s CSP *and*
  in `PROD_CSP` in `tests/e2e/helpers.js`, or reports are silently blocked in
  production and nowhere else.
