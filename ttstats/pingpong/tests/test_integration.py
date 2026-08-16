"""End-to-end flows, exercised through the HTTP client.

CLAUDE.md mandates five of these. Two were already covered elsewhere --
scheduled-match conversion in test_scheduled_match_conversion.py's
TestConversionIntegration, and head-to-head in test_views.py's
TestHeadToHead* classes -- so this module carries the other three.

The point of these is different from the unit tests around them: each one
walks the whole path a user actually takes, through real view calls, so a
break in the seams between form, view, signal and template shows up here
even when every part passes its own test.
"""
import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client
from django.urls import reverse

from pingpong.models import EloHistory, Match, Player, Side, UserProfile

from .conftest import LocationFactory, PlayerFactory, UserFactory


def _verified(user):
    user.profile.email_verified = True
    user.profile.save()
    return user


def _player(name, **kwargs):
    """A verified user with a linked player, ready to log in."""
    user = UserFactory(**kwargs)
    _verified(user)
    return PlayerFactory(user=user, name=name, elo_rating=1500, matches_for_elo=25)


def _login(user):
    client = Client()
    client.force_login(user)
    return client


# ---------------------------------------------------------------------------
# Flow 1: registration -> verification -> login
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRegistrationToLogin:
    def test_signup_verify_then_reach_the_dashboard(self):
        client = Client()

        resp = client.post(
            reverse("pingpong:signup"),
            {
                "username": "newcomer",
                "email": "newcomer@example.com",
                "password1": "Str0ngP@ssw0rd!",
                "password2": "Str0ngP@ssw0rd!",
                "full_name": "Newcomer Person",
                "nickname": "",
                "playing_style": "normal",
            },
        )
        assert resp.status_code == 200

        user = User.objects.get(username="newcomer")
        profile = UserProfile.objects.get(user=user)
        assert profile.email_verified is False
        assert Player.objects.filter(user=user, name="Newcomer Person").exists()

        # A verification email goes out with the token in it.
        assert len(mail.outbox) >= 1
        body = "".join(m.body for m in mail.outbox)
        assert profile.email_verification_token in body

        # Following the link verifies and logs the user straight in.
        resp = Client().get(
            reverse(
                "pingpong:email_verify",
                kwargs={"token": profile.email_verification_token},
            ),
            follow=True,
        )
        assert resp.status_code == 200
        profile.refresh_from_db()
        assert profile.email_verified is True

    def test_verified_user_can_log_in_and_load_the_dashboard(self):
        """base.html dereferences user.player.pk, so this also pins that a
        freshly registered user has a usable profile."""
        client = Client()
        client.post(
            reverse("pingpong:signup"),
            {
                "username": "returner",
                "email": "returner@example.com",
                "password1": "Str0ngP@ssw0rd!",
                "password2": "Str0ngP@ssw0rd!",
                "full_name": "Returner Person",
                "nickname": "",
                "playing_style": "normal",
            },
        )
        profile = UserProfile.objects.get(user__username="returner")
        Client().get(
            reverse(
                "pingpong:email_verify",
                kwargs={"token": profile.email_verification_token},
            )
        )

        fresh = Client()
        resp = fresh.post(
            reverse("pingpong:login"),
            {"username": "returner", "password": "Str0ngP@ssw0rd!"},
            follow=True,
        )
        assert resp.status_code == 200

        # base.html renders {% url 'player_detail' user.player.pk %}
        # unconditionally, so a user without a linked Player crashes the
        # template rather than merely looking odd. Assert the link resolved.
        dashboard = fresh.get(reverse("pingpong:dashboard"))
        assert dashboard.status_code == 200
        player = Player.objects.get(user__username="returner")
        assert (
            reverse("pingpong:player_detail", args=[player.pk]).encode()
            in dashboard.content
        )
        assert b"returner" in dashboard.content


# ---------------------------------------------------------------------------
# Flow 2: singles match lifecycle, with Elo
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSinglesMatchLifecycle:
    def test_create_score_confirm_and_see_it_on_the_leaderboard(self):
        winner = _player("Aurelio Winner", username="aurelio")
        loser = _player("Bartholomew Loser", username="bartholomew")
        location = LocationFactory()
        client = _login(winner.user)

        # 1. Create the match through the form.
        resp = client.post(
            reverse("pingpong:match_add"),
            {
                "player1": winner.pk,
                "player2": loser.pk,
                "is_double": "False",
                "date_played": "2026-02-02T14:30",
                "location": location.pk,
                "match_type": "casual",
                "best_of": "5",
                "notes": "",
            },
        )
        assert resp.status_code == 302, "valid form should redirect, not redisplay"

        match = Match.all_objects.latest("id")
        assert set(match.side1_players) == {winner}
        assert set(match.side2_players) == {loser}

        # 2. Score it, through the game form, until someone wins.
        mail.outbox.clear()
        for number, (a, b) in enumerate([(11, 5), (11, 7), (11, 9)], start=1):
            resp = client.post(
                reverse("pingpong:game_add", kwargs={"match_pk": match.pk}),
                {"game_number": number, "team1_score": a, "team2_score": b},
            )
            assert resp.status_code == 302

        match.refresh_from_db()
        assert match.winner_side == Side.ONE
        assert match.is_confirmed is False

        # 3. Both players are verified, so confirmation emails go out.
        assert len(mail.outbox) >= 1

        # 4. Elo holds until every verified player has confirmed.
        assert EloHistory.objects.filter(match=match).count() == 0
        client.post(reverse("pingpong:match_confirm", args=[match.pk]))
        match.refresh_from_db()
        assert match.is_confirmed is False, "one confirmation is not enough"

        _login(loser.user).post(reverse("pingpong:match_confirm", args=[match.pk]))
        match.refresh_from_db()
        assert match.is_confirmed is True

        # 5. Elo moved, in the right direction, for both players.
        winner.refresh_from_db()
        loser.refresh_from_db()
        assert winner.elo_rating > 1500
        assert loser.elo_rating < 1500
        assert EloHistory.objects.filter(match=match).count() == 2

        # 6. And the result surfaces on the leaderboard.
        body = client.get(reverse("pingpong:leaderboard")).content.decode()
        assert "Aurelio Winner" in body
        assert "Bartholomew Loser" in body


# ---------------------------------------------------------------------------
# Flow 3: doubles match lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDoublesMatchLifecycle:
    def test_four_players_all_confirm_and_all_four_elos_move(self):
        p1 = _player("Aurelio Home", username="aurelio")
        p2 = _player("Beatrix Home", username="beatrix")
        p3 = _player("Casimir Away", username="casimir")
        p4 = _player("Drusilla Away", username="drusilla")
        client = _login(p1.user)

        resp = client.post(
            reverse("pingpong:match_add"),
            {
                "player1": p1.pk,
                "player2": p2.pk,
                "player3": p3.pk,
                "player4": p4.pk,
                "is_double": "True",
                "date_played": "2026-02-02T14:30",
                "match_type": "casual",
                "best_of": "5",
                "notes": "",
            },
        )
        assert resp.status_code == 302, "valid doubles form should redirect"

        match = Match.all_objects.latest("id")
        assert match.is_double is True
        assert set(match.side1_players) == {p1, p2}
        assert set(match.side2_players) == {p3, p4}

        for number, (a, b) in enumerate([(11, 5), (11, 7), (11, 9)], start=1):
            client.post(
                reverse("pingpong:game_add", kwargs={"match_pk": match.pk}),
                {"game_number": number, "team1_score": a, "team2_score": b},
            )

        match.refresh_from_db()
        assert match.winner_side == Side.ONE

        # Doubles needs all four confirmations, not two.
        for player in (p1, p2, p3):
            _login(player.user).post(
                reverse("pingpong:match_confirm", args=[match.pk])
            )
            match.refresh_from_db()
            assert match.is_confirmed is False, f"still waiting after {player.name}"

        _login(p4.user).post(reverse("pingpong:match_confirm", args=[match.pk]))
        match.refresh_from_db()
        assert match.is_confirmed is True

        for player in (p1, p2, p3, p4):
            player.refresh_from_db()
        assert p1.elo_rating > 1500 and p2.elo_rating > 1500
        assert p3.elo_rating < 1500 and p4.elo_rating < 1500
        assert EloHistory.objects.filter(match=match).count() == 4

    def test_match_detail_names_all_four_players(self):
        """The doubles layout is where the removed Team fields were read
        from most heavily."""
        p1 = _player("Aurelio Home", username="aurelio")
        p2 = _player("Beatrix Home", username="beatrix")
        p3 = _player("Casimir Away", username="casimir")
        p4 = _player("Drusilla Away", username="drusilla")
        client = _login(p1.user)

        client.post(
            reverse("pingpong:match_add"),
            {
                "player1": p1.pk, "player2": p2.pk,
                "player3": p3.pk, "player4": p4.pk,
                "is_double": "True",
                "date_played": "2026-02-02T14:30",
                "match_type": "casual",
                "best_of": "5",
                "notes": "",
            },
        )
        match = Match.all_objects.latest("id")

        body = client.get(
            reverse("pingpong:match_detail", args=[match.pk])
        ).content.decode()
        for player in (p1, p2, p3, p4):
            assert player.name in body, f"{player.name} missing from match detail"


@pytest.mark.django_db
class TestFormsShowEveryError:
    """Templates printed `.errors.0` and dropped the rest.

    Django's password validators are the clearest case: a weak password
    fails several of them at once, and the user was told only the first
    reason, fixed it, and was then told the next one.
    """

    def test_signup_lists_all_password_problems_at_once(self):
        resp = Client().post(
            reverse("pingpong:signup"),
            {
                "username": "shorty",
                "email": "shorty@example.com",
                "password1": "1",
                "password2": "1",
                "full_name": "Shorty Person",
                "nickname": "",
                "playing_style": "normal",
            },
        )
        assert resp.status_code == 200
        body = resp.content.decode()

        assert "too short" in body
        assert "too common" in body
        assert "entirely numeric" in body
