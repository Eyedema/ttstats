"""What `settings/prod.py` does with the database and sessions (KAN-123).

prod.py refuses to import without its required environment, and importing it
in-process would initialise Sentry and replace this suite's settings. Each
case therefore imports it in a fresh interpreter and reads the values back.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]  # the directory with manage.py

REQUIRED_ENV = {
    'SECRET_KEY': 'x' * 50,
    'ALLOWED_HOSTS': 'example.com',
    'SITE_DOMAIN': 'example.com',
    'SENTRY_DSN': '',
}

PROBE = """
import json
from ttstats.settings import prod
db = prod.DATABASES['default']
print(json.dumps({
    'conn_max_age': db.get('CONN_MAX_AGE'),
    'conn_health_checks': db.get('CONN_HEALTH_CHECKS'),
    # Unset in prod.py means Django's default, the plain `db` engine.
    'session_engine': getattr(prod, 'SESSION_ENGINE', 'django.contrib.sessions.backends.db'),
}))
"""


def _prod_settings(**env):
    full = {
        key: value
        for key, value in os.environ.items()
        if key not in ('REDIS_URL', 'DB_CONN_MAX_AGE', 'DJANGO_SETTINGS_MODULE')
    }
    full.update(REQUIRED_ENV)
    full.update(env)
    out = subprocess.run(
        [sys.executable, '-c', PROBE],
        cwd=PROJECT_DIR,
        env=full,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


class TestPersistentConnections:
    def test_connections_are_kept_for_a_minute_by_default(self):
        s = _prod_settings()

        assert s['conn_max_age'] == 60
        assert s['conn_health_checks'] is True

    def test_the_lifetime_is_env_overridable(self):
        """0 is the rollback: Django's close-after-every-request default."""
        assert _prod_settings(DB_CONN_MAX_AGE='0')['conn_max_age'] == 0


class TestSessionEngine:
    def test_cached_db_when_redis_is_configured(self):
        s = _prod_settings(REDIS_URL='redis://localhost:6379/1')

        assert s['session_engine'] == 'django.contrib.sessions.backends.cached_db'

    def test_plain_db_without_a_shared_cache(self):
        """LocMemCache is per worker. A session cached there goes stale the
        moment another worker writes it -- and a logout on one worker would
        leave the session alive in the others' memory."""
        s = _prod_settings()

        assert s['session_engine'] == 'django.contrib.sessions.backends.db'
