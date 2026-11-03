"""End-to-end flows, exercised through the HTTP client.

CLAUDE.md mandates these flows. Some are covered elsewhere --
scheduled-match conversion in test_scheduled_match_conversion.py's
TestConversionIntegration, head-to-head in test_views.py's TestHeadToHead*
classes, club invites in test_club_invites.py -- so this module carries
registration, the singles and doubles lifecycles and the season lifecycle.

The point of these is different from the unit tests around them: each one
walks the whole path a user actually takes, through real view calls, so a
break in the seams between form, view, signal and template shows up here
even when every part passes its own test.
"""
from datetime import date, datetime, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from pingpong import season_rollover
from pingpong.models import (
    EloHistory,
    JobRun,
    Match,
    Player,
    PlayerRating,
    Season,
    SeasonStanding,
    Side,
    UserProfile,
)

from .conftest import (
    ClubFactory,
    ClubMembershipFactory,
    LocationFactory,
    PlayerFactory,
    SeasonFactory,
    UserFactory,
)


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
        # The nav identifies you by your Player, which is what every other
        # screen shows. The old "Welcome back, <username>" banner is gone.
        assert player.name.encode() in dashboard.content


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


@pytest.mark.django_db
class TestPagesSurviveADeletedPlayer:
    """MatchParticipant.player cascades on delete.

    So removing a player strips their participant rows and leaves their old
    matches with an empty side. Templates then asked for
    {% url 'player_detail' <empty> %}, which raises NoReverseMatch -- a 500
    on the match list for every user, permanently, caused by an ordinary
    admin deletion. Reproduced live before fixing.
    """

    def _match_then_delete_one_player(self):
        keeper = _player("Aurelio Keeper", username="keeper")
        doomed = _player("Bartholomew Doomed", username="doomed")
        client = _login(keeper.user)

        client.post(
            reverse("pingpong:match_add"),
            {
                "player1": keeper.pk,
                "player2": doomed.pk,
                "is_double": "False",
                "date_played": "2026-02-02T14:30",
                "match_type": "casual",
                "best_of": "5",
                "notes": "",
            },
        )
        match = Match.all_objects.latest("id")
        for number, (a, b) in enumerate([(11, 5), (11, 7), (11, 9)], start=1):
            client.post(
                reverse("pingpong:game_add", kwargs={"match_pk": match.pk}),
                {"game_number": number, "team1_score": a, "team2_score": b},
            )

        doomed.delete()
        match.refresh_from_db()
        assert list(match.side2_players) == [], "cascade should empty the side"
        return client, match

    def test_match_list_still_renders(self):
        client, _ = self._match_then_delete_one_player()
        resp = client.get(reverse("pingpong:match_list"))
        assert resp.status_code == 200

    def test_match_detail_still_renders(self):
        client, match = self._match_then_delete_one_player()
        resp = client.get(reverse("pingpong:match_detail", args=[match.pk]))
        assert resp.status_code == 200

    def test_player_detail_still_renders(self):
        client, _ = self._match_then_delete_one_player()
        keeper = Player.objects.get(name="Aurelio Keeper")
        resp = client.get(reverse("pingpong:player_detail", args=[keeper.pk]))
        assert resp.status_code == 200

    def test_the_surviving_player_is_still_linked(self):
        """Degrading must not cost the players who are still there."""
        client, _ = self._match_then_delete_one_player()
        keeper = Player.objects.get(name="Aurelio Keeper")
        body = client.get(reverse("pingpong:match_list")).content.decode()
        assert reverse("pingpong:player_detail", args=[keeper.pk]) in body


# ---------------------------------------------------------------------------
# Flow 7: a season, from confirmed matches to the frozen archive (KAN-9)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSeasonLifecycleIntegration:
    """Matches confirmed through the views land in the season's ratings and
    leaderboard; the nightly job then freezes the table into the archive and
    the next season is already there."""

    @pytest.fixture(autouse=True)
    def _flags(self, settings):
        settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "clubs": True, "seasons": True}

    def _member(self, club, name):
        player = _player(name, username=name.split()[0].lower())
        player.club = club
        player.save()
        ClubMembershipFactory(club=club, user=player.user)
        return player

    def _play_and_confirm(self, winner, loser, day):
        """Create, score and confirm a singles match entirely through the views."""
        client = _login(winner.user)
        resp = client.post(reverse("pingpong:match_add"), {
            "player1": winner.pk, "player2": loser.pk, "is_double": "False",
            "date_played": f"{day.isoformat()}T18:00", "location": "",
            "match_type": "casual", "best_of": "3", "notes": "",
        })
        assert resp.status_code == 302, resp.context and resp.context["form"].errors
        match = Match.all_objects.latest("id")
        for number in (1, 2):
            client.post(
                reverse("pingpong:game_add", kwargs={"match_pk": match.pk}),
                {"game_number": number, "team1_score": 11, "team2_score": 6},
            )
        for player in (winner, loser):
            _login(player.user).post(reverse("pingpong:match_confirm", args=[match.pk]))
        match.refresh_from_db()
        assert match.is_confirmed and match.winner_side == Side.ONE
        return match

    def test_confirm_rank_archive_and_roll_over(self):
        club = ClubFactory()
        season = SeasonFactory(
            club=club, name="Q1 2026", starts_on=date(2026, 1, 1), ends_on=date(2026, 3, 31)
        )
        ann = self._member(club, "Ann Ace")
        bob = self._member(club, "Bob Block")
        cat = self._member(club, "Cat Chop")

        # 1. Three results in the season, through the real pipeline.
        self._play_and_confirm(ann, bob, date(2026, 2, 1))
        self._play_and_confirm(ann, cat, date(2026, 2, 2))
        self._play_and_confirm(cat, bob, date(2026, 2, 3))

        # 2. The season's PlayerRating rows exist, one per player.
        rows = {
            r.player_id: r
            for r in PlayerRating.all_objects.filter(season=season, format="singles")
        }
        assert set(rows) == {ann.pk, bob.pk, cat.pk}
        assert rows[ann.pk].rating > rows[cat.pk].rating > rows[bob.pk].rating
        assert {r.matches for r in rows.values()} == {2}

        # 3. The seasons leaderboard ranks them.
        resp = _login(ann.user).get(
            reverse("pingpong:leaderboard"), {"season": season.pk, "format": "singles"}
        )
        assert resp.context["rating_scope"].season == season
        assert [row["player"].pk for row in resp.context["player_stats"]] == [ann.pk, cat.pk, bob.pk]

        # 4. The nightly job, run past the grace days, freezes and rolls over.
        mail.outbox.clear()
        after_grace = datetime(2026, 4, 10, 3, 15, tzinfo=dt_timezone.utc)
        with patch.object(season_rollover.timezone, "now", return_value=after_grace):
            call_command("run_scheduled_jobs", "--job", "season_rollover", stdout=StringIO())

        assert JobRun.objects.get(name="season_rollover").status == JobRun.Status.SUCCESS
        season.refresh_from_db()
        assert season.archived_at is not None
        assert SeasonStanding.all_objects.filter(season=season).count() == 3
        nxt = Season.all_objects.get(club=club, starts_on=date(2026, 4, 1))
        assert nxt.ends_on == date(2026, 6, 30) and nxt.archived_at is None

        # 5. The archive shows the frozen table.
        resp = _login(bob.user).get(reverse("pingpong:season_archive_detail", args=[season.pk]))
        assert resp.status_code == 200
        assert [(r.player_name, r.rank, r.wins, r.losses) for r in resp.context["standings"]] == [
            ("Ann Ace", 1, 2, 0), ("Cat Chop", 2, 1, 1), ("Bob Block", 3, 0, 2),
        ]
        body = resp.content.decode()
        table = body[body.index("<ol"):body.index("</ol>")]  # not the viewer's nav name
        assert table.index("Ann Ace") < table.index("Cat Chop") < table.index("Bob Block")

        # 6. Everyone verified heard where they finished (no push server: email).
        told = {m.to[0] for m in mail.outbox}
        assert {ann.user.email, bob.user.email, cat.user.email} <= told
