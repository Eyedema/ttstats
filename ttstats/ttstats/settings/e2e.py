"""Dev settings with production's static pipeline, for Playwright (e2e + perf).

Everything is `dev` (DEBUG, SQLite, seed_e2e allowed) except how static files
are named and served, which matches prod.py: collectstatic into a throwaway
STATIC_ROOT through WhiteNoise's CompressedManifestStaticFilesStorage, served
by WhiteNoise with hashed names, pre-compressed bodies and immutable caching.
Without this the byte budgets (tests/e2e/budgets.spec.js) and `npm run perf`
would measure uncompressed, unhashed, uncacheable files prod never serves.

Run under `runserver --nostatic` (scripts/e2e_server.sh); under a bare runserver Django's
own static handler answers /static/ before WhiteNoise sees it.
"""
import os

from whitenoise.storage import CompressedManifestStaticFilesStorage

from .dev import *  # noqa: F403
from .dev import BASE_DIR, STORAGES


class HashedUnderDebugStorage(CompressedManifestStaticFilesStorage):
    """Prod's storage, but `{% static %}` hashes even though DEBUG is on.

    ManifestFilesMixin returns the plain name when DEBUG is True unless the
    caller forces it. Forcing keeps the URLs (and WhiteNoise's immutable-file
    test, which round-trips through url()) identical to production's.
    """

    def url(self, name, force=False):
        return super().url(name, force=True)


STATIC_ROOT = os.environ.get('TTSTATS_STATIC_ROOT') or BASE_DIR / 'staticfiles_e2e'

STORAGES = {
    **STORAGES,
    "staticfiles": {"BACKEND": "ttstats.settings.e2e.HashedUnderDebugStorage"},
}

# WhiteNoise defaults these to DEBUG. Autorefresh / finders would serve the
# unhashed source files with max-age=0; prod serves STATIC_ROOT only.
WHITENOISE_AUTOREFRESH = False
WHITENOISE_USE_FINDERS = False
WHITENOISE_MAX_AGE = 60  # prod's default for non-hashed files (DEBUG=False)

# --- Shared scoring timings (KAN-155) ----------------------------------------
# The two-phone specs wait out a hold and the presence thresholds for real
# (takeover after the other phone dies), so they are short here and
# env-tunable. IN_SYNC stays above live_ops.SEEN_EVERY (5 s), or a phone that
# is between two polls would read as "last seen" for a moment.
LIVE_LONGPOLL_WAIT = int(os.environ.get('E2E_LONGPOLL_WAIT', '6'))
LIVE_PRESENCE_IN_SYNC = int(os.environ.get('E2E_PRESENCE_IN_SYNC', '6'))
LIVE_PRESENCE_LOST = int(os.environ.get('E2E_PRESENCE_LOST', '20'))

# Rate limits off. The suite repeats the join link (20/h by IP) and the
# login form (5/15 min by IP) far past any per-IP budget, every request from
# 127.0.0.1, and `--repeat-each=10` multiplies that. The limits themselves
# are pytest's (test_club_invites.py, test_shared_scoring.py, test_views.py).
RATELIMIT_ENABLE = False

