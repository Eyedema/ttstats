"""View logic introduced by the visual overhaul.

Three things that are easy to get subtly wrong and impossible to see in a
screenshot:

  * "Waiting on you" must contain only matches *you* still have to act on.
  * The Elo figure shown before you agree must be the figure that gets written
    when you do.
  * The leaderboard's weekly movement, and the editorial line derived from it.
"""
import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from pingpong.elo import projected_elo_change_for, projected_elo_changes
from pingpong.models import EloHistory, Match, MatchConfirmation, Side

from .conftest import (
    GameFactory,
    MatchFactory,
    PlayerFactory,
    ScheduledMatchFactory,
    confirm_match,
)
from .test_views import _login_client, _verified_user_with_player


def _verified_player(**kwargs):
    """A player whose email is verified, so confirmations are actually required.

    An unverified participant makes the match auto-confirm on completion (see
    should_auto_confirm), which would quietly empty every "waiting on you"
    assertion below.
    """
    player = PlayerFactory(with_user=True, **kwargs)
    player.user.profile.email_verified = True
    player.user.profile.save()
    return player


def _finished_match(winner, loser, **kwargs):
    """A completed 3-0 with no confirmations recorded."""
    match = MatchFactory(player1=winner, player2=loser, best_of=5, **kwargs)
    for n in (1, 2, 3):
        GameFactory(match=match, game_number=n, team1_score=11, team2_score=5)
    match.refresh_from_db()
    return match


# ---------------------------------------------------------------------------
# Today
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestWaitingOnYou:
    def test_lists_a_match_this_player_has_not_confirmed(self):
        user, me = _verified_user_with_player()
        them = _verified_player()
        match = _finished_match(them, me)
        MatchConfirmation.objects.create(match=match, player=them)

        resp = _login_client(user).get(reverse("pingpong:dashboard"))
        rows = resp.context["pending_confirmations"]

        assert [row["match"].pk for row in rows] == [match.pk]
        assert rows[0]["opponent"].pk == them.pk

    def test_excludes_a_match_this_player_has_already_confirmed(self):
        """A match waiting on the *other* side is not waiting on you.

        Listing it under a heading that says "Waiting on you" is precisely how
        a dashboard teaches people to stop reading that block.
        """
        user, me = _verified_user_with_player()
        them = _verified_player()
        match = _finished_match(them, me)
        MatchConfirmation.objects.create(match=match, player=me)

        resp = _login_client(user).get(reverse("pingpong:dashboard"))
        assert resp.context["pending_confirmations"] == []

    def test_excludes_a_match_with_no_winner_yet(self):
        user, me = _verified_user_with_player()
        them = _verified_player()
        match = MatchFactory(player1=them, player2=me, best_of=5)
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        match.refresh_from_db()
        assert match.winner_side is None

        resp = _login_client(user).get(reverse("pingpong:dashboard"))
        assert resp.context["pending_confirmations"] == []

    def test_scores_are_oriented_to_the_reader(self):
        """Every row is written from your point of view, so the template never
        has to work out which column it is in."""
        user, me = _verified_user_with_player()
        them = _verified_player()
        # `me` is on side two here, so a naive team1/team2 read would invert.
        match = _finished_match(them, me)
        MatchConfirmation.objects.create(match=match, player=them)

        row = _login_client(user).get(reverse("pingpong:dashboard")).context["pending_confirmations"][0]
        assert row["their_score"] == 3
        assert row["my_score"] == 0

    def test_the_elo_cost_of_agreeing_is_shown(self):
        user, me = _verified_user_with_player()
        them = _verified_player()
        match = _finished_match(them, me)
        MatchConfirmation.objects.create(match=match, player=them)

        row = _login_client(user).get(reverse("pingpong:dashboard")).context["pending_confirmations"][0]
        assert row["elo_if_true"] == projected_elo_change_for(match, me)
        assert row["elo_if_true"] < 0  # you lost it


@pytest.mark.django_db
class TestRivalries:
    def test_counts_wins_and_losses_from_the_reader_side(self):
        user, me = _verified_user_with_player()
        them = _verified_player()

        confirm_match(_finished_match(me, them))
        confirm_match(_finished_match(me, them))
        confirm_match(_finished_match(them, me))

        rivalries = _login_client(user).get(reverse("pingpong:dashboard")).context["rivalries"]
        assert len(rivalries) == 1
        assert rivalries[0]["player"].pk == them.pk
        assert (rivalries[0]["wins"], rivalries[0]["losses"]) == (2, 1)
        assert rivalries[0]["played"] == 3
        assert rivalries[0]["leading"] is True

    def test_ignores_unconfirmed_matches(self):
        """An unconfirmed result is a claim, not a record."""
        user, me = _verified_user_with_player()
        them = _verified_player()
        _finished_match(them, me)

        assert _login_client(user).get(reverse("pingpong:dashboard")).context["rivalries"] == []

    def test_ordered_by_how_recently_you_played(self):
        """A rivalry is live because it is ongoing, not because it was busy."""
        user, me = _verified_user_with_player()
        old, recent = _verified_player(), _verified_player()

        now = timezone.now()
        for days, opponent in ((30, old), (30, old), (1, recent)):
            match = _finished_match(me, opponent)
            Match.all_objects.filter(pk=match.pk).update(
                date_played=now - datetime.timedelta(days=days)
            )
            confirm_match(Match.all_objects.get(pk=match.pk))

        rivalries = _login_client(user).get(reverse("pingpong:dashboard")).context["rivalries"]
        assert rivalries[0]["player"].pk == recent.pk


@pytest.mark.django_db
class TestFixtures:
    def test_lists_upcoming_unconverted_fixtures(self):
        user, me = _verified_user_with_player()
        them = _verified_player()
        tomorrow = timezone.localdate() + datetime.timedelta(days=1)
        sm = ScheduledMatchFactory(
            player1=me, player2=them, scheduled_date=tomorrow, scheduled_time=datetime.time(18, 0)
        )

        fixtures = _login_client(user).get(reverse("pingpong:dashboard")).context["fixtures"]
        assert [f["scheduled_match"].pk for f in fixtures] == [sm.pk]
        assert fixtures[0]["opponent"].pk == them.pk

    def test_excludes_past_fixtures(self):
        user, me = _verified_user_with_player()
        them = _verified_player()
        ScheduledMatchFactory(
            player1=me,
            player2=them,
            scheduled_date=timezone.localdate() - datetime.timedelta(days=2),
            scheduled_time=datetime.time(18, 0),
        )

        assert _login_client(user).get(reverse("pingpong:dashboard")).context["fixtures"] == []


@pytest.mark.django_db
class TestPlayView:
    def test_renders_the_three_ways_to_begin(self):
        user, _ = _verified_user_with_player()
        resp = _login_client(user).get(reverse("pingpong:play"))

        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Score a game live" in body
        assert "Log a finished match" in body
        assert "Schedule a match" in body

    def test_requires_login(self):
        from django.test import Client

        resp = Client().get(reverse("pingpong:play"))
        assert resp.status_code == 302


# ---------------------------------------------------------------------------
# The Elo figure you are asked to agree to
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestProjectedElo:
    def test_the_projection_is_what_gets_written(self):
        """The dashboard says "-16 Elo if true" before you confirm. If that is
        not the number the confirmation actually applies, the app has lied to
        the user about the consequence of a decision it asked them to make.
        """
        winner = _verified_player()
        loser = _verified_player()
        match = _finished_match(winner, loser)

        projection = projected_elo_changes(match)
        confirm_match(match)

        history = {h.player_id: h for h in EloHistory.objects.filter(match=match)}
        assert history[winner.pk].rating_change == projection.side1_change
        assert history[loser.pk].rating_change == projection.side2_change

    def test_the_recorded_k_factor_is_the_one_used(self):
        winner = _verified_player()
        loser = _verified_player()
        match = _finished_match(winner, loser)

        projection = projected_elo_changes(match)
        confirm_match(match)

        history = {h.player_id: h for h in EloHistory.objects.filter(match=match)}
        assert history[winner.pk].k_factor == pytest.approx(projection.side1_k)
        assert history[loser.pk].k_factor == pytest.approx(projection.side2_k)

    def test_a_match_with_no_winner_moves_nobody(self):
        a, b = PlayerFactory(), PlayerFactory()
        match = MatchFactory(player1=a, player2=b, best_of=5)
        assert projected_elo_changes(match) == (0, 0, 0.0, 0.0)

    def test_a_player_who_did_not_play_is_worth_nothing(self):
        a, b = PlayerFactory(), PlayerFactory()
        bystander = PlayerFactory()
        match = _finished_match(a, b)
        assert projected_elo_change_for(match, bystander) == 0
        assert projected_elo_change_for(match, None) == 0

    def test_the_two_sides_are_opposite_in_sign(self):
        a, b = PlayerFactory(), PlayerFactory()
        projection = projected_elo_changes(_finished_match(a, b))
        assert projection.side1_change > 0 > projection.side2_change


# ---------------------------------------------------------------------------
# The Table
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestLeaderboardMovement:
    def test_movement_sums_this_week_only(self):
        user, me = _verified_user_with_player()
        them = _verified_player()
        confirm_match(_finished_match(me, them))

        # Age one of the two history rows out of the window.
        EloHistory.objects.filter(player=me).update(
            created_at=timezone.now() - datetime.timedelta(days=30)
        )

        stats = _login_client(user).get(reverse("pingpong:leaderboard")).context["player_stats"]
        by_player = {row["player"].pk: row for row in stats}
        assert by_player[me.pk]["movement"] == 0
        assert by_player[them.pk]["movement"] < 0

    def test_biggest_mover_is_the_largest_absolute_change(self):
        """A 40-point collapse is at least as newsworthy as a 40-point climb."""
        user, me = _verified_user_with_player()
        them = _verified_player()
        confirm_match(_finished_match(me, them))

        context = _login_client(user).get(reverse("pingpong:leaderboard")).context
        mover = context["biggest_mover"]
        assert mover is not None
        assert abs(mover["movement"]) == max(
            abs(row["movement"]) for row in context["player_stats"]
        )

    def test_no_movement_produces_no_editorial_line(self):
        """A week where nothing happened says nothing, rather than "Nobody, +0"."""
        user, me = _verified_user_with_player()
        them = _verified_player()
        confirm_match(_finished_match(me, them))
        EloHistory.objects.all().update(
            created_at=timezone.now() - datetime.timedelta(days=30)
        )

        context = _login_client(user).get(reverse("pingpong:leaderboard")).context
        assert context["biggest_mover"] is None

    def test_the_fragment_carries_the_same_context_as_the_page(self):
        """htmx swaps _leaderboard_results.html on its own, so it has to be
        handed everything it renders -- including the movement column and the
        footer line, which are derived rather than stored."""
        user, me = _verified_user_with_player()
        them = _verified_player()
        confirm_match(_finished_match(me, them))

        resp = _login_client(user).get(
            reverse("pingpong:leaderboard"), HTTP_HX_REQUEST="true"
        )
        assert "pingpong/_leaderboard_results.html" in [t.name for t in resp.templates]
        assert "movement" in resp.context["player_stats"][0]
        assert "biggest_mover" in resp.context

    def test_movement_survives_the_cache(self):
        """The leaderboard is cached per filter set. Both the hit and the miss
        path have to produce the derived context, or a cached page loses its
        movement column and its footer."""
        user, me = _verified_user_with_player()
        them = _verified_player()
        confirm_match(_finished_match(me, them))

        client = _login_client(user)
        first = client.get(reverse("pingpong:leaderboard")).context
        second = client.get(reverse("pingpong:leaderboard")).context

        assert second["biggest_mover"]["player"].pk == first["biggest_mover"]["player"].pk
        assert second["leaderboard_week"] == first["leaderboard_week"]


# ---------------------------------------------------------------------------
# Match detail
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestMatchDetailViewerContext:
    def test_a_participant_who_has_not_confirmed_gets_the_confirm_bar(self):
        user, me = _verified_user_with_player()
        them = _verified_player()
        match = _finished_match(them, me)
        MatchConfirmation.objects.create(match=match, player=them)

        resp = _login_client(user).get(reverse("pingpong:match_detail", args=[match.pk]))
        assert resp.context["viewer_side"] == Side.TWO
        assert resp.context["viewer_can_confirm"] is True
        assert b"Confirm 3" in resp.content

    def test_a_participant_who_has_confirmed_does_not(self):
        """The old template asked the same question in four duplicated places
        and they disagreed about whether a confirmed side still sees a button."""
        user, me = _verified_user_with_player()
        them = _verified_player()
        match = _finished_match(them, me)
        MatchConfirmation.objects.create(match=match, player=me)

        resp = _login_client(user).get(reverse("pingpong:match_detail", args=[match.pk]))
        assert resp.context["viewer_has_confirmed"] is True
        assert resp.context["viewer_can_confirm"] is False
        assert b"Confirm 3" not in resp.content

    def test_no_confirm_bar_before_there_is_a_winner(self):
        user, me = _verified_user_with_player()
        them = _verified_player()
        match = MatchFactory(player1=me, player2=them, best_of=5)

        resp = _login_client(user).get(reverse("pingpong:match_detail", args=[match.pk]))
        assert resp.context["viewer_can_confirm"] is False

    def test_elo_reads_as_pending_until_confirmed(self):
        user, me = _verified_user_with_player()
        them = _verified_player()
        match = _finished_match(them, me)
        MatchConfirmation.objects.create(match=match, player=them)

        resp = _login_client(user).get(reverse("pingpong:match_detail", args=[match.pk]))
        assert resp.context["elo_is_pending"] is True
        assert b"if agreed" in resp.content

    def test_elo_reads_as_applied_once_confirmed(self):
        user, me = _verified_user_with_player()
        them = _verified_player()
        match = _finished_match(them, me)
        confirm_match(match)

        resp = _login_client(user).get(reverse("pingpong:match_detail", args=[match.pk]))
        assert resp.context["elo_is_pending"] is False
        assert resp.context["side1_elo_delta"] != 0
        assert b"if agreed" not in resp.content

    def test_game_rows_carry_the_share_of_points(self):
        """The bar replaces two score boxes, which only said who won."""
        user, me = _verified_user_with_player()
        them = _verified_player()
        match = MatchFactory(player1=me, player2=them, best_of=5)
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=9)

        rows = _login_client(user).get(
            reverse("pingpong:match_detail", args=[match.pk])
        ).context["game_rows"]
        assert rows[0]["side1_share"] == round(11 / 20 * 100)

    def test_a_deuce_game_is_called_out(self):
        user, me = _verified_user_with_player()
        them = _verified_player()
        match = MatchFactory(player1=me, player2=them, best_of=5)
        GameFactory(match=match, game_number=1, team1_score=14, team2_score=16)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=4)

        note = _login_client(user).get(
            reverse("pingpong:match_detail", args=[match.pk])
        ).context["long_games_note"]
        assert note.startswith("1 of 2 games went past 11")

    def test_no_note_when_nothing_went_long(self):
        """A line that appears every time stops being read."""
        user, me = _verified_user_with_player()
        them = _verified_player()
        match = MatchFactory(player1=me, player2=them, best_of=5)
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=4)

        assert _login_client(user).get(
            reverse("pingpong:match_detail", args=[match.pk])
        ).context["long_games_note"] == ""
