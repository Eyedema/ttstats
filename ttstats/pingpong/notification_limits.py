"""Per-player daily push limits (KAN-38 / T3.7).

Notification volume is the fastest way to get the app uninstalled, so every
push that respects preferences also passes ``allow()`` (called from
``push.send_to_user``, the one path every ``notify_*`` uses). Two counters per
user per UTC day:

* **per kind**, for kinds listed in ``NOTIFICATION_DAILY_LIMITS``;
* **overall**, across all kinds, capped by ``NOTIFICATION_DAILY_CEILING``.

Defaults: only ``season_ended`` has a per-kind limit (1/day), and the ceiling
is 50/day, far above what a long club night of confirmation and result pushes
produces, so existing kinds behave exactly as before. Both settings can be
overridden in Django settings.

The counters live in the cache (Redis in production) with a TTL to the end of
the UTC day; the date is also in the key, so "a day" is a UTC day and DST
never matters. **The limiter fails closed**: if the cache errors, the push is
not sent. A suppressed push is simply "not delivered", so the caller's email
fallback still applies.
"""

import logging
from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache

from . import cache_utils

logger = logging.getLogger(__name__)

DEFAULT_DAILY_LIMITS = {'season_ended': 1}
DEFAULT_DAILY_CEILING = 50


def seconds_until_utc_midnight(now):
    now = now.astimezone(dt_timezone.utc)
    midnight = datetime.combine(now.date() + timedelta(days=1), time(), dt_timezone.utc)
    return max(1, int((midnight - now).total_seconds()))


def _bump(key, ttl):
    """Increment a day counter, creating it with the day's TTL. Returns the
    new value. Raises on any cache error; the caller fails closed."""
    cache.add(key, 0, ttl)
    return cache.incr(key)


def allow(user_id, kind, *, now=None):
    """True if one more push of ``kind`` to this user fits today's limits.

    Counts the push when it returns True. Never raises.
    """
    now = now or datetime.now(dt_timezone.utc)
    day = now.astimezone(dt_timezone.utc).date()
    ttl = seconds_until_utc_midnight(now)
    kind = str(getattr(kind, 'value', kind))
    limits = getattr(settings, 'NOTIFICATION_DAILY_LIMITS', DEFAULT_DAILY_LIMITS)
    ceiling = getattr(settings, 'NOTIFICATION_DAILY_CEILING', DEFAULT_DAILY_CEILING)

    try:
        kind_limit = limits.get(kind)
        if kind_limit is not None:
            sent = _bump(cache_utils.notification_count_key(user_id, kind, day), ttl)
            if sent > kind_limit:
                logger.info(
                    "Push suppressed for user %s: %s per-kind daily limit (%s) reached",
                    user_id, kind, kind_limit,
                )
                return False

        total = _bump(cache_utils.notification_count_key(user_id, 'all', day), ttl)
        if total > ceiling:
            logger.info(
                "Push suppressed for user %s: %s hit the overall daily ceiling (%s)",
                user_id, kind, ceiling,
            )
            return False
    except Exception:
        logger.warning(
            "Push suppressed for user %s (%s): rate-limit cache unavailable, failing closed",
            user_id, kind, exc_info=True,
        )
        return False

    return True
