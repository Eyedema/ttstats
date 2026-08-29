import time
from threading import local

from django.conf import settings

_thread_locals = local()

# Everything this middleware may stash on the thread-local, in one place.
#
# It exists because the list was duplicated across six test cleanup fixtures
# and adding a seventh attribute silently left stale state in all of them --
# which shows up as a test reading another test's club. Tests clear
# REQUEST_LOCAL_ATTRS rather than their own hand-copied tuple.
REQUEST_LOCAL_ATTRS = (
    'user',
    'session',
    'club',
    'user_clubs',
    'user_memberships',
    'flag_memo',
    'request_path',
)


# Where the switcher stores the viewer's chosen club. The session is the right
# home: SessionMiddleware runs before CurrentUserMiddleware, it survives
# navigation, and it costs nothing extra to read.
ACTIVE_CLUB_SESSION_KEY = 'active_club_id'


def get_current_user():
    """
    Get the current user from thread-local storage.
    Used by custom managers to filter querysets automatically.
    """
    return getattr(_thread_locals, 'user', None)


def get_current_club():
    """The club the current request is looking at, or None.

    `None` means two different things, and the difference is the whole
    security contract:

    * **No user in context** (a management command, a migration) -- there is no
      viewer, so downstream code stays *unfiltered*. This matches the existing
      `get_current_user() is None` contract exactly.
    * **An authenticated user with no club** -- there is a viewer and they
      belong nowhere, so downstream code shows *nothing*. Failing open here
      would make "no club" the widest possible permission, which is precisely
      backwards.

    Managers therefore branch on the *user* first and only then consult the
    club; see `managers.ScopedManagerMixin`.

    Resolved lazily and memoized for the request, so a page that never touches
    a scoped manager pays no query at all.
    """
    if hasattr(_thread_locals, 'club'):
        return _thread_locals.club

    club = _resolve_active_club(
        get_current_user(),
        getattr(_thread_locals, 'session', None),
    )
    _thread_locals.club = club
    return club


def get_user_memberships():
    """The viewer's memberships, club preloaded, memoized for the request.

    **One query answers three questions**: which clubs the user is in, which is
    active, and what role they hold in it. Asking separately put two identical
    queries on every authenticated page render, and adding the role check for
    the switcher's "Club settings" row would have made it three -- on every
    page in the app, to decide whether to draw one menu row.
    """
    if hasattr(_thread_locals, 'user_memberships'):
        return _thread_locals.user_memberships

    user = get_current_user()
    if not user or not getattr(user, 'is_authenticated', False):
        memberships = []
    else:
        from pingpong.models import ClubMembership

        memberships = list(
            ClubMembership.objects.filter(user=user)
            .select_related('club')
            .order_by('club__name')
        )

    _thread_locals.user_memberships = memberships
    return memberships


def get_user_clubs():
    """The clubs the viewer belongs to, ordered by name."""
    return [membership.club for membership in get_user_memberships()]


def get_current_membership():
    """The viewer's membership of their *active* club, or None.

    Read from the same memoized list, so a role check costs no extra query.
    """
    club = get_current_club()
    if club is None:
        return None
    for membership in get_user_memberships():
        if membership.club_id == club.pk:
            return membership
    return None


def _resolve_active_club(user, session):
    """Pick the viewer's active club, re-checking membership every time.

    **The membership re-check is the tenancy boundary.** A club id arriving
    from a session (or, later, a query parameter or header) is an untrusted
    claim: a user removed from a club mid-session still holds its id, and a
    forged one costs nothing to try. So the stored id is only ever used to
    *choose among* the clubs the user is actually in -- it can never introduce
    one.

    One indexed query, and only for authenticated users.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return None

    memberships = get_user_clubs()
    if not memberships:
        return None

    stored = (session or {}).get(ACTIVE_CLUB_SESSION_KEY)
    if stored is not None:
        for club in memberships:
            if club.pk == stored:
                return club
        # Stale or forged. Drop it rather than carrying a selection that will
        # keep failing this check on every subsequent request.
        try:
            del session[ACTIVE_CLUB_SESSION_KEY]
        except (KeyError, TypeError):
            pass

    # Deterministic fallback: `for_user` orders by name, so the same user gets
    # the same club on every request until they choose otherwise.
    return memberships[0]


def set_active_club(request, club):
    """Record the viewer's club choice, and make it effective immediately.

    Callers (the S1.8 switcher) MUST have already checked membership --
    `Club.objects.for_user()` is how. This only records a choice; it is not an
    authorization check, and `_resolve_active_club` re-checks on every later
    request regardless.

    Two tabs in two clubs means last write wins. That is acceptable: a club is
    a viewing context, and the alternative -- per-tab state -- is not
    expressible in a session.
    """
    request.session[ACTIVE_CLUB_SESSION_KEY] = club.pk if club else None
    _thread_locals.club = club


class CurrentUserMiddleware:
    """
    Middleware that stores the current request user in thread-local storage.
    This enables automatic row-level security filtering in model managers.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Store current user in thread-local storage
        _thread_locals.user = getattr(request, 'user', None)
        # The session is stashed rather than the resolved club: resolution is
        # lazy, so a page that never touches a scoped manager pays no query.
        _thread_locals.session = getattr(request, 'session', None)
        # For the log filter. The path only -- never the query string,
        # which can carry tokens.
        _thread_locals.request_path = request.path

        # Clear the memo on the way *in* as well as out. Clearing only on exit
        # assumes every prior request left through this middleware, and the one
        # that did not -- a crash above us, a worker recycled mid-request, a
        # test that set the thread-local by hand -- would hand this request a
        # club resolved for somebody else. Entering a request must start from a
        # known state, not a hoped-for one.
        for attr in ('club', 'user_clubs', 'user_memberships'):
            if hasattr(_thread_locals, attr):
                delattr(_thread_locals, attr)

        # Feature-flag resolution is memoized for the life of the request.
        # `managers.get_queryset()` checks the clubs flag and runs constantly,
        # so without this every query would touch the cache backend.
        from pingpong import flags

        try:
            with flags.request_scope():
                response = self.get_response(request)
        finally:
            # Clean up thread-local storage after request. In a `finally` so an
            # exception inside the view cannot leave this set for whatever the
            # thread serves next -- with two thread-locals plus the flag memo
            # the leak surface is three times what it was, and a leaked *club*
            # is a cross-tenant read rather than merely a stale badge count.
            for attr in REQUEST_LOCAL_ATTRS:
                if hasattr(_thread_locals, attr):
                    delattr(_thread_locals, attr)

        return response


class CacheDebugMiddleware:
    """Add cache statistics to response headers (DEBUG mode only)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not settings.DEBUG:
            return self.get_response(request)

        start_time = time.time()

        initial_hits = initial_misses = 0
        con = None
        try:
            from django_redis import get_redis_connection
            con = get_redis_connection("default")
            info = con.info()
            initial_hits = info.get('keyspace_hits', 0)
            initial_misses = info.get('keyspace_misses', 0)
        except Exception:
            pass

        response = self.get_response(request)

        duration = time.time() - start_time
        response['X-Request-Time'] = f'{duration:.3f}s'

        if con:
            try:
                info = con.info()
                response['X-Cache-Hits'] = str(info.get('keyspace_hits', 0) - initial_hits)
                response['X-Cache-Misses'] = str(info.get('keyspace_misses', 0) - initial_misses)
            except Exception:
                pass

        return response
