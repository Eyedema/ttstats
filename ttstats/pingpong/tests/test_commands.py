"""
Tests for management commands.
"""

import pytest
from io import StringIO
from django.core.management import call_command

from ..models import Player, Match, Game, EloHistory
from .conftest import PlayerFactory, MatchFactory, GameFactory, confirm_match, confirm_match_silent


@pytest.mark.django_db
class TestRecalculateElo:
    """Test recalculate_elo management command"""

    def test_dry_run_no_changes(self):
        """Dry run should not make any changes"""
        p1 = PlayerFactory(elo_rating=1600, matches_for_elo=10)
        p2 = PlayerFactory(elo_rating=1400, matches_for_elo=10)

        match = MatchFactory(player1=p1, player2=p2)
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
        match.refresh_from_db()
        confirm_match(match)

        # Save Elo values AFTER confirmation (signal will have updated them)
        p1.refresh_from_db()
        p2.refresh_from_db()
        elo_p1_before = p1.elo_rating
        elo_p2_before = p2.elo_rating
        matches_p1_before = p1.matches_for_elo
        matches_p2_before = p2.matches_for_elo

        out = StringIO()
        call_command('recalculate_elo', '--dry-run', stdout=out)

        p1.refresh_from_db()
        p2.refresh_from_db()

        # Elo should not change from what it was before the command
        assert p1.elo_rating == elo_p1_before
        assert p2.elo_rating == elo_p2_before
        assert p1.matches_for_elo == matches_p1_before
        assert p2.matches_for_elo == matches_p2_before

        # Output should mention dry run
        output = out.getvalue()
        assert 'DRY RUN' in output
        assert 'Found 1 confirmed matches' in output

    def test_recalculate_resets_all_players(self):
        """Command should reset all players to 1500 before recalculating"""
        p1 = PlayerFactory(elo_rating=1700, elo_peak=1750, matches_for_elo=50)
        p2 = PlayerFactory(elo_rating=1300, elo_peak=1500, matches_for_elo=40)

        match = MatchFactory(player1=p1, player2=p2)
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
        match.refresh_from_db()
        # Use silent confirm to avoid triggering Elo updates before the command
        confirm_match_silent(match)

        call_command('recalculate_elo', stdout=StringIO())

        p1.refresh_from_db()
        p2.refresh_from_db()

        # Elo should be recalculated from 1500 base (not from 1700/1300)
        # Winner should be slightly above 1500
        assert p1.elo_rating > 1500
        assert p1.elo_rating < 1550
        # Loser should be slightly below 1500
        assert p2.elo_rating < 1500
        assert p2.elo_rating > 1450

        # matches_for_elo should be 1 (only one match)
        assert p1.matches_for_elo == 1
        assert p2.matches_for_elo == 1

    def test_recalculate_chronological_order(self):
        """Command should process matches in chronological order"""
        from datetime import datetime, timedelta
        from django.utils import timezone

        p1 = PlayerFactory(elo_rating=1500, matches_for_elo=0)
        p2 = PlayerFactory(elo_rating=1500, matches_for_elo=0)

        # Create 3 matches over 3 days
        base_date = timezone.make_aware(datetime(2024, 1, 1, 12, 0, 0))

        for i in range(3):
            match = MatchFactory(
                player1=p1,
                player2=p2,
                date_played=base_date + timedelta(days=i)
            )
            # P1 wins all matches
            GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
            GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
            GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
            match.refresh_from_db()
            # Use silent confirm to avoid triggering Elo updates before the command
            confirm_match_silent(match)

        call_command('recalculate_elo', stdout=StringIO())

        p1.refresh_from_db()
        p2.refresh_from_db()

        # P1 should have gained Elo progressively (3 wins)
        assert p1.elo_rating > 1540  # ~16 points per win
        # P2 should have lost Elo progressively (3 losses)
        assert p2.elo_rating < 1460

        # 3 matches counted
        assert p1.matches_for_elo == 3
        assert p2.matches_for_elo == 3

        # 6 history records (2 per match)
        assert EloHistory.objects.count() == 6

    def test_recalculate_clears_existing_history(self):
        """Command should delete existing Elo history before recalculating"""
        p1 = PlayerFactory()
        p2 = PlayerFactory()
        match = MatchFactory(player1=p1, player2=p2)

        # Create fake history
        EloHistory.objects.create(
            match=match,
            player=p1,
            old_rating=1500,
            new_rating=1516,
            rating_change=16,
            k_factor=32.0,
        )

        assert EloHistory.objects.count() == 1

        # Create real match
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
        match.refresh_from_db()
        confirm_match(match)

        out = StringIO()
        call_command('recalculate_elo', stdout=out)

        # Old history should be deleted, new history created
        assert EloHistory.objects.count() == 2

        output = out.getvalue()
        assert 'Deleted 1 existing Elo history records' in output

    def test_skip_unconfirmed_matches(self):
        """Command should skip matches without both confirmations"""
        # Create players with verified emails to prevent auto-confirm
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        p1.user.profile.email_verified = True
        p1.user.profile.save()
        p2.user.profile.email_verified = True
        p2.user.profile.save()

        # Confirmed match
        match1 = MatchFactory(player1=p1, player2=p2)
        GameFactory(match=match1, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match1, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match1, game_number=3, team1_score=11, team2_score=9)
        match1.refresh_from_db()
        confirm_match(match1)

        # Unconfirmed match (will NOT auto-confirm because players are verified)
        match2 = MatchFactory(player1=p1, player2=p2)
        GameFactory(match=match2, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match2, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match2, game_number=3, team1_score=11, team2_score=9)
        match2.refresh_from_db()
        # No confirmations

        out = StringIO()
        call_command('recalculate_elo', stdout=out)

        output = out.getvalue()
        # Only 1 match should be processed
        assert 'Found 1 confirmed matches' in output

        # Only 2 history records (from confirmed match)
        # Note: There might be 4 if the first match was auto-confirmed, but we manually confirmed it
        # so there should only be 2 from the recalculate command
        assert EloHistory.objects.count() == 2


# ---------------------------------------------------------------------------
# KAN-49 (S4.7): recalculate_elo rebuilds the PlayerRating mirror too
# ---------------------------------------------------------------------------

from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.core.management.base import CommandError

from .. import elo_replay
from ..models import PlayerRating, RatingChange, Side
from .conftest import ClubFactory, SeasonFactory

KAN49_BASE = datetime(2026, 1, 1, 12, tzinfo=dt_timezone.utc)


def _won(side1, side2, *, day, club, winner=Side.ONE):
    match = MatchFactory(team1_players=side1, team2_players=side2, is_double=len(side1) > 1,
                         date_played=KAN49_BASE + timedelta(days=day), club=club)
    for n in (1, 2, 3):
        t1, t2 = (11, 6) if winner == Side.ONE else (6, 11)
        GameFactory(match=match, game_number=n, team1_score=t1, team2_score=t2)
    return match


def _season_history():
    """Live-confirmed history with a season, a gap, singles and doubles."""
    club = ClubFactory()
    season = SeasonFactory(club=club, starts_on=date(2026, 1, 5), ends_on=date(2026, 1, 15))
    a, b, c, d = (PlayerFactory(club=club, name=n) for n in ("Ada", "Bob", "Cy", "Di"))
    for day in range(20):
        if day % 5 == 4:
            _won([a, b], [c, d], day=day, club=club, winner=Side.TWO if day % 2 else Side.ONE)
        else:
            _won([a], [b if day % 2 else c], day=day, club=club, winner=Side.ONE if day % 3 else Side.TWO)
    return season, (a, b, c, d)


def _none_safe(row):
    return tuple((v is None, v if v is not None else 0) for v in row)


def _state():
    """Everything a rebuild may write, independent of pks it regenerates."""
    return (
        sorted(Player.objects.values_list("pk", "elo_rating", "elo_peak", "matches_for_elo")),
        sorted(EloHistory.objects.values_list("match_id", "player_id", "old_rating", "new_rating",
                                              "rating_change", "k_factor")),
        sorted(PlayerRating.all_objects.values_list("player_id", "club_id", "season_id", "format",
                                                    "rating", "peak", "matches"), key=_none_safe),
        sorted(RatingChange.objects.values_list("match_id", "player_id", "rating__season_id",
                                                "rating__format", "old_rating", "new_rating",
                                                "rating_change", "k_factor"), key=_none_safe),
    )


@pytest.mark.django_db
class TestRecalculateEloRebuildsRatings:
    def test_a_full_run_reproduces_live_history_confirmed_in_play_order(self):
        _season_history()
        live = _state()

        call_command('recalculate_elo', stdout=StringIO())

        assert _state() == live

    def test_a_full_run_repairs_corrupted_mirror_rows(self):
        _season_history()
        live = _state()
        PlayerRating.all_objects.update(rating=1111, matches=0)
        RatingChange.objects.all().delete()

        call_command('recalculate_elo', stdout=StringIO())

        assert _state() == live

    def test_two_runs_are_identical(self):
        _season_history()
        # Shuffle the stored state so the first run has real work to do.
        Player.objects.update(elo_rating=1234)

        call_command('recalculate_elo', stdout=StringIO())
        first = _state()
        call_command('recalculate_elo', stdout=StringIO())

        assert _state() == first

    def test_keeps_the_existing_output_style(self):
        _season_history()
        out = StringIO()

        call_command('recalculate_elo', stdout=out)

        text = out.getvalue()
        assert 'Found 20 confirmed matches to process' in text
        assert 'Resetting all player Elo ratings to 1500...' in text
        assert 'Successfully recalculated Elo for 20 matches' in text
        assert 'PlayerRating' in text


@pytest.mark.django_db
class TestRecalculateEloDryRunDiff:
    def test_dry_run_writes_nothing_even_when_things_differ(self):
        _season_history()
        Player.objects.filter(name="Ada").update(elo_rating=1999)
        PlayerRating.all_objects.filter(format="doubles").update(rating=1111)
        before = _state()

        call_command('recalculate_elo', '--dry-run', stdout=StringIO())

        assert _state() == before

    def test_a_clean_database_reports_zero_diffs(self):
        _season_history()
        out = StringIO()

        call_command('recalculate_elo', '--dry-run', stdout=out)

        assert '0 players differ' in out.getvalue()

    def test_a_seeded_divergence_is_reported_per_player_and_scope(self):
        season, (ada, bob, cy, di) = _season_history()
        Player.objects.filter(pk=ada.pk).update(elo_rating=1999)
        PlayerRating.all_objects.filter(player=cy, season=season, format="singles").update(peak=1777)
        out = StringIO()

        call_command('recalculate_elo', '--dry-run', stdout=out)

        text = out.getvalue()
        assert '2 players differ' in text
        ada_lines = [line for line in text.splitlines() if line.strip().startswith("Ada")]
        assert any("legacy" in line and "1999" in line for line in ada_lines)
        cy_lines = [line for line in text.splitlines() if line.strip().startswith("Cy")]
        assert any(season.name in line and "1777" in line for line in cy_lines)
        assert not any(line.strip().startswith("Bob") for line in text.splitlines())

    def test_a_missing_row_is_reported(self):
        _season_history()
        PlayerRating.all_objects.filter(format="doubles", season=None).delete()
        out = StringIO()

        call_command('recalculate_elo', '--dry-run', stdout=out)

        assert '4 players differ' in out.getvalue()
        assert 'missing' in out.getvalue()


@pytest.mark.django_db
class TestRecalculateEloScopes:
    def test_format_scope_rebuilds_only_that_format(self):
        _season_history()
        live = _state()
        PlayerRating.all_objects.update(rating=1111)
        Player.objects.update(elo_rating=1222)
        history_pks = sorted(EloHistory.objects.values_list("pk", flat=True))

        call_command('recalculate_elo', '--format', 'doubles', stdout=StringIO())

        doubles = PlayerRating.all_objects.filter(format="doubles")
        assert doubles.exists() and not doubles.filter(rating=1111).exists()
        assert set(PlayerRating.all_objects.filter(format="singles").values_list("rating", flat=True)) == {1111}
        # Legacy and EloHistory are only rewritten by a full, unscoped run.
        assert set(Player.objects.values_list("elo_rating", flat=True)) == {1222}
        assert sorted(EloHistory.objects.values_list("pk", flat=True)) == history_pks
        assert [r for r in _state()[2] if r[3] == "doubles"] == [r for r in live[2] if r[3] == "doubles"]

    def test_season_scope_rebuilds_only_that_season(self):
        season, _ = _season_history()
        live = _state()
        PlayerRating.all_objects.update(rating=1111)

        call_command('recalculate_elo', '--season', str(season.pk), stdout=StringIO())

        assert not PlayerRating.all_objects.filter(season=season, rating=1111).exists()
        assert set(PlayerRating.all_objects.filter(season=None).values_list("rating", flat=True)) == {1111}
        assert [r for r in _state()[2] if r[2] == season.pk] == [r for r in live[2] if r[2] == season.pk]

    def test_season_and_format_scopes_combine(self):
        season, _ = _season_history()
        PlayerRating.all_objects.update(rating=1111)

        call_command('recalculate_elo', '--season', str(season.pk), '--format', 'singles',
                     stdout=StringIO())

        rebuilt = PlayerRating.all_objects.exclude(rating=1111)
        assert set(rebuilt.values_list("season_id", "format")) == {(season.pk, "singles")}

    def test_an_unknown_season_is_an_error(self):
        with pytest.raises(CommandError):
            call_command('recalculate_elo', '--season', '999999', stdout=StringIO())


@pytest.mark.django_db
class TestRecalculateEloIsAllOrNothing:
    def test_an_interruption_mid_run_leaves_everything_old(self):
        _season_history()
        Player.objects.update(elo_rating=1234)
        PlayerRating.all_objects.update(rating=1111)
        before = _state()
        real_add = elo_replay.RatingWriter.add
        calls = []

        def explode_halfway(self, match_id, changes):
            calls.append(match_id)
            if len(calls) == 10:
                raise RuntimeError("power cut")
            return real_add(self, match_id, changes)

        with mock.patch.object(elo_replay.RatingWriter, "add", explode_halfway):
            with pytest.raises(RuntimeError):
                call_command('recalculate_elo', '--chunk-size', '3', stdout=StringIO())

        assert _state() == before
