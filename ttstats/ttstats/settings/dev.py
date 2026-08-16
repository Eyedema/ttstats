"""Development settings"""
from .base import *

DEBUG = True

SECRET_KEY = 'django-insecure-dev-key-change-in-production'

ALLOWED_HOSTS = ['localhost', '127.0.0.1', '0.0.0.0']

# SQLite for local dev, unless DATABASE_URL points somewhere else (the Supabase
# clone of prod). Tests never reach here: settings/test.py overrides DATABASES
# after importing this module, so an exported DATABASE_URL cannot redirect them.
if os.environ.get('DATABASE_URL'):
    import dj_database_url

    DATABASES = {
        'default': dj_database_url.parse(
            os.environ['DATABASE_URL'],
            conn_max_age=600,
            ssl_require=True,
        )
    }
else:
    # TTSTATS_SQLITE_NAME lets the Playwright suite point at a throwaway file
    # instead of the working db.sqlite3, so an e2e run cannot clobber whatever
    # you have been clicking around in.
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': os.environ.get('TTSTATS_SQLITE_NAME') or BASE_DIR / 'db.sqlite3',
        }
    }

# Development-specific settings
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# Disable some security for local dev
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# Cache debug headers (X-Cache-Hits, X-Cache-Misses, X-Request-Time)
MIDDLEWARE += ['ttstats.middleware.CacheDebugMiddleware']
