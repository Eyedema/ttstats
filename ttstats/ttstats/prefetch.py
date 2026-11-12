"""Let a tap-intent prefetch be reused by the navigation that follows (KAN-124).

`pingpong/js/prefetch.js` fetches a tab's page on `touchstart` (or
`mousedown`) with `Purpose: prefetch`, roughly 100 ms before the `click`
starts the real navigation. A rendered page carries no `Cache-Control`, so the
browser would not store that response and the navigation would fetch the page
again. This middleware marks a prefetch's HTML

    Cache-Control: private, max-age=5

so the navigation is answered from the HTTP cache instead (Chromium even joins
the prefetch while it is still in flight). Only prefetch requests are marked:
a normal navigation is untouched, and so is anything a view already decided
about caching (`never_cache` wins). A bare `Cache-Control: private` -- what
an offline-snapshot page sends (KAN-132, pingpong/offline.py) -- says who may
store the page, not for how long, so a prefetch of one still gets its window.

Why five seconds: the window only has to span a tap. Anything longer widens
the one stale case, a prefetch followed within the window by a POST that
redirects to the same URL (the redirect would be answered from the cache).

Not marked: non-GET, non-200 (a login redirect must not be replayed after the
user logs back in), non-HTML, streaming.
"""
from django.utils.cache import patch_cache_control

PREFETCH_MAX_AGE = 5


def is_prefetch(request):
    """True for our own `Purpose: prefetch` and browsers' `Sec-Purpose: prefetch`."""
    for header in ('Purpose', 'Sec-Purpose'):
        value = request.headers.get(header, '')
        if any(token.strip().split(';')[0] == 'prefetch' for token in value.split(',')):
            return True
    return False


def _lifetime_undecided(response):
    """No Cache-Control, or one that only says `private`."""
    directives = {
        part.split('=')[0].strip().lower()
        for part in response.get('Cache-Control', '').split(',')
        if part.strip()
    }
    return directives <= {'private'}


class PrefetchCacheMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            request.method == 'GET'
            and response.status_code == 200
            and not response.streaming
            and _lifetime_undecided(response)
            and response.get('Content-Type', '').startswith('text/html')
            and is_prefetch(request)
        ):
            patch_cache_control(response, private=True, max_age=PREFETCH_MAX_AGE)
        return response
