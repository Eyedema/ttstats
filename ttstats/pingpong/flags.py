"""Minimal, dependency-free feature flags.

Every risky change in this backlog needs to reach production in the *off*
position and be enableable for a subset of users without a deploy. That is one
dictionary lookup plus an override table, so this is not django-waffle: the app
runs on a 3.7GB VPS and `requirements.txt` is tightly pinned (`pywebpush<2.4`
against a `cryptography` conflict). A package for this would be disproportionate.

**Fail closed.** A flag nobody declared is off, and an unknown name logs a
warning rather than raising -- same convention as `[x-cloak]`, the mobile
drawer's static `hidden` class and `push.js`: the default state is off, and
something must actively turn it on. Failing *open* here would mean a typo in a
flag name silently enabling a half-finished tenancy cutover.

Resolution order, cheapest first:

1. A per-request memo (see ``request_scope``), so `managers.get_queryset()` --
   which runs constantly -- resolves a flag once per request, not once per query.
2. A per-user override row, cached. Overrides win in **both** directions, so the
   same table is the staff-only rollout and the per-user kill switch.
3. The ``FEATURE_FLAGS`` dict in settings.

This module is importable without the Django app registry being ready: the
override model is imported lazily inside the lookup, and a DB or cache failure
degrades to the settings value rather than propagating. A migration or a
management command therefore gets the settings-only answer, which is correct --
it has no user to override for.

**Propagation delay.** Saving or deleting an override deletes its cache key
(see `signals.py`), so with Redis shared across processes the change is visible
on the next request. Under the LocMemCache fallback each process holds its own
copy, so the worst case is ``OVERRIDE_CACHE_TIMEOUT`` (30s) before every process
agrees. Flipping the *settings* default is a deploy and therefore a restart.
"""
import contextlib
import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Short by design. A flag flip during a rollout should take effect in seconds,
# and the row it caches is one boolean -- there is nothing to gain by holding it.
OVERRIDE_CACHE_TIMEOUT = 30

# Cached in place of a missing override row, so "this user has no override" is
# itself a cache hit rather than a repeated query. `None` cannot be used: it is
# indistinguishable from a cache miss.
_NO_OVERRIDE = "__none__"

# Thread-local memo, shared with the request lifecycle by `request_scope()`.
# Uses the same `local()` the row-level-security user context lives in, so
# there is one place where per-request state is set up and torn down.
from ttstats.middleware import _thread_locals  # noqa: E402


def all_flags():
    """Every declared flag and its settings-level default."""
    return dict(getattr(settings, "FEATURE_FLAGS", {}))


def _request_memo():
    """The current request's memo dict, or None outside a request scope."""
    return getattr(_thread_locals, "flag_memo", None)


@contextlib.contextmanager
def request_scope():
    """Memoize flag resolution for the duration of a request.

    Cleared in a ``finally`` so an exception mid-request cannot leave a stale
    memo for whatever the thread serves next -- the same leak the user
    thread-local already has to guard against.
    """
    _thread_locals.flag_memo = {}
    try:
        yield
    finally:
        if hasattr(_thread_locals, "flag_memo"):
            del _thread_locals.flag_memo


def _override_cache_key(name, user_pk):
    return f"feature_flag_override:{name}:{user_pk}"


def _lookup_override(name, user_pk):
    """The user's override for `name`, or None. Never raises.

    A dead cache falls through to the DB; a dead DB falls through to None,
    which the caller reads as "use the settings default".
    """
    key = _override_cache_key(name, user_pk)
    try:
        cached = cache.get(key)
    except Exception:
        logger.warning("Feature-flag cache unavailable; falling back to settings")
        return None

    if cached is not None:
        return None if cached == _NO_OVERRIDE else bool(cached)

    try:
        from .models import FeatureFlagOverride

        value = (
            FeatureFlagOverride.objects.filter(name=name, user_id=user_pk)
            .values_list("enabled", flat=True)
            .first()
        )
    except Exception:
        logger.warning("Feature-flag override lookup failed for %r", name, exc_info=True)
        return None

    with contextlib.suppress(Exception):
        cache.set(
            key,
            _NO_OVERRIDE if value is None else value,
            OVERRIDE_CACHE_TIMEOUT,
        )
    return value


def is_enabled(name, user=None):
    """Is feature `name` on, for `user` (or globally when no user is given)?

    Returns a plain bool. Unknown flags return False and log a warning.
    """
    declared = all_flags()
    if name not in declared:
        logger.warning("Unknown feature flag %r checked; treating as off", name)
        return False

    user_pk = getattr(user, "pk", None) if getattr(user, "is_authenticated", False) else None

    memo = _request_memo()
    memo_key = (name, user_pk)
    if memo is not None and memo_key in memo:
        return memo[memo_key]

    result = bool(declared[name])
    if user_pk is not None:
        override = _lookup_override(name, user_pk)
        if override is not None:
            result = bool(override)

    if memo is not None:
        memo[memo_key] = result
    return result


def invalidate_override(name, user_pk):
    """Drop a cached override. Called from the override model's signals."""
    with contextlib.suppress(Exception):
        cache.delete(_override_cache_key(name, user_pk))
    memo = _request_memo()
    if memo is not None:
        memo.pop((name, user_pk), None)
