import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from pingpong.context_processors import pingpong_context
from .conftest import MatchFactory, PlayerFactory, UserFactory, GameFactory, confirm_match


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
        # Make other verified to prevent auto-confirm
        other = PlayerFactory(with_user=True)
        other.user.profile.email_verified = True
        other.user.profile.save()
        # Make p verified too
        p.user.profile.email_verified = True
        p.user.profile.save()

        # Match where user is player1 and not confirmed (no MatchConfirmation records)
        m1 = MatchFactory(player1=p, player2=other)
        # Add games to trigger winner
        GameFactory(match=m1, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m1, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=m1, game_number=3, team1_score=11, team2_score=9)
        m1.refresh_from_db()

        # Match where user is player2 and not confirmed
        m2 = MatchFactory(player1=other, player2=p)
        GameFactory(match=m2, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m2, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=m2, game_number=3, team1_score=11, team2_score=9)
        m2.refresh_from_db()

        request = self._make_request(u)
        ctx = pingpong_context(request)
        assert ctx["pending_matches_count"] == 2

    def test_confirmed_matches_not_counted(self):
        u = UserFactory()
        p = PlayerFactory(user=u)
        # Make other verified to prevent auto-confirm
        other = PlayerFactory(with_user=True)
        other.user.profile.email_verified = True
        other.user.profile.save()
        # Make p verified too
        p.user.profile.email_verified = True
        p.user.profile.save()

        # Confirmed match (all players confirmed)
        m1 = MatchFactory(player1=p, player2=other)
        GameFactory(match=m1, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m1, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=m1, game_number=3, team1_score=11, team2_score=9)
        m1.refresh_from_db()
        confirm_match(m1)  # Confirm all players

        # Unconfirmed match (no MatchConfirmation records)
        m2 = MatchFactory(player1=p, player2=other)
        GameFactory(match=m2, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m2, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=m2, game_number=3, team1_score=11, team2_score=9)
        m2.refresh_from_db()
        # Don't confirm this one

        request = self._make_request(u)
        ctx = pingpong_context(request)
        assert ctx["pending_matches_count"] == 1
