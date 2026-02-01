import pytest
from datetime import date, timedelta, time
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from pingpong.models import EloHistory, Game, Match, Player, ScheduledMatch
from .conftest import (
    GameFactory,
    LocationFactory,
    MatchFactory,
    PlayerFactory,
    ScheduledMatchFactory,
    UserFactory,
    confirm_match,
    confirm_team,
    get_match_players,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _verified_user_with_player():
    """Create a verified user with a linked player profile."""
    u = UserFactory()
    u.profile.email_verified = True
    u.profile.save()
    p = PlayerFactory(user=u)
    return u, p


def _staff_with_player():
    """Create a verified staff user with a player profile (needed for template rendering)."""
    u = UserFactory(is_staff=True)
    u.profile.email_verified = True
    u.profile.save()
    p = PlayerFactory(user=u)
    return u, p


def _login_client(user):
    c = Client()
    c.force_login(user)
    return c


# ===========================================================================
# DashboardView
# ===========================================================================

@pytest.mark.django_db
class TestDashboardView:
    def test_authenticated_access(self):
        u, _ = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:dashboard"))
        assert resp.status_code == 200
        assert "total_players" in resp.context
        assert "total_matches" in resp.context
        assert "recent_matches" in resp.context

    def test_unauthenticated_redirect(self):
        c = Client()
        resp = c.get(reverse("pingpong:dashboard"))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_template(self):
        u, _ = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:dashboard"))
        assert "pingpong/dashboard.html" in [t.name for t in resp.templates]


# ===========================================================================
# PlayerListView
# ===========================================================================

@pytest.mark.django_db
class TestPlayerListView:
    def test_authenticated_access(self):
        u, _ = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:player_list"))
        assert resp.status_code == 200

    def test_unauthenticated_redirect(self):
        resp = Client().get(reverse("pingpong:player_list"))
        assert resp.status_code == 302

    def test_pagination(self):
        u, _ = _verified_user_with_player()
        # Create 15 players total (1 from the verified user + 14 more)
        for i in range(14):
            PlayerFactory(name=f"Extra {i:02d}")
        c = _login_client(u)
        resp = c.get(reverse("pingpong:player_list"))
        assert resp.context["is_paginated"] is True


# ===========================================================================
# PlayerDetailView
# ===========================================================================

@pytest.mark.django_db
class TestPlayerDetailView:
    def test_access(self):
        u, p = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:player_detail", args=[p.pk]))
        assert resp.status_code == 200
        assert "total_matches" in resp.context

    def test_no_matches_case(self):
        u, p = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:player_detail", args=[p.pk]))
        assert resp.context["total_matches"] == 0
        assert resp.context["wins"] == 0
        assert resp.context["losses"] == 0

    def test_streak_calculation(self):
        u, p = _verified_user_with_player()
        other = PlayerFactory(with_user=True)
        # 2 confirmed wins
        for i in range(2):
            m = MatchFactory(player1=p, player2=other)
            # Add games to make player1/team1 win
            GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
            GameFactory(match=m, game_number=2, team1_score=11, team2_score=9)
            GameFactory(match=m, game_number=3, team1_score=11, team2_score=7)
            m.refresh_from_db()
            # Confirm the match
            confirm_match(m)

        c = _login_client(u)
        resp = c.get(reverse("pingpong:player_detail", args=[p.pk]))
        assert resp.context["wins"] == 2
        assert resp.context["current_streak"] == 2
        assert resp.context["streak_type"] == "win"


# ===========================================================================
# PlayerCreateView
# ===========================================================================

@pytest.mark.django_db
class TestPlayerCreateView:
    def test_get_form(self):
        u, _ = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:player_add"))
        assert resp.status_code == 200

    def test_post_valid(self):
        u, _ = _verified_user_with_player()
        c = _login_client(u)
        resp = c.post(reverse("pingpong:player_add"), {
            "name": "New Player",
            "nickname": "",
            "playing_style": "normal",
            "notes": "",
        })
        assert resp.status_code == 302
        assert Player.objects.filter(name="New Player").exists()

    def test_post_invalid(self):
        u, _ = _verified_user_with_player()
        c = _login_client(u)
        resp = c.post(reverse("pingpong:player_add"), {
            "name": "",  # required
        })
        assert resp.status_code == 200  # re-renders form


# ===========================================================================
# PlayerUpdateView
# ===========================================================================

@pytest.mark.django_db
class TestPlayerUpdateView:
    def test_get_form(self):
        u, p = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:player_edit", args=[p.pk]))
        assert resp.status_code == 200

    def test_post_valid(self):
        u, p = _verified_user_with_player()
        c = _login_client(u)
        resp = c.post(reverse("pingpong:player_edit", args=[p.pk]), {
            "name": "Updated Name",
            "nickname": "",
            "playing_style": "normal",
            "notes": "",
        })
        assert resp.status_code == 302
        p.refresh_from_db()
        assert p.name == "Updated Name"


# ===========================================================================
# MatchListView
# ===========================================================================

@pytest.mark.django_db
class TestMatchListView:
    def test_authenticated_access(self):
        u, p = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:match_list"))
        assert resp.status_code == 200

    def test_unauthenticated_redirect(self):
        resp = Client().get(reverse("pingpong:match_list"))
        assert resp.status_code == 302


# ===========================================================================
# MatchDetailView
# ===========================================================================

@pytest.mark.django_db
class TestMatchDetailView:
    def test_access(self):
        u, p = _verified_user_with_player()
        other = PlayerFactory(with_user=True)
        m = MatchFactory(player1=p, player2=other)
        c = _login_client(u)
        resp = c.get(reverse("pingpong:match_detail", args=[m.pk]))
        assert resp.status_code == 200

    def test_404(self):
        u, _ = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:match_detail", args=[99999]))
        assert resp.status_code == 404


# ===========================================================================
# MatchCreateView
# ===========================================================================

@pytest.mark.django_db
class TestMatchCreateView:
    def test_staff_full_form(self):
        staff, _ = _staff_with_player()
        c = _login_client(staff)
        resp = c.get(reverse("pingpong:match_add"))
        assert resp.status_code == 200
        assert resp.context["is_staff"] is True

    def test_non_staff_player1_locked(self):
        u, p = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:match_add"))
        assert resp.status_code == 200
        form = resp.context["form"]
        assert form.fields["player1"].disabled is True

    def test_non_staff_forced_as_player1(self):
        u, p = _verified_user_with_player()
        other = PlayerFactory(with_user=True)
        c = _login_client(u)
        resp = c.post(reverse("pingpong:match_add"), {
            "player1": p.pk,
            "player2": other.pk,
            "date_played": timezone.now().strftime("%Y-%m-%dT%H:%M"),
            "match_type": "casual",
            "best_of": 5,
            "notes": "",
        })
        assert resp.status_code == 302
        # Check that a match exists where player p is in team1
        match = Match.objects.filter(team1__players=p, team2__players=other).first()
        assert match is not None

    def test_same_player_rejection(self):
        staff, _ = _staff_with_player()
        p = PlayerFactory()
        c = _login_client(staff)
        resp = c.post(reverse("pingpong:match_add"), {
            "player1": p.pk,
            "player2": p.pk,
            "date_played": timezone.now().strftime("%Y-%m-%dT%H:%M"),
            "match_type": "casual",
            "best_of": 5,
            "notes": "",
        })
        assert resp.status_code == 200  # re-renders form with error


# ===========================================================================
# MatchUpdateView
# ===========================================================================

@pytest.mark.django_db
class TestMatchUpdateView:
    def test_incomplete_match_uses_match_form(self):
        u, p = _verified_user_with_player()
        other = PlayerFactory(with_user=True)
        m = MatchFactory(player1=p, player2=other)
        c = _login_client(u)
        resp = c.get(reverse("pingpong:match_edit", args=[m.pk]))
        assert resp.status_code == 200
        assert resp.context.get("is_complete") is False

    def test_completed_match_uses_edit_form(self):
        u, p = _verified_user_with_player()
        other = PlayerFactory(with_user=True)
        m = MatchFactory(player1=p, player2=other, best_of=3)
        GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m, game_number=2, team1_score=11, team2_score=9)
        m.refresh_from_db()
        assert m.winner is not None

        c = _login_client(u)
        resp = c.get(reverse("pingpong:match_edit", args=[m.pk]))
        assert resp.status_code == 200
        assert resp.context.get("is_complete") is True


# ===========================================================================
# GameCreateView
# ===========================================================================

@pytest.mark.django_db
class TestGameCreateView:
    def test_completed_match_redirect(self):
        u, p = _verified_user_with_player()
        other = PlayerFactory(with_user=True)
        m = MatchFactory(player1=p, player2=other, best_of=3)
        GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m, game_number=2, team1_score=11, team2_score=9)
        m.refresh_from_db()

        c = _login_client(u)
        resp = c.get(reverse("pingpong:game_add", args=[m.pk]))
        assert resp.status_code == 302

    def test_auto_game_number(self):
        u, p = _verified_user_with_player()
        other = PlayerFactory(with_user=True)
        m = MatchFactory(player1=p, player2=other)
        c = _login_client(u)
        resp = c.get(reverse("pingpong:game_add", args=[m.pk]))
        assert resp.context["next_game_number"] == 1

    def test_add_game(self):
        u, p = _verified_user_with_player()
        other = PlayerFactory(with_user=True)
        m = MatchFactory(player1=p, player2=other)
        c = _login_client(u)
        resp = c.post(reverse("pingpong:game_add", args=[m.pk]), {
            "game_number": 1,
            "team1_score": 11,
            "team2_score": 5,
        })
        assert resp.status_code == 302
        assert m.games.count() == 1

    def test_add_another_redirect(self):
        u, p = _verified_user_with_player()
        other = PlayerFactory(with_user=True)
        m = MatchFactory(player1=p, player2=other)
        c = _login_client(u)
        resp = c.post(reverse("pingpong:game_add", args=[m.pk]), {
            "game_number": 1,
            "team1_score": 11,
            "team2_score": 5,
            "add_another": "true",
        })
        assert resp.status_code == 302
        assert f"/matches/{m.pk}/add-game/" in resp.url

    def test_match_completion_flow(self):
        u, p = _verified_user_with_player()
        other = PlayerFactory(with_user=True)
        m = MatchFactory(player1=p, player2=other, best_of=3)
        GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
        c = _login_client(u)
        # This second game should complete the match
        resp = c.post(reverse("pingpong:game_add", args=[m.pk]), {
            "game_number": 2,
            "team1_score": 11,
            "team2_score": 9,
        })
        assert resp.status_code == 302
        assert f"/matches/{m.pk}/" in resp.url
        m.refresh_from_db()
        # Winner is now a Team, and p is in team1
        assert m.winner == m.team1
        assert p in m.winner.players.all()


# ===========================================================================
# LeaderboardView
# ===========================================================================

@pytest.mark.django_db
class TestLeaderboardView:
    def test_authenticated_access(self):
        u, _ = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:leaderboard"))
        assert resp.status_code == 200
        assert "player_stats" in resp.context

    def test_only_confirmed_matches(self):
        u, p = _verified_user_with_player()
        # Make other player verified to prevent auto-confirm
        other_user = UserFactory()
        other_user.profile.email_verified = True
        other_user.profile.save()
        other = PlayerFactory(user=other_user)

        # Confirmed match where p (in team1) wins
        m = MatchFactory(player1=p, player2=other)
        GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=m, game_number=3, team1_score=11, team2_score=9)
        m.refresh_from_db()
        confirm_match(m)

        # Unconfirmed match (both players are verified, so won't auto-confirm)
        m2 = MatchFactory(player1=p, player2=other)
        GameFactory(match=m2, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m2, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=m2, game_number=3, team1_score=11, team2_score=9)
        m2.refresh_from_db()
        # Don't confirm m2

        c = _login_client(u)
        resp = c.get(reverse("pingpong:leaderboard"))
        stats = resp.context["player_stats"]
        p_stat = next(s for s in stats if s["player"] == p)
        # Only the confirmed match should count
        assert p_stat["total_matches"] == 1
        assert p_stat["wins"] == 1

    def test_sorting(self):
        u, p = _verified_user_with_player()
        other = PlayerFactory(with_user=True)

        m = MatchFactory(player1=p, player2=other)
        GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=m, game_number=3, team1_score=11, team2_score=9)
        m.refresh_from_db()
        confirm_match(m)

        c = _login_client(u)
        resp = c.get(reverse("pingpong:leaderboard"))
        stats = resp.context["player_stats"]
        # p should be first (100% win rate)
        assert stats[0]["player"] == p


# ===========================================================================
# HeadToHeadStatsView
# ===========================================================================

@pytest.mark.django_db
class TestHeadToHeadStatsView:
    def test_no_players_selected(self):
        u, _ = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:head_to_head"))
        assert resp.status_code == 200
        assert "has_data" not in resp.context

    def test_both_players_selected_with_data(self):
        u, p1 = _verified_user_with_player()
        p2 = PlayerFactory(with_user=True)

        m = MatchFactory(player1=p1, player2=p2)
        GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m, game_number=2, team1_score=11, team2_score=9)
        GameFactory(match=m, game_number=3, team1_score=11, team2_score=7)
        m.refresh_from_db()
        confirm_match(m)

        c = _login_client(u)
        resp = c.get(reverse("pingpong:head_to_head"), {"player1": p1.pk, "player2": p2.pk})
        assert resp.status_code == 200
        assert resp.context["has_data"] is True
        assert resp.context["total_matches"] == 1

    def test_no_matches_between(self):
        u, p1 = _verified_user_with_player()
        p2 = PlayerFactory(with_user=True)
        c = _login_client(u)
        resp = c.get(reverse("pingpong:head_to_head"), {"player1": p1.pk, "player2": p2.pk})
        assert resp.context["has_data"] is False


# ===========================================================================
# PlayerRegistrationView
# ===========================================================================

@pytest.mark.django_db
class TestPlayerRegistrationView:
    def test_successful_signup(self):
        c = Client()
        resp = c.post(reverse("pingpong:signup"), {
            "username": "newbie",
            "email": "newbie@example.com",
            "password1": "Str0ngP@ssw0rd!",
            "password2": "Str0ngP@ssw0rd!",
            "full_name": "New Player",
            "nickname": "",
            "playing_style": "normal",
        })
        assert resp.status_code == 200  # renders verify_email_sent template
        assert Player.objects.filter(name="New Player").exists()

    def test_logged_in_redirect(self):
        u, _ = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:signup"))
        assert resp.status_code == 302

    def test_verification_email_sent(self):
        mail.outbox.clear()
        c = Client()
        c.post(reverse("pingpong:signup"), {
            "username": "emailtest",
            "email": "emailtest@example.com",
            "password1": "Str0ngP@ssw0rd!",
            "password2": "Str0ngP@ssw0rd!",
            "full_name": "Email Test",
            "nickname": "",
            "playing_style": "normal",
        })
        assert len(mail.outbox) == 1
        assert "verify" in mail.outbox[0].body.lower()


# ===========================================================================
# CustomLoginView
# ===========================================================================

@pytest.mark.django_db
class TestCustomLoginView:
    def test_verified_user_login(self):
        u = UserFactory(username="verified_login")
        u.profile.email_verified = True
        u.profile.save()
        c = Client()
        resp = c.post("/accounts/login/", {
            "username": "verified_login",
            "password": "testpass123",
        })
        assert resp.status_code == 302
        assert "/pingpong/" in resp.url

    def test_unverified_user_blocked(self):
        u = UserFactory(username="unverified_login")
        u.profile.email_verified = False
        u.profile.save()
        c = Client()
        resp = c.post("/accounts/login/", {
            "username": "unverified_login",
            "password": "testpass123",
        })
        # Redirects back to login
        assert resp.status_code == 302
        assert "login" in resp.url

    def test_already_authenticated_redirect(self):
        u = UserFactory()
        u.profile.email_verified = True
        u.profile.save()
        c = _login_client(u)
        resp = c.get("/accounts/login/")
        assert resp.status_code == 302


# ===========================================================================
# EmailVerifyView
# ===========================================================================

@pytest.mark.django_db
class TestEmailVerifyView:
    def test_valid_token(self):
        u = UserFactory()
        token = u.profile.email_verification_token
        c = Client()
        resp = c.get(reverse("pingpong:email_verify", args=[token]))
        assert resp.status_code == 302
        u.profile.refresh_from_db()
        assert u.profile.email_verified is True

    def test_invalid_token(self):
        c = Client()
        resp = c.get(reverse("pingpong:email_verify", args=["badtoken123"]))
        assert resp.status_code == 302

    def test_already_verified(self):
        u = UserFactory()
        token = u.profile.email_verification_token
        u.profile.email_verified = True
        u.profile.save()
        c = Client()
        resp = c.get(reverse("pingpong:email_verify", args=[token]))
        assert resp.status_code == 302


# ===========================================================================
# EmailResendVerificationView
# ===========================================================================

@pytest.mark.django_db
class TestEmailResendVerificationView:
    def test_resend_for_unverified(self):
        u = UserFactory()
        u.profile.email_verified = False
        u.profile.save()
        mail.outbox.clear()
        c = _login_client(u)
        resp = c.post(reverse("pingpong:email_resend_verification"))
        assert resp.status_code == 302
        assert len(mail.outbox) == 1

    def test_already_verified(self):
        u = UserFactory()
        u.profile.email_verified = True
        u.profile.save()
        mail.outbox.clear()
        c = _login_client(u)
        resp = c.post(reverse("pingpong:email_resend_verification"))
        assert resp.status_code == 302
        assert len(mail.outbox) == 0


# ===========================================================================
# match_confirm
# ===========================================================================

@pytest.mark.django_db
class TestMatchConfirm:
    def test_confirm_as_player1(self):
        u, p = _verified_user_with_player()
        other = PlayerFactory(with_user=True)
        m = MatchFactory(player1=p, player2=other)
        c = _login_client(u)
        resp = c.post(reverse("pingpong:match_confirm", args=[m.pk]))
        assert resp.status_code == 302
        m.refresh_from_db()
        # Check that player p (team1) has confirmed
        assert p in m.confirmations.all()

    def test_confirm_as_player2(self):
        u, p = _verified_user_with_player()
        other = PlayerFactory(with_user=True)
        m = MatchFactory(player1=other, player2=p)
        c = _login_client(u)
        resp = c.post(reverse("pingpong:match_confirm", args=[m.pk]))
        assert resp.status_code == 302
        m.refresh_from_db()
        # Check that player p (team2) has confirmed
        assert p in m.confirmations.all()

    def test_already_confirmed(self):
        u, p = _verified_user_with_player()
        other = PlayerFactory(with_user=True)
        m = MatchFactory(player1=p, player2=other)
        # Pre-confirm the match for player p
        confirm_team(m, 1)
        c = _login_client(u)
        resp = c.post(reverse("pingpong:match_confirm", args=[m.pk]))
        assert resp.status_code == 302

    def test_not_participant(self):
        """Non-participant can't see match (MatchManager filters) -> 404."""
        u, p = _verified_user_with_player()
        m = MatchFactory()  # u is not in this match
        c = _login_client(u)
        resp = c.post(reverse("pingpong:match_confirm", args=[m.pk]))
        assert resp.status_code == 404

    def test_no_player_profile(self):
        """User without player can't see any match (MatchManager) -> 404."""
        u, p = _verified_user_with_player()
        # Use a staff user without player to bypass manager, or accept 404
        # Since MatchManager returns none() for users without a player,
        # we use a staff user who can see the match
        staff, _ = _staff_with_player()
        m = MatchFactory()
        c = _login_client(staff)
        # Staff can see the match but is not a participant
        resp = c.post(reverse("pingpong:match_confirm", args=[m.pk]))
        assert resp.status_code == 302  # redirects with error message


# ===========================================================================
# ScheduledMatchCreateView
# ===========================================================================

@pytest.mark.django_db
class TestScheduledMatchCreateView:
    def test_non_staff_player1_locked(self):
        u, p = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:match_schedule"))
        assert resp.status_code == 200
        form = resp.context["form"]
        assert form.fields["player1"].disabled is True

    def test_staff_full_form(self):
        staff, _ = _staff_with_player()
        c = _login_client(staff)
        resp = c.get(reverse("pingpong:match_schedule"))
        assert resp.status_code == 200
        assert resp.context["is_staff"] is True

    def test_email_notifications_sent(self):
        u, p = _verified_user_with_player()
        other = PlayerFactory(with_user=True)
        mail.outbox.clear()
        c = _login_client(u)
        resp = c.post(reverse("pingpong:match_schedule"), {
            "player1": p.pk,
            "player2": other.pk,
            "scheduled_date": (date.today() + timedelta(days=3)).isoformat(),
            "scheduled_time": "14:00",
            "notes": "",
        })
        assert resp.status_code == 302
        # Both players should get notification
        assert len(mail.outbox) == 2
        assert ScheduledMatch.objects.count() == 1


# ===========================================================================
# CalendarView
# ===========================================================================

@pytest.mark.django_db
class TestCalendarView:
    def test_default_month(self):
        u, _ = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:calendar"))
        assert resp.status_code == 200
        today = timezone.now().date()
        assert resp.context["current_date"].month == today.month
        assert resp.context["current_date"].year == today.year

    def test_navigation(self):
        u, _ = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:calendar"), {"year": 2025, "month": 6})
        assert resp.context["current_date"].month == 6
        assert resp.context["current_date"].year == 2025
        assert resp.context["prev_month"].month == 5
        assert resp.context["next_month"].month == 7

    def test_navigation_year_boundary(self):
        u, _ = _verified_user_with_player()
        c = _login_client(u)
        resp = c.get(reverse("pingpong:calendar"), {"year": 2025, "month": 1})
        assert resp.context["prev_month"].month == 12
        assert resp.context["prev_month"].year == 2024

        resp = c.get(reverse("pingpong:calendar"), {"year": 2025, "month": 12})
        assert resp.context["next_month"].month == 1
        assert resp.context["next_month"].year == 2026

    def test_matches_by_day(self):
        u, p = _verified_user_with_player()
        other = PlayerFactory(with_user=True)
        today = timezone.now().date()
        sm = ScheduledMatchFactory(
            player1=p, player2=other,
            scheduled_date=today,
            scheduled_time=time(14, 0),
        )
        c = _login_client(u)
        resp = c.get(reverse("pingpong:calendar"), {
            "year": today.year,
            "month": today.month,
        })
        assert resp.status_code == 200
        weeks = resp.context["calendar_weeks"]
        # Find today's data
        found = False
        for week in weeks:
            for day in week:
                if day["date"] == today and not day["is_other_month"]:
                    assert len(day["matches"]) >= 1
                    found = True
        assert found


# ===========================================================================
# Match Confirmation Elo Update
# ===========================================================================


@pytest.mark.django_db
class TestMatchConfirmEloUpdate:
    """Test that Elo updates when matches are confirmed via the UI"""

    def test_elo_updates_on_second_confirmation(self):
        """Elo should update when second player confirms via match_confirm view"""
        # Setup: Create verified users with players
        user1 = UserFactory(username='player1', email='p1@test.com')
        user1.profile.email_verified = True
        user1.profile.save()
        player1 = PlayerFactory(user=user1, elo_rating=1500, matches_for_elo=25)

        user2 = UserFactory(username='player2', email='p2@test.com')
        user2.profile.email_verified = True
        user2.profile.save()
        player2 = PlayerFactory(user=user2, elo_rating=1500, matches_for_elo=25)

        # Create match with winner
        match = MatchFactory(player1=player1, player2=player2, best_of=5)
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
        match.refresh_from_db()

        # Verify winner is set but not confirmed
        assert match.winner == match.team1
        assert player1 in match.winner.players.all()
        assert not match.team1_confirmed
        assert not match.team2_confirmed
        assert EloHistory.objects.count() == 0

        # Player 1 confirms via UI
        c = Client()
        c.force_login(user1)
        response = c.post(reverse('pingpong:match_confirm', args=[match.pk]))
        assert response.status_code == 302

        match.refresh_from_db()
        player1.refresh_from_db()
        assert match.team1_confirmed
        assert not match.team2_confirmed
        # Elo should NOT update yet (only one confirmation)
        assert player1.elo_rating == 1500
        assert EloHistory.objects.count() == 0

        # Player 2 confirms via UI (second confirmation - should trigger Elo)
        c.force_login(user2)
        response = c.post(reverse('pingpong:match_confirm', args=[match.pk]))
        assert response.status_code == 302

        match.refresh_from_db()
        player1.refresh_from_db()
        player2.refresh_from_db()

        # NOW: Both confirmed, Elo should update
        assert match.team1_confirmed
        assert match.team2_confirmed
        assert match.match_confirmed

        # CRITICAL: Elo should be updated
        assert player1.elo_rating > 1500, f"Winner Elo should increase, got {player1.elo_rating}"
        assert player2.elo_rating < 1500, f"Loser Elo should decrease, got {player2.elo_rating}"

        # CRITICAL: Elo history should exist
        assert EloHistory.objects.count() == 2, "Should have 2 Elo history entries"

        history_p1 = EloHistory.objects.get(match=match, player=player1)
        assert history_p1.old_rating == 1500
        assert history_p1.new_rating == player1.elo_rating
        assert history_p1.rating_change > 0
