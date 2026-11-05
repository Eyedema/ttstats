"""
Tests for management commands.
"""

import re
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from itertools import pairwise
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from .. import rating_replay
from ..models import EloHistory, PlayerRating, RatingChange, Side
from .conftest import (
    ClubFactory,
    GameFactory,
    MatchFactory,
    PlayerFactory,
    PlayerRatingFactory,
    SeasonFactory,
    confirm_match,
    confirm_match_silent,
)


def _win_3_0(match):
    GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
    GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)
    GameFactory(match=match, game_number=3, team1_score=11, team2_score=9)
    match.refresh_from_db()


def _all_time(player, fmt="singles"):
    return PlayerRating.all_objects.get(player=player, season=None, format=fmt)


def _summary(text):
    """(rows that differ, unexplained, explained) from the dry-run summary line."""
    m = re.search(r"Dry run: (\d+) scope rows? differ \((\d+) unexplained, (\d+) explained", text)
    assert m, text
    return tuple(int(g) for g in m.groups())


@pytest.mark.django_db
class TestRecalculateRatings:
    """Test the recalculate_ratings management command (was recalculate_elo)."""

    def test_dry_run_no_changes(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        match = MatchFactory(player1=p1, player2=p2)
        _win_3_0(match)
        confirm_match(match)
        before = sorted(PlayerRating.all_objects.values_list(
            "pk", "matches", "glicko_rating", "rd", "volatility", "last_rated_on"))
        changes = sorted(RatingChange.objects.values_list("pk", flat=True))
        assert before

        out = StringIO()
        call_command('recalculate_ratings', '--dry-run', stdout=out)

        assert sorted(PlayerRating.all_objects.values_list(
            "pk", "matches", "glicko_rating", "rd", "volatility", "last_rated_on")) == before
        assert sorted(RatingChange.objects.values_list("pk", flat=True)) == changes
        output = out.getvalue()
        assert 'DRY RUN' in output
        assert 'Found 1 confirmed matches' in output

    def test_recalculate_resets_stored_state_to_the_replay(self):
        """Whatever is stored, a run rebuilds from the unrated base."""
        p1, p2 = PlayerFactory(), PlayerFactory()
        PlayerRatingFactory(player=p1, matches=50, glicko_rating=1700.0, rd=50.0,
                            volatility=0.06, last_rated_on=date(2024, 1, 1))
        PlayerRatingFactory(player=p2, matches=40, glicko_rating=1300.0, rd=50.0,
                            volatility=0.06, last_rated_on=date(2024, 1, 1))
        match = MatchFactory(player1=p1, player2=p2)
        _win_3_0(match)
        confirm_match_silent(match)  # no live write before the command

        call_command('recalculate_ratings', stdout=StringIO())

        r1, r2 = _all_time(p1), _all_time(p2)
        assert r1.glicko_rating > 1500 > r2.glicko_rating
        assert r1.glicko_rating + r2.glicko_rating == pytest.approx(3000)  # from 1500/1500
        assert (r1.matches, r2.matches) == (1, 1)

    def test_recalculate_chronological_order(self):
        """Matches replay in play order: each starts from the previous result."""
        from django.utils import timezone

        p1, p2 = PlayerFactory(), PlayerFactory()
        base_date = timezone.make_aware(datetime(2024, 1, 1, 12, 0, 0))
        matches = []
        for i in (2, 0, 1):  # created out of order
            match = MatchFactory(player1=p1, player2=p2, date_played=base_date + timedelta(days=i))
            _win_3_0(match)
            confirm_match_silent(match)
            matches.append((i, match))
        matches = [m for _, m in sorted(matches, key=lambda t: t[0])]

        call_command('recalculate_ratings', stdout=StringIO())

        chain = [RatingChange.objects.get(match=m, player=p1) for m in matches]
        assert chain[0].glicko_old_rating == 1500
        for before, after in pairwise(chain):
            assert after.glicko_old_rating == before.glicko_new_rating
        assert chain[0].glicko_new_rating < chain[1].glicko_new_rating < chain[2].glicko_new_rating
        assert _all_time(p1).glicko_rating == chain[2].glicko_new_rating
        assert (_all_time(p1).matches, _all_time(p2).matches) == (3, 3)
        assert RatingChange.objects.count() == 6

    def test_a_full_run_leaves_elo_history_untouched(self):
        """EloHistory is frozen pre-KAN-73 history: never deleted, never written."""
        p1, p2 = PlayerFactory(), PlayerFactory()
        match = MatchFactory(player1=p1, player2=p2)
        EloHistory.objects.create(match=match, player=p1, old_rating=1500, new_rating=1516,
                                  rating_change=16, k_factor=32.0)
        _win_3_0(match)
        confirm_match(match)
        frozen = list(EloHistory.objects.values_list(
            "pk", "match_id", "player_id", "old_rating", "new_rating", "rating_change", "k_factor"))

        out = StringIO()
        call_command('recalculate_ratings', stdout=out)

        assert list(EloHistory.objects.values_list(
            "pk", "match_id", "player_id", "old_rating", "new_rating", "rating_change",
            "k_factor")) == frozen
        assert RatingChange.objects.filter(match=match).count() == 2
        assert 'EloHistory' not in out.getvalue()

    def test_an_empty_database(self):
        out = StringIO()

        call_command('recalculate_ratings', stdout=out)

        text = out.getvalue()
        assert 'Found 0 confirmed matches' in text
        assert 'Rebuilt 0 PlayerRating rows and 0 rating changes' in text
        assert not PlayerRating.all_objects.exists()
        assert not RatingChange.objects.exists()

    def test_an_empty_database_dry_run(self):
        out = StringIO()

        call_command('recalculate_ratings', '--dry-run', stdout=out)

        assert _summary(out.getvalue()) == (0, 0, 0)

    def test_skip_unconfirmed_matches(self):
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        for p in (p1, p2):
            p.user.profile.email_verified = True
            p.user.profile.save()

        match1 = MatchFactory(player1=p1, player2=p2)
        _win_3_0(match1)
        confirm_match(match1)
        # Unconfirmed (will NOT auto-confirm because players are verified)
        match2 = MatchFactory(player1=p1, player2=p2)
        _win_3_0(match2)

        out = StringIO()
        call_command('recalculate_ratings', stdout=out)

        assert 'Found 1 confirmed matches' in out.getvalue()
        assert set(RatingChange.objects.values_list("match_id", flat=True)) == {match1.pk}
        assert RatingChange.objects.count() == 2


# ---------------------------------------------------------------------------
# KAN-49 (S4.7): the rebuild of every PlayerRating scope
# ---------------------------------------------------------------------------

KAN49_BASE = datetime(2026, 1, 1, 12, tzinfo=UTC)


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


def _rounded(row):
    return tuple(round(v, 6) if isinstance(v, float) else v for v in row)


def _state():
    """Everything a rebuild may write (or must not touch), independent of regenerated pks."""
    return (
        sorted(EloHistory.objects.values_list("match_id", "player_id", "old_rating", "new_rating",
                                              "rating_change", "k_factor")),
        sorted((_rounded(r) for r in PlayerRating.all_objects.values_list(
            "player_id", "club_id", "season_id", "format", "matches",
            "glicko_rating", "rd", "volatility", "last_rated_on")), key=_none_safe),
        sorted((_rounded(r) for r in RatingChange.objects.values_list(
            "match_id", "player_id", "rating__season_id", "rating__format",
            "glicko_old_rating", "glicko_new_rating", "glicko_change",
            "glicko_rd_before", "glicko_rd_after",
            "elo_old_rating", "elo_new_rating", "elo_change", "elo_k_factor")), key=_none_safe),
    )


@pytest.mark.django_db
class TestRecalculateRatingsRebuildsRatings:
    def test_a_full_run_reproduces_live_history_confirmed_in_play_order(self):
        _season_history()
        live = _state()

        call_command('recalculate_ratings', stdout=StringIO())

        assert _state() == live

    def test_a_full_run_repairs_corrupted_rows(self):
        _season_history()
        live = _state()
        PlayerRating.all_objects.update(glicko_rating=1111.0, matches=0)
        RatingChange.objects.all().delete()

        call_command('recalculate_ratings', stdout=StringIO())

        assert _state() == live

    def test_two_runs_are_identical(self):
        _season_history()
        # Shuffle the stored state so the first run has real work to do.
        PlayerRating.all_objects.update(glicko_rating=1234.0, rd=99.0)

        call_command('recalculate_ratings', stdout=StringIO())
        first = _state()
        call_command('recalculate_ratings', stdout=StringIO())

        assert _state() == first

    def test_output(self):
        _season_history()
        out = StringIO()

        call_command('recalculate_ratings', stdout=out)

        text = out.getvalue()
        assert 'Found 20 confirmed matches to process' in text
        assert 'Recalculating ratings...' in text
        assert 'Successfully recalculated ratings for 20 matches' in text
        assert 'PlayerRating' in text


@pytest.mark.django_db
class TestRecalculateRatingsDryRunDiff:
    def test_dry_run_writes_nothing_even_when_things_differ(self):
        _season_history()
        PlayerRating.all_objects.filter(format="doubles").update(glicko_rating=1111.0)
        before = _state()

        call_command('recalculate_ratings', '--dry-run', stdout=StringIO())

        assert _state() == before

    def test_a_clean_database_reports_zero_diffs(self):
        _season_history()
        out = StringIO()

        call_command('recalculate_ratings', '--dry-run', stdout=out)

        assert _summary(out.getvalue()) == (0, 0, 0)

    def test_a_seeded_divergence_is_reported_per_player_and_scope(self):
        season, (ada, _bob, cy, _di) = _season_history()
        PlayerRating.all_objects.filter(player=ada, season=None, format="singles").update(
            glicko_rating=1999.0)
        PlayerRating.all_objects.filter(player=cy, season=season, format="singles").update(
            rd=177.5)
        out = StringIO()

        call_command('recalculate_ratings', '--dry-run', stdout=out)

        text = out.getvalue()
        assert _summary(text) == (2, 2, 0)
        assert 'Per-player differences (stored -> recomputed):' in text
        ada_lines = [line for line in text.splitlines() if line.strip().startswith("Ada")]
        assert any("all-time" in line and "1999" in line for line in ada_lines)
        cy_lines = [line for line in text.splitlines() if line.strip().startswith("Cy")]
        assert any(season.name in line and "177.5" in line for line in cy_lines)
        assert not any(line.strip().startswith("Bob") for line in text.splitlines())

    def test_a_tampered_match_count_is_reported(self):
        _season_history()
        PlayerRating.all_objects.filter(format="doubles", season=None).update(matches=99)
        out = StringIO()

        call_command('recalculate_ratings', '--dry-run', stdout=out)

        assert _summary(out.getvalue())[0] == 4
        assert 'matches: 99 ->' in out.getvalue()

    def test_a_missing_row_is_reported(self):
        _season_history()
        PlayerRating.all_objects.filter(format="doubles", season=None).delete()
        out = StringIO()

        call_command('recalculate_ratings', '--dry-run', stdout=out)

        assert _summary(out.getvalue())[0] == 4
        assert 'missing' in out.getvalue()


@pytest.mark.django_db
class TestRecalculateRatingsScopes:
    def test_format_scope_rebuilds_only_that_format(self):
        _season_history()
        live = _state()
        PlayerRating.all_objects.update(glicko_rating=1111.0)

        call_command('recalculate_ratings', '--format', 'doubles', stdout=StringIO())

        doubles = PlayerRating.all_objects.filter(format="doubles")
        assert doubles.exists() and not doubles.filter(glicko_rating=1111.0).exists()
        singles = PlayerRating.all_objects.filter(format="singles")
        assert set(singles.values_list("glicko_rating", flat=True)) == {1111.0}
        assert [r for r in _state()[1] if r[3] == "doubles"] == [r for r in live[1] if r[3] == "doubles"]
        assert _state()[0] == live[0]  # EloHistory untouched

    def test_season_scope_rebuilds_only_that_season(self):
        season, _ = _season_history()
        live = _state()
        PlayerRating.all_objects.update(glicko_rating=1111.0)

        call_command('recalculate_ratings', '--season', str(season.pk), stdout=StringIO())

        assert not PlayerRating.all_objects.filter(season=season, glicko_rating=1111.0).exists()
        all_time = PlayerRating.all_objects.filter(season=None)
        assert set(all_time.values_list("glicko_rating", flat=True)) == {1111.0}
        assert [r for r in _state()[1] if r[2] == season.pk] == [r for r in live[1] if r[2] == season.pk]

    def test_season_and_format_scopes_combine(self):
        season, _ = _season_history()
        PlayerRating.all_objects.update(glicko_rating=1111.0)

        call_command('recalculate_ratings', '--season', str(season.pk), '--format', 'singles',
                     stdout=StringIO())

        rebuilt = PlayerRating.all_objects.exclude(glicko_rating=1111.0)
        assert set(rebuilt.values_list("season_id", "format")) == {(season.pk, "singles")}

    def test_an_unknown_season_is_an_error(self):
        with pytest.raises(CommandError):
            call_command('recalculate_ratings', '--season', '999999', stdout=StringIO())


@pytest.mark.django_db
class TestRecalculateRatingsIsAllOrNothing:
    def test_an_interruption_mid_run_leaves_everything_old(self):
        _season_history()
        PlayerRating.all_objects.update(glicko_rating=1111.0)
        before = _state()
        real_add = rating_replay.RatingWriter.add
        calls = []

        def explode_halfway(self, match_id, changes):
            calls.append(match_id)
            if len(calls) == 10:
                raise RuntimeError("power cut")
            return real_add(self, match_id, changes)

        with (
            mock.patch.object(rating_replay.RatingWriter, "add", explode_halfway),
            pytest.raises(RuntimeError),
        ):
            call_command('recalculate_ratings', '--chunk-size', '3', stdout=StringIO())

        assert _state() == before


# run_scheduled_jobs (KAN-37 / T3.6). Imports are local to this section so it
# merges cleanly with concurrent work on the recalculate_ratings tests above.


@pytest.fixture
def job_registry(monkeypatch):
    """An empty job registry, so the command never runs a real job by accident."""
    from pingpong import jobs

    fresh = {}
    monkeypatch.setattr(jobs, "_REGISTRY", fresh)
    monkeypatch.setattr(jobs, "_loaded", True)
    return fresh


def _register_counter(name):
    """Register a job that records each call's dry_run flag."""
    from pingpong import jobs

    calls = []

    @jobs.register(name)
    def _job(*, dry_run):
        calls.append(dry_run)
        return f"{name} {'would do' if dry_run else 'did'} 1 thing"

    return calls


@pytest.mark.django_db
class TestRunScheduledJobs:
    """The dispatcher host cron calls."""

    def test_runs_every_registered_job(self, job_registry):
        from pingpong.models import JobRun

        a, b = _register_counter("alpha"), _register_counter("beta")
        out = StringIO()

        call_command("run_scheduled_jobs", stdout=out)

        assert a == [False] and b == [False]
        assert JobRun.objects.filter(status=JobRun.Status.SUCCESS).count() == 2
        assert "alpha did 1 thing" in out.getvalue()

    def test_job_option_runs_only_that_job(self, job_registry):
        a, b = _register_counter("alpha"), _register_counter("beta")

        call_command("run_scheduled_jobs", "--job", "beta", stdout=StringIO())

        assert a == [] and b == [False]

    def test_unknown_job_is_a_command_error(self, job_registry):
        from django.core.management.base import CommandError

        with pytest.raises(CommandError):
            call_command("run_scheduled_jobs", "--job", "nope", stdout=StringIO())

    def test_dry_run_is_passed_to_every_job_and_writes_nothing(self, job_registry):
        from pingpong.models import JobRun

        a, b = _register_counter("alpha"), _register_counter("beta")
        out = StringIO()

        call_command("run_scheduled_jobs", "--dry-run", stdout=out)

        assert a == [True] and b == [True]
        assert JobRun.objects.count() == 0
        assert "DRY RUN" in out.getvalue()

    def test_list_runs_nothing(self, job_registry):
        a = _register_counter("alpha")
        out = StringIO()

        call_command("run_scheduled_jobs", "--list", stdout=out)

        assert a == []
        assert "alpha" in out.getvalue()

    def test_held_lock_means_the_second_run_does_nothing(self, job_registry):
        from django.core.cache import cache

        from pingpong import cache_utils

        a = _register_counter("alpha")
        cache.add(cache_utils.job_lock_key("alpha"), "other-process", 60)
        out = StringIO()

        call_command("run_scheduled_jobs", stdout=out)

        assert a == []
        assert "skipped" in out.getvalue().lower()

    def test_a_failing_job_is_reported_not_raised(self, job_registry):
        from pingpong import jobs
        from pingpong.models import JobRun

        @jobs.register("broken")
        def _broken(*, dry_run):
            raise RuntimeError("kaboom")

        out, err = StringIO(), StringIO()
        call_command("run_scheduled_jobs", stdout=out, stderr=err)

        assert JobRun.objects.get().status == JobRun.Status.FAILED
        assert "broken" in err.getvalue()

    def test_prune_job_runs_end_to_end_is_idempotent(self):
        # The one real job, through the real registry.
        from datetime import timedelta

        from django.utils import timezone

        from pingpong import scheduled_jobs
        from pingpong.models import JobRun

        from .conftest import JobRunFactory

        JobRunFactory(
            started_at=timezone.now()
            - timedelta(days=scheduled_jobs.JOB_RUN_RETENTION_DAYS + 1)
        )

        call_command("run_scheduled_jobs", "--job", "prune_job_runs", stdout=StringIO())
        assert JobRun.objects.count() == 1  # the aged row is gone; this run's row stays
        call_command("run_scheduled_jobs", "--job", "prune_job_runs", stdout=StringIO())

        assert JobRun.objects.count() == 2
        assert set(JobRun.objects.values_list("name", flat=True)) == {"prune_job_runs"}
