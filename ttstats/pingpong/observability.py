"""Tenancy observability: request context on log records, and a leak canary.

A tenancy leak is silent. It does not raise, does not 500 and does not appear
in any log -- a user simply sees a name that is not theirs. Production runs one
console handler at INFO with no request context, so without this the first
signal would be somebody asking "who is Marco?".

**The canary is a check, not an enforcement mechanism.** The manager filter is
the security boundary; this only reports on whether it held. It must never
raise in production, because turning a leak into an outage is a strictly worse
failure -- so every path here is wrapped and every failure is swallowed.

What the canary can and cannot catch is worth being precise about, since a
canary trusted beyond its reach is worse than none:

* **Catches** a queryset that reached a viewer *unfiltered* while that viewer
  had an active club -- the fail-open case. That covers a flag misread, a
  memo left stale across requests, an `all_objects` path that skipped
  `scope_to_active_club`, and a manager whose scoping was accidentally
  short-circuited.
* **Does not catch** a filter applied against the *wrong* club, because the
  check compares against the same `get_current_club()` the filter used. Nothing
  inside one request can detect that; Q1.11 is what covers it, by asserting
  from the outside with two real tenants.
"""
import logging
import random

from django.conf import settings

logger = logging.getLogger("pingpong.tenancy")

# Rows examined per canary check. The point is to notice a leak, not to
# enumerate it, and an unbounded scan on the leaderboard's queryset would cost
# more than the page it is watching.
CANARY_SAMPLE_ROWS = 25


def _sample_rate():
    return getattr(settings, "TENANCY_CANARY_SAMPLE_RATE", 0.0)


def canary_enabled(user=None):
    """Is the canary armed for this request?

    Two gates, both of which must pass: a sample rate, and the
    `tenancy_canary` feature flag. The flag makes it killable without a deploy.

    **The rate is checked first, and the order is load-bearing.** A flag lookup
    for an authenticated user costs a database query on its first use in a
    request, and this runs inside `get_queryset()` -- the hottest path in the
    app. Testing the flag first added that query to every page for a feature
    that is off by default, which the match-list query-count test caught.
    Checking the rate first means the default configuration costs one float
    comparison and nothing else.
    """
    rate = _sample_rate()
    if rate <= 0.0:
        return False
    if rate < 1.0 and random.random() >= rate:
        return False

    from pingpong import flags

    return flags.is_enabled("tenancy_canary", user)


def check_queryset_scope(queryset, club, club_lookup, source):
    """Log loudly if `queryset` can reach rows outside `club`. Never raises.

    Bounded to `CANARY_SAMPLE_ROWS` and executed as a single `EXISTS`-shaped
    query, so the cost is one indexed lookup on a slice rather than a scan.
    """
    if club is None:
        return

    try:
        offenders = (
            queryset.exclude(**{club_lookup: club})
            .values_list("pk", flat=True)[:CANARY_SAMPLE_ROWS]
        )
        leaked = list(offenders)
    except Exception:
        # A canary that breaks a page is worse than no canary. This includes
        # querysets that cannot be re-filtered (already sliced, .none(), etc.).
        logger.debug("Tenancy canary could not evaluate %s", source, exc_info=True)
        return

    if leaked:
        logger.error(
            "TENANCY CANARY: %s returned %d row(s) outside club %s "
            "(sampled pks: %s). The filter did not hold -- this is a leak.",
            source,
            len(leaked),
            club.pk,
            leaked,
        )


class RequestContextFilter(logging.Filter):
    """Stamp every log record with the request path, user id and club id.

    Without these a production log line cannot be traced to a tenant, which is
    the one thing a tenancy incident needs from its logs.

    **Ids only, never identities.** No email addresses, no usernames, no
    tokens, no push endpoints -- a log aggregator is a copy of your data in
    somebody else's system, and an id is enough to answer "which tenant" while
    a username is not.

    Every attribute is always set, so a formatter naming them cannot blow up on
    a record emitted outside a request (a management command, a cron job, or an
    exception during startup).
    """

    def filter(self, record):
        record.request_path = "-"
        record.user_id = "-"
        record.club_id = "-"

        try:
            from ttstats.middleware import _thread_locals

            path = getattr(_thread_locals, "request_path", None)
            if path:
                record.request_path = path

            user = getattr(_thread_locals, "user", None)
            if user is not None and getattr(user, "is_authenticated", False):
                record.user_id = str(user.pk)

            # Deliberately reads the *memo* rather than calling
            # get_current_club(): resolving a club during logging would issue a
            # query from inside a log handler, and a log line is not a reason to
            # touch the database.
            club = getattr(_thread_locals, "club", None)
            if club is not None:
                record.club_id = str(club.pk)
        except Exception:
            # Logging must never be the thing that fails.
            pass

        return True
