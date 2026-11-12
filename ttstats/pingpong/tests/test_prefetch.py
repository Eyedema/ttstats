"""Tap-intent prefetch (KAN-124 / S10.4).

`prefetch.js` fetches a tab's page on `touchstart` with `Purpose: prefetch`.
The navigation that follows can only reuse that response if the browser was
allowed to store it, so `PrefetchCacheMiddleware` marks a prefetch's HTML
`private, max-age=5`, and nothing else. A normal navigation keeps the
no-Cache-Control default (never stored), and anything a view already decided
about caching wins.
"""
import re

import pytest
from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect, StreamingHttpResponse
from django.test import RequestFactory
from django.urls import reverse
from django.views.decorators.cache import never_cache

from ttstats.prefetch import PREFETCH_MAX_AGE, PrefetchCacheMiddleware

from .test_views import _login_client, _staff_with_player, _verified_user_with_player

EXPECTED = f'private, max-age={PREFETCH_MAX_AGE}'


def _html(request):
    return HttpResponse('<p>page</p>', content_type='text/html; charset=utf-8')


def _run(view=_html, method='get', **headers):
    request = getattr(RequestFactory(), method)('/pingpong/', headers=headers)
    return PrefetchCacheMiddleware(view)(request)


class TestPrefetchRequestsAreBrieflyCacheable:
    def test_purpose_prefetch_gets_a_short_private_lifetime(self):
        response = _run(Purpose='prefetch')
        assert response['Cache-Control'] == EXPECTED

    def test_sec_purpose_prefetch_too(self):
        # What Chromium's own prefetchers send (speculation rules, link
        # rel=prefetch). Firefox's link prefetch sends it as well.
        response = _run(**{'Sec-Purpose': 'prefetch'})
        assert response['Cache-Control'] == EXPECTED

    def test_the_lifetime_is_seconds_not_minutes(self):
        # These pages are live data. The window only has to span a tap.
        assert 0 < PREFETCH_MAX_AGE <= 10

    def test_it_is_private_never_shared(self):
        # Every page is per-user; a shared cache must never keep one.
        assert 'private' in _run(Purpose='prefetch')['Cache-Control']
        assert 'public' not in _run(Purpose='prefetch')['Cache-Control']


class TestEverythingElseIsLeftAlone:
    def test_a_normal_navigation_gets_no_cache_control(self):
        assert not _run().has_header('Cache-Control')

    def test_an_unrelated_purpose_value(self):
        assert not _run(Purpose='prerender-but-not-the-p-word').has_header('Cache-Control')

    def test_a_post_is_never_made_cacheable(self):
        assert not _run(method='post', Purpose='prefetch').has_header('Cache-Control')

    def test_a_redirect_is_not_cached(self):
        # An expired session redirects a prefetch to the login page. Caching
        # that 302 would replay it after the user logged back in.
        response = _run(lambda r: HttpResponseRedirect('/accounts/login/'), Purpose='prefetch')
        assert not response.has_header('Cache-Control')

    def test_an_error_is_not_cached(self):
        response = _run(lambda r: HttpResponse('boom', status=500), Purpose='prefetch')
        assert not response.has_header('Cache-Control')

    def test_non_html_is_not_cached(self):
        response = _run(
            lambda r: HttpResponse('{}', content_type='application/json'), Purpose='prefetch'
        )
        assert not response.has_header('Cache-Control')

    def test_streaming_is_not_cached(self):
        response = _run(
            lambda r: StreamingHttpResponse(iter(['a']), content_type='text/html'),
            Purpose='prefetch',
        )
        assert not response.has_header('Cache-Control')

    def test_a_bare_private_still_gets_the_window(self):
        # What an offline-snapshot page sends (KAN-132): who may store it,
        # not for how long. The prefetch must stay reusable.
        def private(request):
            response = _html(request)
            response['Cache-Control'] = 'private'
            return response

        assert _run(private, Purpose='prefetch')['Cache-Control'] == EXPECTED

    def test_a_view_lifetime_wins(self):
        def own(request):
            response = _html(request)
            response['Cache-Control'] = 'private, max-age=60'
            return response

        assert _run(own, Purpose='prefetch')['Cache-Control'] == 'private, max-age=60'

    def test_a_view_that_said_never_cache_keeps_its_word(self):
        response = _run(never_cache(_html), Purpose='prefetch')
        assert 'no-cache' in response['Cache-Control']
        assert 'max-age=0' in response['Cache-Control']


class TestInstalled:
    def test_the_middleware_is_in_the_stack(self):
        assert 'ttstats.prefetch.PrefetchCacheMiddleware' in settings.MIDDLEWARE


@pytest.mark.django_db
class TestRealPages:
    """The tab destinations, through the whole stack."""

    @pytest.mark.parametrize('name', ['dashboard', 'play', 'leaderboard', 'championship_list'])
    def test_a_prefetched_tab_page_is_cacheable(self, name):
        user, _ = _verified_user_with_player()
        response = _login_client(user).get(reverse(f'pingpong:{name}'), headers={'Purpose': 'prefetch'})
        assert response.status_code == 200
        assert response['Cache-Control'] == EXPECTED

    def test_the_same_page_navigated_normally_is_not(self):
        user, _ = _verified_user_with_player()
        response = _login_client(user).get(reverse('pingpong:dashboard'))
        assert not response.has_header('Cache-Control')


@pytest.mark.django_db
class TestOptInMarkup:
    """prefetch.js only acts on links that opt in with `data-prefetch`."""

    def _body(self, user):
        return _login_client(user).get(reverse('pingpong:dashboard')).content.decode()

    def test_every_tab_opts_in(self):
        user, _ = _verified_user_with_player()
        nav = self._body(user).split('aria-label="Primary"')[1].split('</nav>')[0]
        tabs = re.findall(r'<a\b[^>]*>', nav)
        assert len(tabs) == 4
        assert all('data-prefetch' in t for t in tabs)

    def test_menu_rows_opt_in(self):
        user, _ = _verified_user_with_player()
        body = self._body(user)
        row = re.search(r'<a href="%s"[^>]*>' % reverse('pingpong:head_to_head'), body)
        assert row and 'data-prefetch' in row.group(0)

    def test_the_admin_row_opts_out(self):
        user, _ = _staff_with_player()
        body = self._body(user)
        row = re.search(r'<a href="%s"[^>]*>' % reverse('admin:index'), body)
        assert row
        assert 'data-no-prefetch' in row.group(0)
        assert 'data-prefetch ' not in row.group(0) and 'data-prefetch>' not in row.group(0)

    def test_prefetch_js_is_loaded_deferred(self):
        user, _ = _verified_user_with_player()
        tags = re.findall(r'<script\b[^>]*prefetch[^>]*>', self._body(user))
        assert len(tags) == 1
        assert re.search(r'\bdefer\b', tags[0])
