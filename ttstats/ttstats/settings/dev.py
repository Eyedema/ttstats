"""Development settings"""
from .base import *

DEBUG = True

SECRET_KEY = 'django-insecure-dev-key-change-in-production'

ALLOWED_HOSTS = ['localhost', '127.0.0.1', '0.0.0.0']

# SQLite for local dev (or Postgres if you prefer)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
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
