"""`GET /healthz` (KAN-109): is this process able to serve?

Answered by a middleware, first in `MIDDLEWARE`, rather than a view, because
everything after it is wrong for a probe:

- `SecurityMiddleware` would 301 Docker's plain-HTTP probe to https;
- host validation would 400 a probe to `127.0.0.1:8000` (`ALLOWED_HOSTS` is the
  public name only);
- sessions, auth, OTP and the tenancy middleware cost queries and can mint
  cookies, and the probe is unauthenticated by definition;
- `ServerTimingMiddleware`/gzip are noise on a 100-byte body.

Who calls it: the container `healthcheck` (compose.prod.yml), the deploy's
post-swap smoke test (deploy/deploy.sh), the uptime monitor, and the offline
"back online?" probe (snapshot_mode.js). It is outside `/pingpong/`, so the
service worker never answers it, is never rate limited (limits are per-view
decorators), and `sentry.make_traces_sampler` never traces it.

Checks, cheapest first:

- **db**: `SELECT 1` on the default connection. Failure is 503.
- **migrations**: no unapplied migration for this image's code. Failure is 503:
  a container that boots against an unmigrated schema (the deploy runs
  `migrate` once, before the swap) must never take traffic. The plan is not
  free, so a clean answer is remembered for the life of the process (a
  migration cannot become unapplied under running code) and a pending one is
  re-asked at most every `PENDING_RECHECK_SECONDS`.
- **cache**: a set/get round trip, given `CACHE_BUDGET_SECONDS`. Failure or
  timeout is **tolerated** (`"degraded"`, still 200): Redis down makes the app
  slower, not unavailable, and a probe that rolls back deploys must not fire
  on it.

The body names which check failed, never why: no exception text, no hosts.
"""
import json
import logging
import os
import threading
import time
import uuid

from django.core.cache import cache
from django.db import DEFAULT_DB_ALIAS, connections
from django.http import HttpResponse
from django.utils.cache import add_never_cache_headers

logger = logging.getLogger(__name__)

PATHS = frozenset({"/healthz", "/healthz/"})
CACHE_KEY = "healthz:ping"
PENDING_RECHECK_SECONDS = 10
# django-redis: 5 s socket timeout, retried once. A hung (not refused) Redis
# would hold the probe past Docker's timeout and fail a check that is meant to
# tolerate it, so the ping gets its own budget on a daemon thread.
CACHE_BUDGET_SECONDS = 1.0

_memo_lock = threading.Lock()
_migrations_clean = False
_pending_checked_at = None


def reset_migration_memo():
    """Forget the per-process migration answer (tests)."""
    global _migrations_clean, _pending_checked_at
    with _memo_lock:
        _migrations_clean = False
        _pending_checked_at = None


def _check_db():
    with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()


def _check_cache():
    token = uuid.uuid4().hex
    cache.set(CACHE_KEY, token, 30)
    if cache.get(CACHE_KEY) != token:
        raise RuntimeError("cache round trip returned a different value")


def _bounded_cache_check():
    """'ok', 'error' or 'timeout'. Never blocks longer than the budget."""
    outcome = {}

    def ping():
        try:
            _check_cache()
            outcome["result"] = "ok"
        except Exception:
            logger.warning("healthz: cache check failed", exc_info=True)
            outcome["result"] = "error"

    worker = threading.Thread(target=ping, name="healthz-cache", daemon=True)
    worker.start()
    worker.join(CACHE_BUDGET_SECONDS)
    if worker.is_alive():
        logger.warning("healthz: cache check exceeded %.1fs", CACHE_BUDGET_SECONDS)
        return "timeout"
    return outcome.get("result", "error")


def _pending_migrations():
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connections[DEFAULT_DB_ALIAS])
    return bool(executor.migration_plan(executor.loader.graph.leaf_nodes()))


def _check_migrations():
    """'ok' or 'pending'; raises if the plan itself cannot be computed."""
    global _migrations_clean, _pending_checked_at
    if _migrations_clean:
        return "ok"
    now = time.monotonic()
    with _memo_lock:
        recent = (
            _pending_checked_at is not None
            and now - _pending_checked_at < PENDING_RECHECK_SECONDS
        )
    if recent:
        return "pending"
    pending = _pending_migrations()
    with _memo_lock:
        if pending:
            _pending_checked_at = now
        else:
            _migrations_clean = True
    return "pending" if pending else "ok"


def run_checks():
    """`(http_status, body)`. Never raises."""
    checks = {}
    healthy = True

    try:
        _check_db()
        checks["db"] = "ok"
    except Exception:
        logger.warning("healthz: database check failed", exc_info=True)
        checks["db"] = "error"
        healthy = False

    checks["cache"] = _bounded_cache_check()

    if checks["db"] == "ok":
        try:
            checks["migrations"] = _check_migrations()
        except Exception:
            logger.warning("healthz: migration check failed", exc_info=True)
            checks["migrations"] = "error"
    else:
        checks["migrations"] = "unknown"
    if checks["migrations"] != "ok":
        healthy = False

    if not healthy:
        status = "error"
    elif checks["cache"] != "ok":
        status = "degraded"
    else:
        status = "ok"
    body = {
        "status": status,
        "release": os.environ.get("SENTRY_RELEASE") or None,
        "checks": checks,
    }
    return (200 if healthy else 503), body


def healthz_response(request):
    if request.method not in ("GET", "HEAD"):
        response = HttpResponse(status=405)
        response["Allow"] = "GET, HEAD"
        return response
    status, body = run_checks()
    content = b"" if request.method == "HEAD" else json.dumps(body).encode()
    response = HttpResponse(content, status=status, content_type="application/json")
    add_never_cache_headers(response)
    return response


class HealthzMiddleware:
    """Answer `/healthz` before the rest of the stack runs. See module docstring."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path_info in PATHS:
            return healthz_response(request)
        return self.get_response(request)
