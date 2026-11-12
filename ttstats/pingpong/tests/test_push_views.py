"""Tests for the PWA endpoints and the push subscription API.

Every logged-in user here gets a Player, because base.html unconditionally
renders `user.player.pk` -- see CLAUDE.md.
"""

import json
import re

import pytest
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse

from pingpong import offline
from pingpong.models import NotificationPreference, PushSubscription

from .conftest import PlayerFactory

with_vapid = override_settings(
    VAPID_PUBLIC_KEY='test-public-key',
    VAPID_PRIVATE_KEY='test-private-key',
    VAPID_ADMIN_EMAIL='mailto:test@example.com',
)

SUBSCRIPTION_JSON = {
    'endpoint': 'https://push.example.com/abc',
    'keys': {'p256dh': 'p256dh-key', 'auth': 'auth-key'},
}


def logged_in(client):
    """A user with a Player, logged in."""
    player = PlayerFactory(with_user=True)
    client.force_login(player.user)
    return player


@pytest.mark.django_db
class TestServiceWorkerView:
    def test_served_from_the_site_root(self, client):
        # Scope is the whole point: a worker under /static/ or /pingpong/
        # could never control the rest of the site.
        assert reverse('service_worker') == '/sw.js'

    def test_served_as_javascript_without_login(self, client):
        # The worker is registered on the login page too, so it must not be
        # behind auth.
        response = client.get('/sw.js')

        assert response.status_code == 200
        assert response['Content-Type'].startswith('application/javascript')

    def test_handles_push_and_notification_clicks(self, client):
        body = client.get('/sw.js').content.decode()

        assert "addEventListener('push'" in body
        assert "addEventListener('notificationclick'" in body

    def test_fetch_handler_is_a_route_table_that_only_sees_gets(self, client):
        # KAN-96: the handler answers nothing unless a route claims it, and a
        # write is never offered to a route -- a cached reply to a POST would
        # be a lost point. pytest cannot run the worker; the e2e `iphone-pwa`
        # project does (pwa.spec.js).
        body = client.get('/sw.js').content.decode()

        assert "addEventListener('fetch'" in body
        assert 'const ROUTES = [' in body
        assert "request.method !== 'GET'" in body
        assert 'self.location.origin' in body


@pytest.mark.django_db
class TestServiceWorkerRoutes:
    """KAN-82 + KAN-133: the table holds exactly three narrow routes.

    String assertions on the rendered worker; behaviour is proved in a real
    WebKit by tests/e2e/scoreboard-offline.pwa.spec.js.
    """

    @staticmethod
    def body(client):
        return client.get('/sw.js').content.decode()

    def test_route_table_is_static_then_scoreboard_then_snapshots(self, client):
        # The scoreboard must win its own URL over the snapshot route.
        assert (
            'const ROUTES = [staticAssets, scoreboardShell, snapshotNavigation];'
            in self.body(client)
        )

    def test_snapshot_route_claims_only_pingpong_navigations(self, client):
        # Behaviour (POST, htmx, /static/, JSON, /live/ never claimed) is
        # unit-tested in tests/js/sw_snapshots.test.mjs.
        body = self.body(client)
        assert "if (request.mode !== 'navigate') return false;" in body
        assert "if (!url.pathname.startsWith('/pingpong/')) return false;" in body
        assert "headers.get('HX-Request')" in body

    def test_snapshot_caches_are_current_and_bounded(self, client):
        body = self.body(client)
        assert "const SNAP_PREFIX = 'ttstats-snap-' + BUILD + '-';" in body
        assert 'name.startsWith(SNAP_PREFIX)' in body
        assert 'const SNAP_MAX = 60;' in body
        assert 'const SNAP_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;' in body
        assert 'const NAV_TIMEOUT_MS = 4000;' in body

    def test_scoreboard_route_claims_only_live_scoreboard_navigations(self, client):
        body = self.body(client)
        assert r"const SCOREBOARD = /^\/pingpong\/matches\/\d+\/live\/$/;" in body
        assert "request.mode === 'navigate' && SCOREBOARD.test(url.pathname)" in body

    def test_scoreboard_pattern_rejects_its_json_and_post_siblings(self, client):
        pattern = re.compile(r'^/pingpong/matches/\d+/live/$')
        assert pattern.match('/pingpong/matches/12/live/')
        for path in ('state/', 'sync/', 'point/', 'undo/', 'start/', 'side-switch/'):
            assert not pattern.match(f'/pingpong/matches/12/live/{path}')
        assert not pattern.match('/pingpong/leaderboard/')

    def test_static_route_claims_only_hashed_files(self, client):
        # Unhashed /static/ (dev) must never be cached: it changes in place.
        body = self.body(client)
        assert r"const HASHED_STATIC = /^\/static\/.+\.[0-9a-f]{12}\.[A-Za-z0-9]+$/;" in body
        pattern = re.compile(r'^/static/.+\.[0-9a-f]{12}\.[A-Za-z0-9]+$')
        assert pattern.match('/static/pingpong/css/app.0123456789ab.css')
        assert not pattern.match('/static/pingpong/css/app.css')

    def test_posts_never_reach_a_route(self, client):
        body = self.body(client)
        # The GET guard sits in front of the table and returns before any route.
        guard = body.index("if (request.method !== 'GET') return null;")
        assert guard < body.index('ROUTES.find(')

    def test_version_bumped_and_build_keyed_on_the_precache(self, client):
        body = self.body(client)
        urls = offline.precache_urls()
        assert "const VERSION = 'ttstats-v4';" in body
        assert f"const BUILD = VERSION + '-{offline.build_key(urls)}';" in body
        assert f'const PRECACHE = {json.dumps(urls)};' in body

    def test_activate_deletes_caches_from_other_builds(self, client):
        body = self.body(client)
        assert "name.startsWith('ttstats-') && !isCurrentCache(name)" in body
        assert 'caches.delete(name)' in body

    def test_caches_are_bounded(self, client):
        body = self.body(client)
        assert 'trim(cache, STATIC_MAX)' in body
        assert 'trim(cache, LIVE_MAX)' in body
        assert 'LIVE_MAX_AGE_MS' in body

    def test_redirects_evict_and_only_identified_200s_are_stored(self, client):
        body = self.body(client)
        assert "response.type === 'opaqueredirect'" in body
        assert 'await evictScoreboard(key);' in body
        assert 'response.status === 200' in body
        assert 'IDENTITY.test(identity)' in body

    def test_documents_and_handles_the_page_messages(self, client):
        body = self.body(client)
        for message in ('identity', 'purge', 'live-match-complete'):
            assert f"{{type: '{message}'" in body, f'{message} undocumented in the header'
            assert f"data.type === '{message}'" in body
        # And the one it sends back to a page served a saved copy.
        assert "{type: 'snapshot-fresh', url}" in body
        assert "postMessage({ type: 'snapshot-fresh', url: url.href })" in body


@pytest.mark.django_db
class TestManifestView:
    def test_served_from_the_site_root(self):
        assert reverse('manifest') == '/manifest.webmanifest'

    def test_is_valid_json_with_the_required_install_fields(self, client):
        response = client.get('/manifest.webmanifest')

        assert response.status_code == 200
        assert response['Content-Type'].startswith('application/manifest+json')
        manifest = json.loads(response.content)
        assert manifest['name']
        assert manifest['start_url'] == reverse('pingpong:dashboard')
        assert manifest['display'] == 'standalone'

    def test_ships_both_icon_sizes_and_a_maskable_one(self):
        from django.test import Client

        manifest = json.loads(Client().get('/manifest.webmanifest').content)
        sizes = {icon['sizes'] for icon in manifest['icons']}
        purposes = {icon['purpose'] for icon in manifest['icons']}

        assert {'192x192', '512x512'} <= sizes
        assert 'maskable' in purposes

    def test_icon_urls_go_through_static(self, client):
        # Hardcoded /static/ paths would break under prod's hashing manifest
        # storage, where every filename carries a content hash.
        manifest = json.loads(client.get('/manifest.webmanifest').content)

        for icon in manifest['icons']:
            assert icon['src'].startswith('/static/')

    def test_scope_covers_the_whole_site(self, client):
        manifest = json.loads(client.get('/manifest.webmanifest').content)

        assert manifest['scope'] == '/'


@pytest.mark.django_db
class TestPushSubscribeView:
    def test_requires_login(self, client):
        response = client.post(
            reverse('pingpong:push_subscribe'),
            data=json.dumps(SUBSCRIPTION_JSON),
            content_type='application/json',
        )

        assert response.status_code == 302

    def test_stores_the_subscription(self, client):
        player = logged_in(client)

        response = client.post(
            reverse('pingpong:push_subscribe'),
            data=json.dumps(SUBSCRIPTION_JSON),
            content_type='application/json',
        )

        assert response.status_code == 200
        assert response.json()['created'] is True
        sub = PushSubscription.objects.get(user=player.user)
        assert sub.endpoint == SUBSCRIPTION_JSON['endpoint']
        assert sub.p256dh == 'p256dh-key'

    def test_resubscribing_updates_instead_of_duplicating(self, client):
        # push.js re-asserts the subscription on every page load. Creating a
        # row each time would deliver every notification N times.
        player = logged_in(client)
        url = reverse('pingpong:push_subscribe')

        client.post(url, data=json.dumps(SUBSCRIPTION_JSON),
                    content_type='application/json')
        rotated = {**SUBSCRIPTION_JSON,
                   'keys': {'p256dh': 'new-p256dh', 'auth': 'new-auth'}}
        response = client.post(url, data=json.dumps(rotated),
                               content_type='application/json')

        assert response.json()['created'] is False
        assert PushSubscription.objects.filter(user=player.user).count() == 1
        assert PushSubscription.objects.get().p256dh == 'new-p256dh'

    def test_resubscribing_clears_the_failure_count(self, client):
        player = logged_in(client)
        PushSubscription.objects.create(
            user=player.user, endpoint=SUBSCRIPTION_JSON['endpoint'],
            p256dh='old', auth='old', failure_count=7,
        )

        client.post(
            reverse('pingpong:push_subscribe'),
            data=json.dumps(SUBSCRIPTION_JSON),
            content_type='application/json',
        )

        assert PushSubscription.objects.get().failure_count == 0

    def test_records_the_user_agent(self, client):
        player = logged_in(client)

        client.post(
            reverse('pingpong:push_subscribe'),
            data=json.dumps(SUBSCRIPTION_JSON),
            content_type='application/json',
            HTTP_USER_AGENT='Mozilla/5.0 (iPhone)',
        )

        assert 'iPhone' in PushSubscription.objects.get().user_agent

    @pytest.mark.parametrize('payload', [
        {},
        {'endpoint': 'https://push.example.com/abc'},
        {'endpoint': 'https://push.example.com/abc', 'keys': {'p256dh': 'x'}},
        {'keys': {'p256dh': 'x', 'auth': 'y'}},
    ])
    def test_rejects_an_incomplete_subscription(self, client, payload):
        logged_in(client)

        response = client.post(
            reverse('pingpong:push_subscribe'),
            data=json.dumps(payload),
            content_type='application/json',
        )

        assert response.status_code == 400
        assert PushSubscription.objects.count() == 0

    def test_rejects_malformed_json(self, client):
        logged_in(client)

        response = client.post(
            reverse('pingpong:push_subscribe'),
            data='not json at all',
            content_type='application/json',
        )

        assert response.status_code == 400


@pytest.mark.django_db
class TestPushUnsubscribeView:
    def test_deletes_the_subscription(self, client):
        player = logged_in(client)
        PushSubscription.objects.create(
            user=player.user, endpoint=SUBSCRIPTION_JSON['endpoint'],
            p256dh='k', auth='a',
        )

        response = client.post(
            reverse('pingpong:push_unsubscribe'),
            data=json.dumps({'endpoint': SUBSCRIPTION_JSON['endpoint']}),
            content_type='application/json',
        )

        assert response.status_code == 200
        assert PushSubscription.objects.count() == 0

    def test_is_idempotent(self, client):
        # The client calls this after the browser has already discarded its
        # own subscription, so the two can legitimately disagree.
        logged_in(client)

        response = client.post(
            reverse('pingpong:push_unsubscribe'),
            data=json.dumps({'endpoint': 'https://push.example.com/gone'}),
            content_type='application/json',
        )

        assert response.status_code == 200
        assert response.json()['deleted'] == 0

    def test_cannot_delete_another_users_subscription(self, client):
        logged_in(client)
        other = PlayerFactory(with_user=True)
        PushSubscription.objects.create(
            user=other.user, endpoint='https://push.example.com/other',
            p256dh='k', auth='a',
        )

        client.post(
            reverse('pingpong:push_unsubscribe'),
            data=json.dumps({'endpoint': 'https://push.example.com/other'}),
            content_type='application/json',
        )

        assert PushSubscription.objects.filter(user=other.user).exists()

    def test_requires_login(self, client):
        response = client.post(
            reverse('pingpong:push_unsubscribe'),
            data=json.dumps({}),
            content_type='application/json',
        )

        assert response.status_code == 302


@pytest.mark.django_db
class TestPushTestView:
    @with_vapid
    def test_sends_to_the_current_user(self, client):
        player = logged_in(client)
        PushSubscription.objects.create(
            user=player.user, endpoint=SUBSCRIPTION_JSON['endpoint'],
            p256dh='k', auth='a',
        )

        with patch('pywebpush.webpush') as mock_webpush:
            response = client.post(reverse('pingpong:push_test'))

        assert response.status_code == 200
        assert response.json() == {'ok': True, 'delivered': 1}
        assert mock_webpush.called

    @with_vapid
    def test_reports_failure_when_nothing_was_delivered(self, client):
        logged_in(client)

        response = client.post(reverse('pingpong:push_test'))

        assert response.json()['ok'] is False

    @with_vapid
    def test_ignores_muted_preferences(self, client):
        # The user just pressed the button; they want this one regardless.
        player = logged_in(client)
        PushSubscription.objects.create(
            user=player.user, endpoint=SUBSCRIPTION_JSON['endpoint'],
            p256dh='k', auth='a',
        )
        prefs = NotificationPreference.for_user(player.user)
        prefs.push_match_result = False
        prefs.save()

        with patch('pywebpush.webpush'):
            response = client.post(reverse('pingpong:push_test'))

        assert response.json()['delivered'] == 1

    def test_requires_login(self, client):
        assert client.post(reverse('pingpong:push_test')).status_code == 302


@pytest.mark.django_db
class TestNotificationSettingsView:
    def test_requires_login(self, client):
        assert client.get(reverse('pingpong:notification_settings')).status_code == 302

    def test_renders_every_kind_as_a_toggle(self, client):
        from pingpong.models import NotificationKind

        logged_in(client)

        response = client.get(reverse('pingpong:notification_settings'))

        assert response.status_code == 200
        content = response.content.decode()
        for kind in NotificationKind:
            assert f'name="{kind.value}"' in content
            assert str(kind.label) in content

    def test_every_kind_has_a_preference_field(self):
        # Without its `push_<kind>` field a toggle renders but its save is
        # silently dropped (setattr on a non-field), so the kind is unmutable.
        field_names = {f.name for f in NotificationPreference._meta.get_fields()}
        from pingpong.models import NotificationKind

        missing = [k.value for k in NotificationKind if f'push_{k.value}' not in field_names]
        assert missing == []

    def test_season_ended_toggle_saves(self, client):
        player = logged_in(client)
        assert NotificationPreference.for_user(player.user).push_season_ended is True

        client.post(
            reverse('pingpong:notification_settings'),
            data={'match_confirmation': 'on'},
        )

        prefs = NotificationPreference.for_user(player.user)
        assert prefs.push_season_ended is False

        client.post(
            reverse('pingpong:notification_settings'),
            data={'season_ended': 'on'},
        )
        prefs.refresh_from_db()
        assert prefs.push_season_ended is True

    def test_never_leaks_the_private_key(self, client):
        logged_in(client)

        with override_settings(VAPID_PUBLIC_KEY='pub-key',
                               VAPID_PRIVATE_KEY='SECRET-PRIVATE-KEY'):
            response = client.get(reverse('pingpong:notification_settings'))

        assert b'SECRET-PRIVATE-KEY' not in response.content

    def test_saving_turns_off_the_unchecked_boxes(self, client):
        # An unchecked checkbox posts nothing, so absence has to mean off.
        # That only works because the form posts every toggle every time.
        player = logged_in(client)
        prefs = NotificationPreference.for_user(player.user)
        assert prefs.push_match_result is True

        response = client.post(
            reverse('pingpong:notification_settings'),
            data={'match_confirmation': 'on'},
        )

        assert response.status_code == 302
        prefs.refresh_from_db()
        assert prefs.push_match_confirmation is True
        assert prefs.push_match_result is False
        assert prefs.push_scheduled_match is False

    def test_saving_turns_on_a_checked_box(self, client):
        player = logged_in(client)

        client.post(
            reverse('pingpong:notification_settings'),
            data={'leaderboard_overtake': 'on'},
        )

        prefs = NotificationPreference.for_user(player.user)
        assert prefs.push_leaderboard_overtake is True

    def test_lists_registered_devices(self, client):
        player = logged_in(client)
        PushSubscription.objects.create(
            user=player.user, endpoint=SUBSCRIPTION_JSON['endpoint'],
            p256dh='k', auth='a', user_agent='Mozilla/5.0 (iPhone)',
        )

        response = client.get(reverse('pingpong:notification_settings'))

        assert b'iPhone' in response.content


@pytest.mark.django_db
class TestPushConfigContext:
    def test_exposes_only_the_public_key(self, client):
        logged_in(client)

        with override_settings(VAPID_PUBLIC_KEY='pub-key',
                               VAPID_PRIVATE_KEY='SECRET'):
            response = client.get(reverse('pingpong:dashboard'))

        config = response.context['push_config']
        assert config['vapidPublicKey'] == 'pub-key'
        assert 'SECRET' not in json.dumps(config)

    def test_disabled_without_keys(self, client):
        logged_in(client)

        with override_settings(VAPID_PUBLIC_KEY='', VAPID_PRIVATE_KEY=''):
            response = client.get(reverse('pingpong:dashboard'))

        assert response.context['push_config']['enabled'] is False

    def test_present_for_anonymous_users_too(self, client):
        # pingpong_context early-returns for anyone without a Player; this
        # must not go missing on the same pages.
        response = client.get(reverse('pingpong:login'))

        assert response.context['push_config']['urls']['serviceWorker'] == '/sw.js'


@pytest.mark.django_db
class TestLoggedOutInstallSurface:
    """The logged-out pages must be installable too.

    registration/base_auth.html deliberately does not extend pingpong/base.html,
    so it does not inherit the PWA tags -- and a new user's first visit lands
    here, which is exactly where they would tap Add to Home Screen. Without the
    manifest they get a plain bookmark, and on iOS a non-standalone bookmark can
    never enable push at all.
    """

    @pytest.mark.parametrize('url_name', [
        'pingpong:login',
        'pingpong:signup',
    ])
    def test_auth_pages_link_the_manifest(self, client, url_name):
        response = client.get(reverse(url_name))

        assert response.status_code == 200
        content = response.content.decode()
        assert 'rel="manifest"' in content
        assert reverse('manifest') in content

    def test_auth_pages_carry_the_ios_install_tags(self, client):
        content = client.get(reverse('pingpong:login')).content.decode()

        assert 'apple-mobile-web-app-capable' in content
        assert 'apple-touch-icon' in content

    def test_carries_a_csrf_token_because_the_cookie_is_httponly(self, client):
        logged_in(client)

        response = client.get(reverse('pingpong:dashboard'))

        assert response.context['push_config']['csrfToken']
