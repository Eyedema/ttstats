"""Dev settings with production's static pipeline, for Playwright (e2e + perf).

Everything is `dev` (DEBUG, SQLite, seed_e2e allowed) except how static files
are named and served, which matches prod.py: collectstatic into a throwaway
STATIC_ROOT through WhiteNoise's CompressedManifestStaticFilesStorage, served
by WhiteNoise with hashed names, pre-compressed bodies and immutable caching.
Without this the byte budgets (tests/e2e/budgets.spec.js) and `npm run perf`
would measure uncompressed, unhashed, uncacheable files prod never serves.

Run under gunicorn (scripts/e2e_server.sh, KAN-168) or `runserver --nostatic`; under a bare runserver Django's
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
