"""`Server-Timing` on every response the app renders (KAN-123 / S10.3).

The header is what `npm run perf` and a browser's network panel read to split
a slow TTFB into "the server" and "the network", and the db entry into "too
many queries" and "slow queries". It is only worth having if it costs nothing
and never breaks a response.
"""
import re
import threading

import pytest
from django.conf import settings
from django.db import connection
from django.http import HttpResponse, StreamingHttpResponse
from django.test import RequestFactory
from django.urls import reverse

from ttstats.server_timing import ServerTimingMiddleware

from .conftest import PlayerFactory

ENTRY = re.compile(r'^(?P<name>[a-z]+);dur=(?P<dur>\d+(\.\d+)?)(;desc="(?P<desc>[^"]*)")?$')


def _entries(header):
    """Parse `a;dur=1.2, b;dur=3;desc="x"` into {name: (dur, desc)}."""
    out = {}
    for raw in header.split(','):
        m = ENTRY.match(raw.strip())
        assert m, f'malformed Server-Timing entry: {raw!r}'
        out[m['name']] = (float(m['dur']), m['desc'])
    return out


def _run(view, path='/pingpong/'):
    return ServerTimingMiddleware(view)(RequestFactory().get(path))


@pytest.mark.django_db
class TestTheHeader:
    def test_app_and_db_entries_are_present(self):
        response = _run(lambda request: HttpResponse('ok'))

        entries = _entries(response['Server-Timing'])
        assert set(entries) == {'app', 'db'}
        assert entries['app'][0] >= 0

    def test_db_counts_the_queries_the_request_ran(self):
        def view(request):
            with connection.cursor() as cursor:
                for _ in range(3):
                    cursor.execute('SELECT 1')
            return HttpResponse('ok')

        entries = _entries(_run(view)['Server-Timing'])

        assert entries['db'][1] == '3 queries'

    def test_a_single_query_is_singular(self):
        def view(request):
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
            return HttpResponse('ok')

        assert _entries(_run(view)['Server-Timing'])['db'][1] == '1 query'

    def test_no_queries_is_reported_as_zero(self):
        entries = _entries(_run(lambda request: HttpResponse('ok'))['Server-Timing'])

        assert entries['db'] == (0.0, '0 queries')

    def test_db_time_is_within_app_time(self):
        def view(request):
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
            return HttpResponse('ok')

        entries = _entries(_run(view)['Server-Timing'])

        assert entries['db'][0] <= entries['app'][0]

    def test_an_existing_header_is_extended_not_replaced(self):
        def view(request):
            response = HttpResponse('ok')
            response['Server-Timing'] = 'tpl;dur=1'
            return response

        entries = _entries(_run(view)['Server-Timing'])

        assert set(entries) == {'tpl', 'app', 'db'}

    def test_streaming_responses_get_the_header_too(self):
        """Headers go out before the body, so the time is "until headers" --
        still the number TTFB is made of. The body must stream untouched."""
        response = _run(lambda request: StreamingHttpResponse(iter([b'a', b'b'])))

        assert 'app' in _entries(response['Server-Timing'])
        assert b''.join(response.streaming_content) == b'ab'


@pytest.mark.django_db
class TestTheWrapperIsPerRequest:
    def test_the_wrapper_is_removed_after_the_request(self):
        before = list(connection.execute_wrappers)

        _run(lambda request: HttpResponse('ok'))

        assert connection.execute_wrappers == before

    def test_the_wrapper_is_removed_when_the_view_raises(self):
        before = list(connection.execute_wrappers)

        def view(request):
            raise ValueError('boom')

        with pytest.raises(ValueError):
            _run(view)

        assert connection.execute_wrappers == before

    def test_queries_outside_a_request_are_not_counted_into_the_next(self):
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')

        entries = _entries(_run(lambda request: HttpResponse('ok'))['Server-Timing'])

        assert entries['db'][1] == '0 queries'

    def test_another_threads_queries_are_not_counted(self):
        """Each thread has its own connection, and the wrapper sits on this
        thread's. A concurrent request's queries must not leak in."""
        def other_thread():
            from django.db import connection as other

            with other.cursor() as cursor:
                cursor.execute('SELECT 1')
            other.close()

        def view(request):
            t = threading.Thread(target=other_thread)
            t.start()
            t.join()
            return HttpResponse('ok')

        entries = _entries(_run(view)['Server-Timing'])

        assert entries['db'][1] == '0 queries'


@pytest.mark.django_db
class TestWiring:
    def test_it_is_installed_directly_below_whitenoise(self):
        """Below WhiteNoise, so a static hit -- answered in WhiteNoise's
        process_request -- never pays for it. Above everything else, so `app`
        covers sessions, auth, the club lookup and gzip."""
        mw = settings.MIDDLEWARE
        assert mw.index('ttstats.server_timing.ServerTimingMiddleware') == (
            mw.index('whitenoise.middleware.WhiteNoiseMiddleware') + 1
        )

    def test_a_rendered_page_carries_it(self, client):
        player = PlayerFactory(with_user=True)
        client.force_login(player.user)

        response = client.get(reverse('pingpong:dashboard'))

        assert response.status_code == 200
        entries = _entries(response['Server-Timing'])
        assert entries['db'][1].endswith('queries')
        assert int(entries['db'][1].split()[0]) > 0

    def test_an_anonymous_redirect_carries_it(self, client):
        response = client.get(reverse('pingpong:dashboard'))

        assert response.status_code == 302
        assert 'app' in _entries(response['Server-Timing'])
