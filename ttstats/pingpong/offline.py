"""Server side of the service worker's offline caches (KAN-82, KAN-132).

The worker (`templates/pingpong/sw.js`) caches hashed static files and the
live scoreboard page (KAN-82), and -- once KAN-133 lands -- read-only page
snapshots (KAN-131). This module owns the inputs it needs from Python:

* **What to precache at install** (`SW_PRECACHE`). Static paths, resolved
  through the staticfiles storage so they carry production's content hashes.
  A path missing from the manifest raises at render time, exactly as a
  template's `{% static %}` would -- `test_offline.py` checks each exists.
* **Whose data a cached page is** (`identity_fingerprint`). The scoreboard
  page embeds the viewer's CSRF token and player ids, and two people can share
  a phone. The worker files each stored page under the fingerprint the server
  sent with it (`X-TTStats-Identity`) and deletes every other partition when
  a page announces a different one (`<meta name="ttstats-identity">`, sent by
  `static/pingpong/js/offline.js`). Logged out = empty = delete everything.

* **Which pages may be saved for offline reading** (`@offline_snapshot`,
  KAN-132). The server decides, not the worker: a view opts in with the
  decorator, its URL name goes in `SNAPSHOT_ALLOWLIST`, and
  `test_offline_snapshots.py` sweeps every URL pattern so the two can never
  disagree. With the `offline_snapshots` flag on for the viewer, a full-page
  GET answered 200 text/html carries `X-TTStats-Snapshot: 1`, the identity
  header and `Cache-Control: private`; the worker stores nothing without it.
  htmx fragments, other methods, errors, redirects and anonymous requests
  never carry it. Only read-only views belong on the list: nothing that
  writes on GET, no form, no scoreboard (KAN-82 owns it), no token link.
* **The freshness marker** (`FRESHNESS_MARKER`), rendered exactly once, on
  `<html>` in `base.html`. The worker string-replaces it on a saved copy to
  show the "saved copy" banner; a page without it is never served from cache.

The fingerprint is the one KAN-132 specifies -- HMAC of user and active club
-- so the scoreboard and the snapshot caches share one partitioning.
"""
from __future__ import annotations

import hashlib
from functools import wraps

from django.contrib.staticfiles.storage import staticfiles_storage
from django.utils.cache import patch_cache_control
from django.utils.crypto import salted_hmac
from django.utils.decorators import method_decorator

IDENTITY_HEADER = "X-TTStats-Identity"
IDENTITY_LENGTH = 16

SNAPSHOT_HEADER = "X-TTStats-Snapshot"
SNAPSHOT_FLAG = "offline_snapshots"
# The exact attribute base.html renders on <html> and sw.js rewrites to
# "snapshot" on a saved copy (KAN-134). Change both together.
FRESHNESS_MARKER = 'data-freshness="network"'
# Set on a decorated view function (and on a decorated class), so the URL
# sweep can find every opted-in route.
SNAPSHOT_MARKER = "offline_snapshot"

# Read-only pages that may be saved on the phone (KAN-131 open question 1).
# Each name's view is decorated with @offline_snapshot; the sweep test fails
# if this set and the decorated views ever differ.
SNAPSHOT_ALLOWLIST = frozenset({
    "pingpong:dashboard",
    "pingpong:leaderboard",
    "pingpong:match_list",
    "pingpong:match_detail",
    "pingpong:player_detail",
    "pingpong:head_to_head",
    "pingpong:championship_list",
    "pingpong:championship_detail",
    "pingpong:calendar",
    "pingpong:season_archive",
    "pingpong:season_archive_detail",
})

# What the scoreboard needs to boot with no signal, cached when the worker
# installs. Anything else under /static/ is cached the first time a controlled
# page loads it, so a file missing here still works offline after one online
# visit; listing it makes the very first offline open work too.
SW_PRECACHE = (
    "pingpong/css/app.css",
    "pingpong/js/vendor/htmx.min.js",
    "pingpong/js/vendor/alpine.min.js",
    "pingpong/fonts/archivo-400.woff2",
    "pingpong/fonts/archivo-600.woff2",
    "pingpong/fonts/archivo-800.woff2",
    "pingpong/js/push.js",
    "pingpong/js/pull_to_refresh.js",
    "pingpong/js/prefetch.js",
    "pingpong/js/offline.js",
    "pingpong/js/live_rules.js",
    "pingpong/js/live_outbox.js",
    "pingpong/js/live_session.js",
    "pingpong/js/scoreboard.js",
    "pingpong/icons/app/icon-192.png",
)


def precache_urls() -> list[str]:
    """`SW_PRECACHE` as served URLs (hashed under the manifest storage)."""
    return [staticfiles_storage.url(path) for path in SW_PRECACHE]


def build_key(urls: list[str]) -> str:
    """A short digest of the precache URLs, for the worker's cache names.

    The URLs carry content hashes in production, so a deploy that changes any
    of them changes this key, the worker's bytes change with it, the browser
    installs the new worker, and its `activate` deletes the caches named for
    the old key. Nobody has to remember to bump VERSION for a CSS change.
    """
    return hashlib.sha256("\n".join(urls).encode()).hexdigest()[:10]


def identity_fingerprint(user_pk: int, club_pk: int | None) -> str:
    """Opaque partition key for one user in one club. Never a raw id."""
    value = f"{user_pk}:{club_pk if club_pk is not None else ''}"
    return salted_hmac("pingpong.offline.identity", value).hexdigest()[:IDENTITY_LENGTH]


def request_identity(request) -> str:
    """The viewer's fingerprint, or "" for an anonymous request."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return ""
    from ttstats.middleware import get_current_club

    club = get_current_club()
    return identity_fingerprint(user.pk, club.pk if club else None)


def snapshots_enabled(user) -> bool:
    """Is the snapshot opt-in on for this viewer? Never for anonymous."""
    if user is None or not user.is_authenticated:
        return False
    from pingpong import flags

    return flags.is_enabled(SNAPSHOT_FLAG, user)


def _may_snapshot(request, response) -> bool:
    return (
        request.method == "GET"
        and response.status_code == 200
        and response.get("Content-Type", "").startswith("text/html")
        and "HX-Request" not in request.headers
        and snapshots_enabled(getattr(request, "user", None))
    )


def _snapshot_view(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        response = view_func(request, *args, **kwargs)
        if _may_snapshot(request, response):
            response[SNAPSHOT_HEADER] = "1"
            response[IDENTITY_HEADER] = request_identity(request)
            # Per-user data: a shared proxy must never keep it.
            patch_cache_control(response, private=True)
        return response

    setattr(wrapped, SNAPSHOT_MARKER, True)
    return wrapped


def offline_snapshot(view):
    """Opt a read-only view in to offline snapshots (KAN-132).

    Decorates a function view, or a class-based view (its `dispatch`), so
    `@offline_snapshot` sits directly on either. The URL name must also be in
    `SNAPSHOT_ALLOWLIST`.
    """
    if isinstance(view, type):
        view = method_decorator(_snapshot_view, name="dispatch")(view)
        setattr(view, SNAPSHOT_MARKER, True)
        return view
    return _snapshot_view(view)


def is_snapshot_view(callback) -> bool:
    """Does a resolved URL callback opt in? (Function or `as_view()`.)"""
    if getattr(callback, SNAPSHOT_MARKER, False):
        return True
    view_class = getattr(callback, "view_class", None)
    return bool(view_class and getattr(view_class.dispatch, SNAPSHOT_MARKER, False))
