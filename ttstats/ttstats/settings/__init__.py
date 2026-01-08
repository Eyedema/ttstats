"""
Settings module that auto-selects dev or prod based on DJANGO_ENV.
"""
import os

env = os.getenv("DJANGO_ENV", "dev")

if env == "prod":
    from .prod import *
else:
    from .dev import *
