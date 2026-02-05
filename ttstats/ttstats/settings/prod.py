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
CSP_SCRIPT_SRC = ("'self'", "'unsafe-inline'")  # Needed for inline scripts
CSP_STYLE_SRC = ("'self'", "'unsafe-inline'")   # Needed for Tailwind inline styles
CSP_IMG_SRC = ("'self'", "data:")
CSP_FONT_SRC = ("'self'",)
CSP_CONNECT_SRC = ("'self'",)
CSP_FRAME_ANCESTORS = ("'none'",)
CSP_FORM_ACTION = ("'self'",)

# Static files
STATIC_ROOT = '/app/static/'

# Logging
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
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

# WebAuthn configuration for production
OTP_WEBAUTHN_RP_ID = os.environ.get("SITE_DOMAIN")
OTP_WEBAUTHN_ALLOWED_ORIGINS = [
    f"https://{os.environ.get('SITE_DOMAIN')}"
]