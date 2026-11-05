"""seed_e2e: the Playwright fixture must be identical however often it runs.

The e2e specs assert exact ratings and ranks, so a re-seed that drifts a
rating (a leftover match, or Elo applied on top of the previous run's) makes
the suite depend on how many times the seed ran.
"""
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from pingpong.models import Match, Player, PlayerRating, Season

from .conftest import GameFactory, MatchFactory, confirm_match

E2E_NAMES = ("E2E Player", "E2E Opponent", "E2E Momentum", "E2E Seasons Player")


def _seed():
    call_command("seed_e2e", stdout=StringIO())


def _ratings():
    players = sorted(
        Player.objects.filter(name__in=E2E_NAMES)
        .values_list("name", "elo_rating", "elo_peak", "matches_for_elo")
    )
    rows = sorted(
        PlayerRating.all_objects.filter(player__name__in=E2E_NAMES)
        .values_list("player__name", "season__name", "format", "rating", "peak", "matches"),
        key=repr,
    )
    return players, rows


@pytest.fixture(autouse=True)
def _debug(settings):
    settings.DEBUG = True


@pytest.mark.django_db
class TestSeedE2EIsIdempotent:
    def test_a_rerun_leaves_identical_ratings(self):
        _seed()
        first = _ratings()

        _seed()

        assert _ratings() == first
        assert first[1], "the seed wrote no PlayerRating rows"

    def test_a_leftover_match_between_seeded_players_is_removed(self):
        """A spec that finishes the live match leaves a confirmed result behind."""
        _seed()
        first = _ratings()
        me = Player.objects.get(name="E2E Seasons Player")
        opponent = Player.objects.get(name="E2E Opponent")
        club = me.club
        leftover = MatchFactory(player1=opponent, player2=me, club=club, best_of=3)
        for n in (1, 2):
            GameFactory(match=leftover, game_number=n, team1_score=11, team2_score=2)
        confirm_match(leftover)
        assert _ratings() != first

        _seed()

        assert not Match.all_objects.filter(pk=leftover.pk).exists()
        assert _ratings() == first

    def test_a_stale_season_is_moved_to_cover_today(self):
        _seed()
        season = Season.all_objects.get(name="E2E Season")
        Season.all_objects.filter(pk=season.pk).update(
            starts_on=season.starts_on - timedelta(days=400),
            ends_on=season.ends_on - timedelta(days=400),
        )

        _seed()

        season.refresh_from_db()
        assert season.starts_on <= timezone.now().date() <= season.ends_on


@pytest.mark.django_db
class TestGlickoSeed:
    def test_the_glicko_member_has_pipeline_written_state_and_a_pending_result(self):
        from django.contrib.auth.models import User

        from pingpong import ratings
        from pingpong.models import RatingChange

        _seed()

        user = User.objects.get(username="e2e-glicko")
        me = Player.objects.get(name="E2E Glicko Player")
        row = PlayerRating.all_objects.get(player=me, season__isnull=True, format="singles")
        assert ratings.system(user) == ratings.GLICKO
        assert row.glicko_rating is not None and row.rd is not None
        assert RatingChange.objects.filter(player=me, glicko_change__isnull=False).exists()
        assert Match.all_objects.filter(participants__player=me, is_confirmed=False).count() == 1

    def test_a_rerun_leaves_the_glicko_state_identical(self):
        _seed()
        first = list(
            PlayerRating.all_objects.filter(player__name="E2E Glicko Player")
            .values_list("season__name", "format", "glicko_rating", "rd")
            .order_by("season__name", "format")
        )

        _seed()

        assert first == list(
            PlayerRating.all_objects.filter(player__name="E2E Glicko Player")
            .values_list("season__name", "format", "glicko_rating", "rd")
            .order_by("season__name", "format")
        )
