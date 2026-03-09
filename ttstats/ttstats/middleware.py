import time
from threading import local

from django.conf import settings

_thread_locals = local()


def get_current_user():
    """
    Get the current user from thread-local storage.
    Used by custom managers to filter querysets automatically.
    """
    return getattr(_thread_locals, 'user', None)


class CurrentUserMiddleware:
    """
    Middleware that stores the current request user in thread-local storage.
    This enables automatic row-level security filtering in model managers.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Store current user in thread-local storage
        _thread_locals.user = getattr(request, 'user', None)

        try:
            response = self.get_response(request)
        finally:
            # Clean up thread-local storage after request
            if hasattr(_thread_locals, 'user'):
                del _thread_locals.user

        return response


class CacheDebugMiddleware:
    """Add cache statistics to response headers (DEBUG mode only)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not settings.DEBUG:
            return self.get_response(request)

        start_time = time.time()

        initial_hits = initial_misses = 0
        con = None
        try:
            from django_redis import get_redis_connection
            con = get_redis_connection("default")
            info = con.info()
            initial_hits = info.get('keyspace_hits', 0)
            initial_misses = info.get('keyspace_misses', 0)
        except Exception:
            pass

        response = self.get_response(request)

        duration = time.time() - start_time
        response['X-Request-Time'] = f'{duration:.3f}s'

        if con:
            try:
                info = con.info()
                response['X-Cache-Hits'] = str(info.get('keyspace_hits', 0) - initial_hits)
                response['X-Cache-Misses'] = str(info.get('keyspace_misses', 0) - initial_misses)
            except Exception:
                pass

        return response
