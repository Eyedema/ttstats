"""seed_e2e: the Playwright fixture must be identical however often it runs.

The e2e specs assert exact ratings and ranks, so a re-seed that drifts a
rating (a leftover match, or a rating applied on top of the previous run's) makes
the suite depend on how many times the seed ran.
"""
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from pingpong.models import FeatureFlagOverride, Match, Player, PlayerRating, Season

from .conftest import GameFactory, MatchFactory, UserFactory, confirm_match

E2E_NAMES = ("E2E Player", "E2E Opponent", "E2E Momentum", "E2E Seasons Player")


def _seed():
    call_command("seed_e2e", stdout=StringIO())


def _ratings():
    return sorted(
        PlayerRating.all_objects.filter(player__name__in=E2E_NAMES).values_list(
            "player__name", "season__name", "format",
            "glicko_rating", "rd", "volatility", "last_rated_on", "matches",
        ),
        key=repr,
    )


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
        assert first, "the seed wrote no PlayerRating rows"
        assert any(row[3] is not None for row in first), "no Glicko state was written"

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

        from pingpong.models import RatingChange

        _seed()

        user = User.objects.get(username="e2e-glicko")
        me = Player.objects.get(name="E2E Glicko Player")
        row = PlayerRating.all_objects.get(player=me, season__isnull=True, format="singles")
        assert user.player == me
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


@pytest.mark.django_db
class TestRetiredFlagOverrides:
    """`seasons` and `glicko2` were retired by KAN-73: the seed sets no
    override for them and drops the ones an older seed left behind."""

    def test_the_seed_creates_no_override_for_a_retired_flag(self):
        _seed()

        assert not FeatureFlagOverride.objects.filter(
            name__in=["seasons", "glicko2"]
        ).exists()

    def test_a_stale_override_is_deleted(self):
        user = UserFactory()
        for flag in ("seasons", "glicko2"):
            FeatureFlagOverride.objects.create(name=flag, user=user, enabled=True)

        _seed()

        assert not FeatureFlagOverride.objects.filter(
            name__in=["seasons", "glicko2"]
        ).exists()


@pytest.mark.django_db
class TestSharedLiveMatch:
    """KAN-153: a two-phone live match for the "my side" scoreboard specs."""

    def test_one_shared_live_match_survives_a_reseed(self):
        _seed()
        _seed()
        shared = [
            m for m in Match.all_objects.filter(is_live=True, scorekeeper__name="E2E Player")
            if m.is_shared
        ]
        assert len(shared) == 1
        rows = {(s.player.name, s.side, s.can_score_both) for s in shared[0].live_scorers.all()}
        assert rows == {("E2E Player", 1, False), ("E2E Partner", 2, False)}


@pytest.mark.django_db
class TestOfflineReaders:
    """KAN-133: the snapshot specs' users, isolated from every other spec."""

    def test_only_the_readers_have_the_snapshot_flag(self):
        from pingpong import flags

        _seed()
        _seed()
        on = set(
            FeatureFlagOverride.objects.filter(name="offline_snapshots", enabled=True)
            .values_list("user__username", flat=True)
        )
        assert on == {"e2e-reader", "e2e-reader-b"}
        off = Player.objects.get(name="E2E Reader Off").user
        assert flags.is_enabled("offline_snapshots", off) is False
        e2e = Player.objects.get(name="E2E Player").user
        assert flags.is_enabled("offline_snapshots", e2e) is False

    def test_reader_a_is_in_two_clubs_and_owns_one_confirmed_match(self):
        _seed()
        _seed()
        reader = Player.objects.get(name="E2E Reader")
        clubs = sorted(reader.user.club_memberships.values_list("club__name", flat=True))
        assert len(clubs) == 2 and clubs[0] == "E2E Reading Club"
        match = Match.all_objects.get(participants__player=reader)
        assert match.is_confirmed
        assert match.club.slug == "e2e-reading-club"

    def test_reader_b_is_scoring_one_live_match_in_the_reading_club(self):
        # KAN-146: a saved Today with amber on it, for the muting spec.
        _seed()
        _seed()
        reader_b = Player.objects.get(name="E2E Reader B")
        live = Match.all_objects.filter(is_live=True, participants__player=reader_b).distinct()
        assert live.count() == 1
        match = live.get()
        assert match.scorekeeper == reader_b
        assert match.club.slug == "e2e-reading-club"
        assert {p.name for p in match.all_players} == {"E2E Reader B", "E2E Reader Rival"}
        # Reader A is untouched: nothing live.
        reader = Player.objects.get(name="E2E Reader")
        assert not Match.all_objects.filter(is_live=True, participants__player=reader).exists()
