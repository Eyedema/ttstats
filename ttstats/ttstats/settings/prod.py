"""Production settings"""
from .base import *
import os

DEBUG = False

# CRITICAL: Use environment variable
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    raise ValueError("SECRET_KEY environment variable must be set in production")

# Validate ALLOWED_HOSTS - MUST be set in production
allowed_hosts_str = os.environ.get('ALLOWED_HOSTS', '')
if not allowed_hosts_str:
    raise ValueError("ALLOWED_HOSTS environment variable must be set in production")
ALLOWED_HOSTS = [h.strip() for h in allowed_hosts_str.split(',') if h.strip()]
if not ALLOWED_HOSTS:
    raise ValueError("ALLOWED_HOSTS must contain at least one valid host")

# PostgreSQL from environment
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('DB_NAME', 'pingpong'),
        'USER': os.environ.get('DB_USER', 'pingpong'),
        'PASSWORD': os.environ.get('DB_PASSWORD'),
        'HOST': os.environ.get('DB_HOST', 'db'),
        'PORT': os.environ.get('DB_PORT', '5432'),
    }
}
# Security settings for production
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# Tighter session settings for production
SESSION_COOKIE_AGE = 86400  # 1 day instead of 2 weeks

# Content Security Policy (CSP)
MIDDLEWARE.insert(1, 'csp.middleware.CSPMiddleware')

# CSP settings - adjust based on your actual requirements
CSP_DEFAULT_SRC = ("'self'",)
CSP_SCRIPT_SRC = (
    "'self'",
    # Still needed: base.html and several page templates carry inline
    # <script> blocks. B.5 moves the rules out of them; the directive can
    # tighten once nothing inline remains.
    "'unsafe-inline'",
    # Alpine 3's standard build compiles every expression with new Function().
    # Without this the live scoreboard is dead on arrival in production -- 27
    # CSP violations, and choosing a server does nothing -- while working
    # perfectly in dev, which sends no CSP at all. The mobile drawer shipped
    # broken for exactly this reason before anyone noticed the scoreboard was
    # too.
    #
    # This is a real weakening, and it is deliberate. It is also a smaller step
    # than it looks: 'unsafe-inline' above already permits injected inline
    # scripts, so the policy's XSS value is limited today either way. The way
    # to remove BOTH is the @alpinejs/csp build (every expression becomes an
    # Alpine.data() member), done alongside B.5's removal of inline scripts.
    #
    # The e2e suite deliberately keeps testing against the stricter policy --
    # see PROD_CSP in tests/e2e/helpers.js -- so the mobile drawer, which is
    # plain JS, can never quietly regress onto eval.
    "'unsafe-eval'",
)
CSP_STYLE_SRC = (
    "'self'",
    "'unsafe-inline'",
)
CSP_IMG_SRC = ("'self'", "data:")
CSP_FONT_SRC = ("'self'",)
CSP_CONNECT_SRC = ("'self'",)
# The service worker. Without this, worker-src falls back to child-src and
# then to default-src -- which is 'self' and would happen to work, but only by
# accident. Naming it means a future default-src change cannot silently
# unregister push for everyone.
CSP_WORKER_SRC = ("'self'",)
# The web app manifest. Chrome enforces manifest-src separately from
# default-src; a blocked manifest means no install prompt and no home-screen
# app, which on iOS means no push at all.
CSP_MANIFEST_SRC = ("'self'",)
CSP_FRAME_ANCESTORS = ("'none'",)
CSP_FORM_ACTION = ("'self'",)

# Static files
STATIC_ROOT = '/app/static/'

# Hash every static file's name and pre-compress it. Requires collectstatic
# to have run, which entrypoint.sh does before starting Gunicorn.
STORAGES = {
    **STORAGES,
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Logging
# Every record carries the request path, user id and active club id.
#
# A tenancy leak is silent -- no raise, no 500, no log line -- so the one thing
# an incident needs from these logs is "which tenant was this". Without the
# context there is no way to answer it, and the first signal would be a user
# asking "who is Marco?".
#
# **Ids only, never identities.** No email addresses, usernames, tokens or push
# endpoints: a log aggregator is a copy of your data somewhere else, and an id
# answers "which tenant" while a username does not.
#
# Dev is deliberately left alone -- the plain console handler is the right thing
# there, and the asymmetry is contained because the filter always sets its
# attributes (see RequestContextFilter), so a record from a management command
# formats fine.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {
        'request_context': {
            '()': 'pingpong.observability.RequestContextFilter',
        },
    },
    'formatters': {
        'tenant': {
            'format': (
                '%(asctime)s %(levelname)s %(name)s '
                'path=%(request_path)s user=%(user_id)s club=%(club_id)s '
                '%(message)s'
            ),
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'filters': ['request_context'],
            'formatter': 'tenant',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        # A 500 must be findable. Django logs unhandled exceptions here at
        # ERROR with the full traceback; previously they went to the same
        # undifferentiated INFO stream as everything else.
        'django.request': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
        # The leak canary. Kept at ERROR and on its own logger so it can be
        # grepped, alerted on, or shipped separately.
        'pingpong.tenancy': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
    },
}

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.eu.mailgun.org')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', 587))
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'true').lower() in ['true', '1', 'yes']
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', '')  

SITE_PROTOCOL = 'https'
SITE_DOMAIN = os.environ.get('SITE_DOMAIN')
if not SITE_DOMAIN:
    raise ValueError("SITE_DOMAIN environment variable must be set in production")

# WebAuthn configuration for production
OTP_WEBAUTHN_RP_ID = SITE_DOMAIN
OTP_WEBAUTHN_ALLOWED_ORIGINS = [
    f"https://{SITE_DOMAIN}"
]