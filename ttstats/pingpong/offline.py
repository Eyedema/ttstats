"""Server side of the service worker's offline cache (KAN-82).

The worker (`templates/pingpong/sw.js`) caches exactly two things: hashed
static files and the live scoreboard page. This module owns the two inputs it
needs from Python:

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

The fingerprint is the one KAN-132 (offline snapshots) specifies -- HMAC of
user and active club -- so the read-side snapshot cache can share the
partitioning instead of inventing a second one.
"""
from __future__ import annotations

import hashlib

from django.contrib.staticfiles.storage import staticfiles_storage
from django.utils.crypto import salted_hmac

IDENTITY_HEADER = "X-TTStats-Identity"
IDENTITY_LENGTH = 16

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
