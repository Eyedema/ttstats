"""`Server-Timing` header: where a request's server time went (KAN-123).

    Server-Timing: app;dur=12.4, db;dur=3.1;desc="7 queries"

* `app` -- wall time from this middleware to the response headers: sessions,
  auth, the club lookup, the view, template rendering and gzip.
* `db` -- time inside the database driver, and how many statements ran.

Browsers show it in the network panel and `npm run perf` prints the medians,
so a slow TTFB can be split into "the server" and "the wire", and a slow
server into "too many queries" and "slow queries" without a profiler.

There is no `cache` entry: the cache backends offer no hook as cheap as
`execute_wrapper`, and wrapping the cache object would put a proxy on every
cache call in the app to produce a number nobody has asked for yet.

**Cost.** Two `perf_counter()` calls per request and two per query, plus one
header. Measured overhead is in the KAN-119 decision record.

**Per request, per thread.** `connection` is the current thread's connection
and `execute_wrapper` is a context manager that removes itself on the way out,
exceptions included. Each request gets its own `_QueryTimer`, so concurrent
requests on other threads never count into each other.

**Streaming responses** get the header too. Headers are sent before the body,
so the number is "time until headers" -- the part TTFB is made of. Queries a
streaming body runs while it is iterated are after the header and not counted.
"""
import time

from django.db import connection


class _QueryTimer:
    __slots__ = ('count', 'seconds')

    def __init__(self):
        self.count = 0
        self.seconds = 0.0

    def __call__(self, execute, sql, params, many, context):
        start = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            self.seconds += time.perf_counter() - start
            self.count += 1


class ServerTimingMiddleware:
    """Installed directly below WhiteNoise in `settings/base.py`.

    Below it, so a static file -- answered in WhiteNoise's process_request --
    never pays for this. Above everything else, so `app` covers the whole
    request. (`/sw.js` and the manifest are views, so they carry it; that is
    harmless.) There is no health-check endpoint to exempt.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        timer = _QueryTimer()
        start = time.perf_counter()
        with connection.execute_wrapper(timer):
            response = self.get_response(request)
        app_ms = (time.perf_counter() - start) * 1000

        noun = 'query' if timer.count == 1 else 'queries'
        entry = (
            f'app;dur={app_ms:.1f}, '
            f'db;dur={timer.seconds * 1000:.1f};desc="{timer.count} {noun}"'
        )
        existing = response.get('Server-Timing')
        response['Server-Timing'] = f'{existing}, {entry}' if existing else entry
        return response
