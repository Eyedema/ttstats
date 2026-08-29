"""Sentry wiring: initialisation, scrubbing, and the 4xx opt-in.

This is the *transport* half of observability. `pingpong/observability.py`
stays the vocabulary -- it decides what a tenancy leak sounds like; this
decides where any of it ends up. The split is the same one `push.py` and
`notifications.py` already use, and for the same reason: the thing that talks
to a third party should be replaceable without touching what the app has to
say.

**Sentry is off unless `SENTRY_DSN` is set.** Unset -- dev, CI, a fresh clone,
the test suite -- `init_sentry()` returns False and the app logs to the console
exactly as it did before. That is the same contract as VAPID/web push, and it
is what keeps `settings/test.py` honest: no test can accidentally ship an event
to production's project.

Three rules govern everything below.

**Telemetry must never be the thing that fails.** Every hook here is wrapped
and swallows its own errors, and `before_send` returns the event unchanged
rather than dropping it if scrubbing raises. A monitoring tool that can 500 the
app is a liability, not a monitor -- the same reasoning as the canary in
`observability.py`, which reports and never enforces.

**Ids only, never identities.** `send_default_pii` is False and stays False.
The tags attached are `user_id` and `club_id`, matching the log formatter in
`prod.py`: a log aggregator is a copy of your data in someone else's system,
and an id answers "which tenant" while an email address does not.

**URLs in this app carry bearer credentials.** `/pingpong/join/<token>/` is a
club invite and `/pingpong/verify-email/<token>/` is an account takeover; both
are documented as never-logged. An error report includes the request URL and
every breadcrumb URL, so `_scrub_url` redacts those segments before an event
leaves the process. Adding a new token-bearing route means adding it to
`TOKEN_PATH_PREFIXES` -- there is no generic defence here, and a token in a
third-party issue tracker is a token that leaked.
"""
import logging
import os
import re

logger = logging.getLogger(__name__)

# Path segments that follow one of these are credentials, not identifiers, and
# are replaced with [redacted] before an event is sent. See the module
# docstring: this list is the whole defence, so a new token-bearing URL has to
# be added here at the same time it is added to urls.py.
TOKEN_PATH_PREFIXES = ("join", "verify-email")

_TOKEN_PATH_RE = re.compile(
    r"(/(?:%s)/)[^/?#]+" % "|".join(re.escape(p) for p in TOKEN_PATH_PREFIXES)
)

# Exceptions that are traffic, not defects.
#
# `Http404` Django's integration already drops; naming it is documentation, not
# behaviour. `DisallowedHost` is what every scanner that hits the VPS by IP
# raises -- it is a correctly configured ALLOWED_HOSTS doing its job, and left
# in it would be the loudest issue in the project within a day.
#
# `SuspiciousOperation` is deliberately NOT here. It is the parent of the
# tampering cases (bad signatures, oversized uploads) and those are exactly
# what this is for.
IGNORED_ERRORS = (
    "django.http.Http404",
    "django.core.exceptions.DisallowedHost",
)


def _env_float(name, default):
    """Read a sample rate, degrading to the default rather than raising.

    A malformed rate in `.env.prod` must not stop Gunicorn from booting: the
    failure mode of a bad monitoring config should be "no monitoring", never
    "no app".
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Ignoring malformed %s=%r; using %s", name, raw, default)
        return default


def capture_4xx_statuses():
    """The statuses `SENTRY_CAPTURE_4XX` asks for, as a set of ints.

    Read in one place so `prod.py` (deciding whether the middleware is worth
    installing at all) and the middleware itself cannot disagree about it.
    """
    name = "SENTRY_CAPTURE_4XX"
    raw = os.environ.get(name, "")
    statuses = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            statuses.add(int(part))
        except ValueError:
            logger.warning("Ignoring non-numeric status %r in %s", part, name)
    return statuses


def scrub_url(url):
    """Redact bearer tokens in a path, and drop the query string entirely.

    The query string goes wholesale rather than per-parameter: an allowlist
    would have to be maintained against every future `?next=`, `?token=` or
    signed link, and the one that gets forgotten is the one that leaks. Nothing
    in this app puts information in a query string that an issue report needs.
    """
    if not isinstance(url, str):
        return url
    url = url.split("?", 1)[0].split("#", 1)[0]
    return _TOKEN_PATH_RE.sub(r"\1[redacted]", url)


def _request_context():
    """The current viewer and club ids, read WITHOUT resolving anything.

    This reads `_thread_locals` directly instead of calling
    `get_current_club()`, and the difference matters. `get_current_club()` is
    lazy: on a request that never touched a scoped manager it would issue a
    membership query -- from inside an exception handler, quite possibly with
    the database connection that just failed, and re-entering the logging
    stack that is trying to report the failure. An already-memoized value is
    free; an unresolved one is not worth a tag.
    """
    try:
        from ttstats.middleware import _thread_locals

        user = getattr(_thread_locals, "user", None)
        club = getattr(_thread_locals, "club", None)
        user_id = getattr(user, "pk", None) if user is not None else None
        club_id = getattr(club, "pk", None) if club is not None else None
        return user_id, club_id
    except Exception:  # pragma: no cover - defensive; telemetry never raises
        return None, None


def before_send(event, hint):
    """Scrub credentials and attach tenancy ids. Never drops an event on error."""
    try:
        request = event.get("request")
        if isinstance(request, dict):
            if "url" in request:
                request["url"] = scrub_url(request["url"])
            # Set to "" rather than deleted: Sentry renders an absent key and an
            # empty one identically, and assigning cannot fail on a frozen dict.
            request["query_string"] = ""
            headers = request.get("headers")
            if isinstance(headers, dict):
                headers.pop("Referer", None)
                headers.pop("Cookie", None)

        for crumb in event.get("breadcrumbs", {}).get("values", []) or []:
            data = crumb.get("data")
            if isinstance(data, dict) and "url" in data:
                data["url"] = scrub_url(data["url"])

        user_id, club_id = _request_context()
        tags = event.setdefault("tags", {})
        if user_id is not None:
            tags.setdefault("user_id", str(user_id))
        if club_id is not None:
            tags.setdefault("club_id", str(club_id))
    except Exception:  # pragma: no cover - defensive; telemetry never raises
        logger.debug("Sentry before_send failed; sending event unmodified", exc_info=True)
    return event


def init_sentry(environment="production", release=None):
    """Start the SDK if a DSN is configured. Returns True if it did.

    Called from `settings/prod.py` at import time, which is the only place
    early enough to catch an exception raised while Django is still booting.
    """
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        # A DSN set with the package missing is a misconfiguration worth a
        # line in the log, and nothing more. It is not worth refusing to boot.
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed")
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", environment),
        # CI pins WEB_IMAGE to a commit SHA, so the same value identifies the
        # running image and the code that built it. Without a release, every
        # regression looks like it has always been there.
        release=release or os.environ.get("SENTRY_RELEASE") or None,
        integrations=[
            # Middleware and signal spans are off: this app has 15 middleware
            # and a busy signal pipeline, and per-span overhead on a 3.7GB VPS
            # buys detail nobody reads. View, template and DB spans stay.
            DjangoIntegration(middleware_spans=False, signals_spans=False),
            # INFO becomes breadcrumbs, ERROR becomes an event. That single
            # line is what puts `pingpong.tenancy` -- the leak canary, which
            # logs at ERROR and raises nothing -- in front of a human. Before
            # this it went to a console handler on a VPS that nobody tails.
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        # Never flipped on. The tags in `before_send` carry the ids that an
        # incident actually needs; usernames, emails and IPs are not among them.
        send_default_pii=False,
        # Request bodies are match scores and, on two routes, a password.
        max_request_body_size="never",
        traces_sample_rate=_env_float("SENTRY_TRACES_SAMPLE_RATE", 0.0),
        ignore_errors=list(IGNORED_ERRORS),
        before_send=before_send,
    )
    return True


class SentryHttpErrorMiddleware:
    """Report 4xx responses whose status is listed in `SENTRY_CAPTURE_4XX`.

    **Default empty, and 404 is deliberately not a suggested value.** Django
    never raises for a 4xx it produced itself, so nothing above reaches Sentry
    for them -- which is mostly correct. A 404 is what a scanner probing
    /wp-login.php gets, and a 400 is usually a malformed request from the same
    source. Routing those into the same inbox as a 500 does not add coverage;
    it buries the one issue that means a user saw a broken page under a
    thousand that mean the internet exists.

    What this exists for is the narrow, high-signal case: a status the *app*
    only ever returns deliberately. `403` is the one worth turning on here --
    `ClubAdminRequiredMixin` returns it for a POST the templates never render a
    control for, so a burst of them is somebody probing the club admin
    endpoints, and a steady trickle is a permissions bug.

    Events are fingerprinted by status and resolved route, so one broken URL is
    one issue rather than one per visitor.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.statuses = capture_4xx_statuses()

    def __call__(self, request):
        response = self.get_response(request)
        if response.status_code in self.statuses:
            self._capture(request, response)
        return response

    def _capture(self, request, response):
        try:
            import sentry_sdk

            route = getattr(getattr(request, "resolver_match", None), "route", None)
            route = route or scrub_url(request.path)
            with sentry_sdk.new_scope() as scope:
                scope.fingerprint = ["http", str(response.status_code), route]
                scope.set_tag("status_code", response.status_code)
                sentry_sdk.capture_message(
                    f"HTTP {response.status_code} {route}", level="warning"
                )
        except Exception:  # pragma: no cover - defensive; telemetry never raises
            logger.debug("Failed to report %s to Sentry", response.status_code, exc_info=True)
