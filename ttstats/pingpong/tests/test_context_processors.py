import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from pingpong.context_processors import pingpong_context
from .conftest import MatchFactory, PlayerFactory, UserFactory


@pytest.mark.django_db
class TestPingpongContext:
    def _make_request(self, user=None):
        rf = RequestFactory()
        request = rf.get("/")
        request.user = user or AnonymousUser()
        return request

    def test_unauthenticated_zero(self):
        request = self._make_request()
        ctx = pingpong_context(request)
        assert ctx["pending_matches_count"] == 0

    def test_authenticated_no_player(self):
        u = UserFactory()
        request = self._make_request(u)
        ctx = pingpong_context(request)
        assert ctx["pending_matches_count"] == 0

    def test_authenticated_with_pending_matches(self):
        u = UserFactory()
        p = PlayerFactory(user=u)
        other = PlayerFactory(with_user=True)

        # Match where user is player1 and not confirmed
        MatchFactory(player1=p, player2=other, player1_confirmed=False)
        # Match where user is player2 and not confirmed
        MatchFactory(player1=other, player2=p, player2_confirmed=False)

        request = self._make_request(u)
        ctx = pingpong_context(request)
        assert ctx["pending_matches_count"] == 2

    def test_confirmed_matches_not_counted(self):
        u = UserFactory()
        p = PlayerFactory(user=u)
        other = PlayerFactory(with_user=True)

        # Confirmed match
        MatchFactory(player1=p, player2=other, player1_confirmed=True)
        # Unconfirmed match
        MatchFactory(player1=p, player2=other, player1_confirmed=False)

        request = self._make_request(u)
        ctx = pingpong_context(request)
        assert ctx["pending_matches_count"] == 1
