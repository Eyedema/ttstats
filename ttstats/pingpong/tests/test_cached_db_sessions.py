"""Sessions under `cached_db`, the engine production uses (KAN-123 / S10.3).

Production reads the session from Redis and falls back to the database, which
saves a query per request. Everything that keeps state in the session has to
keep working through that: password login, the passkey ceremony's challenge,
and the club switcher's active club. And because the cache is a cache, losing
it -- `cache_control --clear`, a Redis restart -- must not log anyone out.

Tests build their own `Client()` after switching the engine: SessionMiddleware
binds its SessionStore when the handler loads, so a client created first would
keep the plain `db` engine.
"""
import pytest
from django.contrib.sessions.backends.cached_db import SessionStore
from django.contrib.sessions.models import Session
from django.core.cache import caches
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from ttstats.middleware import (
    ACTIVE_CLUB_SESSION_KEY,
    REQUEST_LOCAL_ATTRS,
    _thread_locals,
)

from .conftest import ClubFactory, ClubMembershipFactory, PlayerFactory, UserFactory

CACHED_DB = 'django.contrib.sessions.backends.cached_db'
DASHBOARD = reverse('pingpong:dashboard')
LOGIN = reverse('pingpong:login')


@pytest.fixture(autouse=True)
def _clean_context():
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)
    yield
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)


@pytest.fixture(autouse=True)
def cached_db(settings):
    settings.SESSION_ENGINE = CACHED_DB


def _session_key(client):
    return client.cookies['sessionid'].value


def _in_cache(key):
    from django.conf import settings

    return caches[settings.SESSION_CACHE_ALIAS].get(SessionStore(key).cache_key) is not None


def _verified_user(username='alice'):
    player = PlayerFactory(with_user=True)
    user = player.user
    user.username = username
    user.save()
    user.profile.email_verified = True
    user.profile.save()
    return user


def _password_login(client, username='alice'):
    response = client.post(LOGIN, {'username': username, 'password': 'testpass123'})
    assert response.status_code == 302
    assert response.url == DASHBOARD


@pytest.mark.django_db
class TestPasswordLogin:
    def test_login_writes_the_session_to_cache_and_database(self):
        _verified_user()
        client = Client()

        _password_login(client)

        key = _session_key(client)
        assert _in_cache(key)
        assert Session.objects.filter(session_key=key).exists()

    def test_the_session_authenticates_later_requests(self):
        _verified_user()
        client = Client()
        _password_login(client)

        assert client.get(DASHBOARD).status_code == 200

    def test_logout_removes_it_from_both(self):
        _verified_user()
        client = Client()
        _password_login(client)
        key = _session_key(client)

        client.post(reverse('logout'))

        assert not _in_cache(key)
        assert not Session.objects.filter(session_key=key).exists()


@pytest.mark.django_db
class TestLosingTheCacheLogsNobodyOut:
    """`cache_control --clear` calls `cache.clear()` -- in Redis, FLUSHDB on the
    one database the sessions share with every other key. `cached_db` treats
    the database as the record, so the next read misses, falls back and
    re-fills the cache."""

    def test_cache_control_clear_keeps_the_user_logged_in(self):
        _verified_user()
        client = Client()
        _password_login(client)
        key = _session_key(client)

        call_command('cache_control', '--clear')
        assert not _in_cache(key)

        assert client.get(DASHBOARD).status_code == 200
        assert _in_cache(key)


@pytest.mark.django_db
class TestPasskeyCeremonyState:
    """The WebAuthn challenge lives in the session between `begin` and
    `complete`. If it is lost the ceremony fails, so it must survive a cache
    miss in between."""

    BEGIN = reverse('otp_webauthn:credential-registration-begin')
    STATE_KEY = 'otp_webauthn_register_state'

    def test_the_registration_challenge_is_stored(self):
        user = _verified_user()
        client = Client()
        client.force_login(user)

        response = client.post(self.BEGIN)

        assert response.status_code == 200
        assert self.STATE_KEY in SessionStore(_session_key(client)).load()

    def test_the_challenge_survives_a_cache_flush(self):
        user = _verified_user()
        client = Client()
        client.force_login(user)
        client.post(self.BEGIN)
        challenge = SessionStore(_session_key(client)).load()[self.STATE_KEY]

        call_command('cache_control', '--clear')

        assert SessionStore(_session_key(client)).load()[self.STATE_KEY] == challenge

    def test_the_login_page_ceremony_works_anonymously(self):
        """The authentication ceremony starts before anyone is logged in, so
        it is what creates the session."""
        client = Client()

        response = client.post(reverse('otp_webauthn:credential-authentication-begin'))

        assert response.status_code == 200
        assert 'otp_webauthn_authentication_state' in SessionStore(_session_key(client)).load()


@pytest.mark.django_db
class TestClubSwitching:
    @pytest.fixture(autouse=True)
    def clubs_on(self, settings):
        settings.FEATURE_FLAGS = {'clubs': True, 'tenancy_canary': False}

    def _two_club_member(self):
        alpha, zulu = ClubFactory(name='Alpha'), ClubFactory(name='Zulu')
        user = UserFactory()
        PlayerFactory(user=user, club=alpha)
        ClubMembershipFactory(club=alpha, user=user)
        ClubMembershipFactory(club=zulu, user=user)
        client = Client()
        client.force_login(user)
        return client, zulu

    def test_the_choice_is_kept(self):
        client, zulu = self._two_club_member()

        client.post(reverse('pingpong:club_switch'), {'club_id': zulu.pk})

        assert client.session[ACTIVE_CLUB_SESSION_KEY] == zulu.pk
        body = client.get(DASHBOARD).content.decode()
        assert 'aria-current="true"' in body

    def test_the_choice_survives_a_cache_flush(self):
        client, zulu = self._two_club_member()
        client.post(reverse('pingpong:club_switch'), {'club_id': zulu.pk})

        call_command('cache_control', '--clear')

        assert client.get(DASHBOARD).status_code == 200
        assert SessionStore(_session_key(client)).load()[ACTIVE_CLUB_SESSION_KEY] == zulu.pk
