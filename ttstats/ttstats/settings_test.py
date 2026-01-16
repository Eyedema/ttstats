from .settings.base import *  # noqa: F403

# Use SQLite for faster tests
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

# Use console email backend for tests
EMAIL_BACKEND = 'django.core.mail.backends.locator.EmailBackend'

# Disable migrations for faster tests (optional)
class DisableMigrations:
    def __contains__(self, item):
        return True
    def __getitem__(self, item):
        return None

# MIGRATION_MODULES = DisableMigrations()  # Uncomment if you want faster tests

# Disable logging during tests
LOGGING = {}
