"""The Glicko-2 shadow on PlayerRating (KAN-67).

Computed and stored beside Elo, read by nothing. The load-bearing promises:
the shadow can never break the Elo write, the preview is the persisted value,
and the replay agrees with the live path.
"""

import logging
import math
import re
from datetime import UTC, date, datetime
from pathlib import Path
from unittest import mock

import pytest
from django.core.management import call_command

from .. import elo, glicko2, glicko_adapter
from ..elo import LiveBook, RatingScope, update_player_elo
from ..glicko2 import Rating
from ..glicko_adapter import DEFAULT_PARAMS, GlickoParams, days_to_periods
from ..models import EloHistory, Match, Player, PlayerRating, RatingChange
from .conftest import (
    ClubFactory,
    GameFactory,
    MatchFactory,
    PlayerFactory,
    PlayerRatingFactory,
    SeasonFactory,
    confirm_match_silent,
)
from .test_migrations import _columns, _leaf, _migrate_to, at_head  # noqa: F401

SINGLES = RatingScope(None, "singles")
FIELDS = ("glicko_rating", "rd", "volatility", "last_rated_on")


def _at(year, month, day):
    return datetime(year, month, day, 20, 0, tzinfo=UTC)


def _finish(side1, side2, *, club=None, when=None, best_of=5):
    """A won match; players without accounts auto-confirm, so Elo is applied."""
    extra = {"club": club} if club is not None else {}
    if when is not None:
        extra["date_played"] = when
    match = MatchFactory(
        team1_players=list(side1),
        team2_players=list(side2),
        is_double=len(side1) > 1,
        best_of=best_of,
        **extra,
    )
    for n in range(1, best_of // 2 + 2):
        GameFactory(match=match, game_number=n, team1_score=11, team2_score=5)
    match.refresh_from_db()
    return match


def _pending(club=None, when=None):
    """A won, confirmed match whose Elo has NOT been applied yet."""
    players = [
        PlayerFactory(with_user=True, **({"club": club} if club else {}))
        for _ in range(2)
    ]
    for p in players:
        p.user.profile.email_verified = True
        p.user.profile.save()
    match = _finish([players[0]], [players[1]], club=club, when=when)
    confirm_match_silent(match)
    match.invalidate_confirmation_state()
    return match, players[0], players[1]


def _finish_pending(p1, p2, club, when):
    """Another won, confirmed-but-unapplied match between two verified players."""
    match = _finish([p1], [p2], club=club, when=when)
    confirm_match_silent(match)
    match.invalidate_confirmation_state()
    return match


def _row(player, season=None, fmt="singles"):
    return PlayerRating.all_objects.get(player=player, season=season, format=fmt)


def _state(row):
    return tuple(getattr(row, f) for f in FIELDS)


def _season(club, start=date(2026, 1, 1), end=date(2026, 3, 31)):
    return SeasonFactory(club=club, starts_on=start, ends_on=end)


@pytest.mark.django_db
class TestConfirmationWritesTheShadow:
    def test_all_four_fields_on_the_all_time_and_season_rows(self):
        club = ClubFactory()
        season = _season(club)
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)

        _finish([p1], [p2], club=club, when=_at(2026, 2, 1))

        for player in (p1, p2):
            for scope_season in (None, season):
                row = _row(player, scope_season)
                assert all(getattr(row, f) is not None for f in FIELDS)
                assert row.last_rated_on == date(2026, 2, 1)
        assert _row(p1).glicko_rating > 1500 > _row(p2).glicko_rating

    def test_a_match_outside_every_season_writes_only_the_all_time_shadow(self):
        club = ClubFactory()
        _season(club)
        p1, p2 = PlayerFactory(club=club), PlayerFactory(club=club)

        _finish([p1], [p2], club=club, when=_at(2026, 6, 1))

        assert PlayerRating.all_objects.filter(player=p1).count() == 1
        assert _row(p1).glicko_rating is not None

    def test_elo_is_unchanged_by_the_shadow(self):
        p1, p2 = PlayerFactory(), PlayerFactory()

        match = _finish([p1], [p2])

        p1.refresh_from_db()
        k = EloHistory.objects.get(match=match, player=p1).k_factor
        assert p1.elo_rating == 1500 + round(k * 0.5)  # 50/50: the Elo arithmetic
        assert _row(p1).rating == p1.elo_rating

    def test_a_doubles_match_confirms_and_leaves_the_shadow_null(self):
        players = [PlayerFactory() for _ in range(4)]

        match = _finish(players[:2], players[2:])

        assert EloHistory.objects.filter(match=match).count() == 4
        rows = PlayerRating.all_objects.filter(format="doubles")
        assert rows.count() == 4
        assert all(_state(r) == (None, None, None, None) for r in rows)

    def test_doubles_project_to_none(self):
        players = [PlayerFactory() for _ in range(4)]
        match = _finish(players[:2], players[2:])

        assert (
            glicko_adapter.projected_glicko_changes(match, RatingScope(None, "doubles"))
            is None
        )

    def test_a_match_with_no_winner_projects_to_none(self):
        match = MatchFactory(
            team1_players=[PlayerFactory()], team2_players=[PlayerFactory()]
        )

        assert glicko_adapter.projected_glicko_changes(match, SINGLES) is None


@pytest.mark.django_db
class TestIdleInflation:
    def test_days_to_periods(self):
        params = GlickoParams(tau=0.5, period_days=7, season_rd_floor=100.0)
        assert days_to_periods(None, date(2026, 1, 1), params) == 0
        assert days_to_periods(date(2026, 1, 1), date(2026, 1, 1), params) == 0
        assert days_to_periods(date(2026, 1, 1), date(2026, 1, 15), params) == 2
        assert (
            days_to_periods(date(2026, 1, 15), date(2026, 1, 1), params) == 0
        )  # clamped

    def test_a_long_gap_gives_a_larger_pre_match_rd(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        _finish([p1], [p2], when=_at(2026, 1, 1))
        match = _finish([p1], [p2], when=_at(2026, 1, 1))  # builds history on one day
        book = LiveBook(match)

        same_day = glicko_adapter.glicko_reading(
            book, p1.pk, SINGLES, date(2026, 1, 1), DEFAULT_PARAMS
        )
        sixty_days = glicko_adapter.glicko_reading(
            book, p1.pk, SINGLES, date(2026, 3, 2), DEFAULT_PARAMS
        )

        assert same_day.rd == pytest.approx(_row(p1).rd)
        assert sixty_days.rd > same_day.rd
        assert sixty_days.rating == same_day.rating

    def test_the_inflated_rd_is_capped_at_rd_max(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        match = _finish([p1], [p2], when=_at(2026, 1, 1))

        reading = glicko_adapter.glicko_reading(
            LiveBook(match), p1.pk, SINGLES, date(2036, 1, 1), DEFAULT_PARAMS
        )

        assert reading.rd <= DEFAULT_PARAMS.rd_max

    def test_an_unrated_player_reads_the_base_state(self):
        match = _finish([PlayerFactory()], [PlayerFactory()])

        reading = glicko_adapter.glicko_reading(
            LiveBook(match), 987654, SINGLES, date(2026, 1, 1), DEFAULT_PARAMS
        )

        assert reading == Rating(1500.0, 350.0, 0.06)

    def test_a_row_with_null_glicko_fields_reads_the_base_state(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        PlayerRatingFactory(player=p1, rating=1700, matches=10)
        match = _finish([p1], [p2], when=_at(2026, 1, 1))
        PlayerRating.all_objects.filter(player=p1).update(
            glicko_rating=None, rd=None, volatility=None
        )

        reading = glicko_adapter.glicko_reading(
            LiveBook(match), p1.pk, SINGLES, date(2026, 1, 2), DEFAULT_PARAMS
        )

        assert reading == Rating(1500.0, 350.0, 0.06)

    def test_a_season_row_with_null_glicko_fields_is_seeded_from_the_all_time_row(self):
        club = ClubFactory()
        season = _season(club)
        match, p1, _p2 = _pending(club=club, when=_at(2026, 2, 1))
        PlayerRatingFactory(
            player=p1,
            rating=1700,
            matches=40,
            glicko_rating=1800.0,
            rd=60.0,
            volatility=0.05,
            last_rated_on=date(2026, 2, 1),
        )
        PlayerRatingFactory(player=p1, season=season, club=club, rating=1600, matches=1)

        seed = glicko_adapter.glicko_reading(
            LiveBook(match, season),
            p1.pk,
            RatingScope(season, "singles"),
            date(2026, 2, 1),
            DEFAULT_PARAMS,
        )

        assert seed.rating == 1650.0
        assert seed.rd == DEFAULT_PARAMS.season_rd_floor
        assert seed.volatility == 0.05

    def test_a_failed_first_season_projection_is_seeded_on_the_next_match(self):
        club = ClubFactory()
        season = _season(club)
        first, p1, p2 = _pending(club=club, when=_at(2026, 2, 1))
        real_rate, calls = glicko2.rate, []

        def fail_for_the_season_scope(*args, **kwargs):
            calls.append(1)
            if len(calls) > 2:  # all-time scope rates both players first
                raise glicko2.VolatilityConvergenceError("season scope only")
            return real_rate(*args, **kwargs)

        with mock.patch.object(glicko2, "rate", fail_for_the_season_scope):
            update_player_elo(first)

        assert _row(p1).glicko_rating is not None
        assert _state(_row(p1, season)) == (None, None, None, None)
        assert _row(p1, season).matches == 1  # Elo still wrote the season row

        second = _finish_pending(p1, p2, club, _at(2026, 2, 2))
        projection = glicko_adapter.projected_glicko_changes(
            Match.all_objects.get(pk=second.pk), RatingScope(season, "singles")
        )
        all_time = _row(p1)
        assert projection.updates[0].old.rating == pytest.approx(
            1500 + 0.5 * (all_time.glicko_rating - 1500)
        )
        update_player_elo(second)
        assert _row(p1, season).glicko_rating == pytest.approx(
            projection.updates[0].new.rating
        )


@pytest.mark.django_db
class TestOutOfOrderConfirmation:
    def test_the_gap_is_clamped_and_last_rated_on_never_moves_back(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        _finish([p1], [p2], when=_at(2026, 3, 10))
        rd_after_first = _row(p1).rd

        _finish([p1], [p2], when=_at(2026, 3, 1))  # confirmed later, played earlier

        row = _row(p1)
        assert row.last_rated_on == date(2026, 3, 10)
        assert row.rd != rd_after_first  # still rated, from a zero-day gap
        assert math.isfinite(row.glicko_rating)

    def test_apply_glicko_keeps_the_later_day(self):
        row = mock.Mock(last_rated_on=date(2026, 3, 10))
        update = glicko_adapter.GlickoUpdate(
            1, Rating(1500, 350, 0.06), Rating(1500, 350, 0.06), Rating(1510, 300, 0.06)
        )

        glicko_adapter.apply_glicko(row, update, date(2026, 3, 1))
        assert row.last_rated_on == date(2026, 3, 10)
        glicko_adapter.apply_glicko(row, update, date(2026, 3, 12))
        assert row.last_rated_on == date(2026, 3, 12)
        assert (row.glicko_rating, row.rd, row.volatility) == (1510, 300, 0.06)


@pytest.mark.django_db
class TestSeasonSeeding:
    def test_a_first_season_row_is_the_float_carry_over_with_the_rd_floor(self):
        club = ClubFactory()
        season = _season(club)
        match, p1, _p2 = _pending(club=club, when=_at(2026, 2, 1))
        PlayerRatingFactory(
            player=p1,
            rating=1700,
            matches=40,
            glicko_rating=1801.0,
            rd=60.0,
            volatility=0.05,
            last_rated_on=date(2026, 1, 20),
        )
        scope = RatingScope(season, "singles")
        book = LiveBook(match, season)

        seed = glicko_adapter.glicko_reading(
            book, p1.pk, scope, date(2026, 2, 1), DEFAULT_PARAMS
        )

        assert seed.rating == 1650.5  # 1500 + 0.5 * (1801 - 1500), unrounded
        assert (
            seed.rd == DEFAULT_PARAMS.season_rd_floor
        )  # all-time RD (~60) is below the floor
        assert seed.volatility == 0.05

    def test_a_high_all_time_rd_beats_the_floor(self):
        club = ClubFactory()
        season = _season(club)
        match, p1, _p2 = _pending(club=club, when=_at(2026, 2, 1))
        PlayerRatingFactory(
            player=p1,
            rating=1500,
            matches=40,
            glicko_rating=1500.0,
            rd=250.0,
            volatility=0.06,
            last_rated_on=date(2026, 2, 1),
        )

        seed = glicko_adapter.glicko_reading(
            LiveBook(match, season),
            p1.pk,
            RatingScope(season, "singles"),
            date(2026, 2, 1),
            DEFAULT_PARAMS,
        )

        assert seed.rd == pytest.approx(250.0)

    def test_the_seed_is_written_through_to_the_season_row(self):
        club = ClubFactory()
        season = _season(club)
        match, p1, _p2 = _pending(club=club, when=_at(2026, 2, 1))
        PlayerRatingFactory(
            player=p1,
            rating=1700,
            matches=40,
            glicko_rating=1800.0,
            rd=60.0,
            volatility=0.06,
            last_rated_on=date(2026, 2, 1),
        )
        projection = glicko_adapter.projected_glicko_changes(
            Match.all_objects.get(pk=match.pk), RatingScope(season, "singles")
        )

        update_player_elo(match)

        season_row = _row(p1, season)
        assert season_row.glicko_rating == pytest.approx(
            projection.updates[0].new.rating
        )
        assert season_row.rd == pytest.approx(projection.updates[0].new.rd)
        assert projection.updates[0].old.rating == 1650.0
        assert (
            season_row.rating != _row(p1).rating
        )  # the shadow rows are separate books


@pytest.mark.django_db
class TestFailureIsolation:
    def _seeded(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        for p in (p1, p2):
            PlayerRatingFactory(
                player=p,
                matches=3,
                glicko_rating=1520.0,
                rd=120.0,
                volatility=0.06,
                last_rated_on=date(2026, 1, 1),
            )
        match = MatchFactory(
            team1_players=[p1], team2_players=[p2], date_played=_at(2026, 1, 5)
        )
        return match, p1, p2

    def _win_unconfirmed_then_apply(self, match):
        for n in (1, 2, 3):
            GameFactory(match=match, game_number=n, team1_score=11, team2_score=5)

    def test_a_convergence_error_costs_the_shadow_not_the_elo(self, caplog):
        match, p1, _p2 = self._seeded()
        before = _state(_row(p1))
        boom = mock.Mock(
            side_effect=glicko2.VolatilityConvergenceError("no convergence")
        )

        with mock.patch.object(glicko2, "rate", boom), caplog.at_level(logging.ERROR):
            self._win_unconfirmed_then_apply(match)

        assert boom.called
        assert EloHistory.objects.filter(match=match).count() == 2
        assert RatingChange.objects.filter(match=match).count() == 2
        assert _row(p1).rating == Player.objects.get(pk=p1.pk).elo_rating > 1500
        assert _state(_row(p1)) == before
        assert "Glicko-2 shadow skipped" in caplog.text
        assert "VolatilityConvergenceError" in caplog.text

    def test_a_nan_result_is_rejected_the_same_way(self, caplog):
        match, p1, _p2 = self._seeded()
        before = _state(_row(p1))
        nan = mock.Mock(return_value=Rating(float("nan"), 100.0, 0.06))

        with mock.patch.object(glicko2, "rate", nan), caplog.at_level(logging.ERROR):
            self._win_unconfirmed_then_apply(match)

        assert EloHistory.objects.filter(match=match).count() == 2
        assert RatingChange.objects.filter(match=match).count() == 2
        assert _state(_row(p1)) == before
        assert "non-finite" in caplog.text

    def test_the_shadow_recovers_on_the_next_match(self):
        match, p1, p2 = self._seeded()
        with mock.patch.object(
            glicko2, "rate", side_effect=glicko2.NonFiniteValueError("x")
        ):
            self._win_unconfirmed_then_apply(match)

        _finish([p1], [p2], when=_at(2026, 1, 9))

        assert _row(p1).last_rated_on == date(2026, 1, 9)


@pytest.mark.django_db
class TestIdempotencyAndRaces:
    def test_applying_twice_changes_the_shadow_once(self):
        match, p1, _p2 = _pending()

        update_player_elo(match)
        once = _state(_row(p1))
        update_player_elo(match)

        assert _state(_row(p1)) == once
        assert _row(p1).matches == 1

    def test_a_confirmer_that_loses_the_race_writes_the_shadow_once(self):
        match, p1, p2 = _pending()
        update_player_elo(match)
        once = (_state(_row(p1)), _state(_row(p2)))

        with mock.patch.object(elo, "_already_applied", side_effect=[False, True]):
            update_player_elo(match)

        assert (_state(_row(p1)), _state(_row(p2))) == once

    def test_a_retried_write_applies_the_shadow_once(self):
        match, p1, _p2 = _pending()
        real, calls = elo._apply_match, []

        def flaky(m):
            calls.append(1)
            if len(calls) == 1:
                raise elo.IntegrityError("conflict")
            return real(m)

        with mock.patch.object(elo, "_apply_match", side_effect=flaky):
            update_player_elo(match)

        assert len(calls) == 2
        assert _row(p1).glicko_rating > 1500
        assert RatingChange.objects.filter(match=match).count() == 2


@pytest.mark.django_db
class TestPreviewIsThePersistedValue:
    def test_projection_before_confirming_equals_what_is_written(self):
        match, p1, p2 = _pending(when=_at(2026, 2, 1))
        projection = glicko_adapter.projected_glicko_changes(
            Match.all_objects.get(pk=match.pk), SINGLES
        )

        update_player_elo(match)

        by_player = {u.player_id: u for u in projection.updates}
        for player in (p1, p2):
            row, update = _row(player), by_player[player.pk]
            assert row.glicko_rating == update.new.rating
            assert row.rd == update.new.rd
            assert row.volatility == update.new.volatility
        assert projection.side1_change == round(_row(p1).glicko_rating - 1500)
        assert projection.side1_change > 0 > projection.side2_change

    def test_a_player_is_rated_against_the_inflated_opponent(self):
        match, _p1, p2 = _pending(when=_at(2026, 3, 1))
        PlayerRatingFactory(
            player=p2,
            rating=1500,
            matches=5,
            glicko_rating=1500.0,
            rd=50.0,
            volatility=0.06,
            last_rated_on=date(2025, 3, 1),
        )
        projection = glicko_adapter.projected_glicko_changes(
            Match.all_objects.get(pk=match.pk), SINGLES
        )

        u1, u2 = projection.updates
        assert u2.inflated.rd > u2.old.rd  # a year idle
        assert u1.new == glicko2.rate(
            u1.inflated,
            [glicko2.Game(u2.inflated.rating, u2.inflated.rd, 1.0)],
            DEFAULT_PARAMS.tau,
            DEFAULT_PARAMS.rd_max,
        )

    def test_the_projection_does_not_read_the_clock(self):
        match, _p1, _p2 = _pending(when=_at(2026, 2, 1))
        match = Match.all_objects.get(pk=match.pk)
        first = glicko_adapter.projected_glicko_changes(match, SINGLES)

        with mock.patch(
            "django.utils.timezone.now", side_effect=AssertionError("clock read")
        ):
            second = glicko_adapter.projected_glicko_changes(match, SINGLES)

        assert first == second


@pytest.mark.django_db
class TestReplayMatchesLive:
    def test_recalculate_elo_rebuilds_the_same_shadow(self):
        club = ClubFactory()
        _season(club)
        p1, p2, p3 = (PlayerFactory(club=club) for _ in range(3))
        days = [_at(2026, 1, 3), _at(2026, 1, 3), _at(2026, 1, 20), _at(2026, 4, 2)]
        pairs = [(p1, p2), (p2, p3), (p1, p3), (p3, p1)]
        for when, (a, b) in zip(days, pairs):
            _finish([a], [b], club=club, when=when)

        def snapshot():
            return {
                (r.player_id, r.season_id, r.format): (r.rating, *_state(r))
                for r in PlayerRating.all_objects.all()
            }

        live = snapshot()
        assert any(v[1] is not None for v in live.values())

        call_command("recalculate_elo")
        replayed = snapshot()

        assert replayed.keys() == live.keys()
        for key, expected in live.items():
            got = replayed[key]
            assert got[0] == expected[0]
            assert got[1:4] == pytest.approx(expected[1:4])
            assert got[4] == expected[4]

    def test_the_writer_skips_the_shadow_on_a_model_without_it(self):
        from ..elo_replay import RatingWriter

        old_model = mock.Mock()
        old_model._meta.fields = [mock.Mock(name=n) for n in ("id", "rating", "peak")]
        for f, n in zip(old_model._meta.fields, ("id", "rating", "peak")):
            f.name = n

        assert RatingWriter(old_model, mock.Mock()).shadow_fields == []
        assert RatingWriter(PlayerRating, mock.Mock()).shadow_fields == list(FIELDS)


@pytest.mark.django_db(transaction=True)
class TestMigration:
    def test_the_fields_are_added_and_reverse_cleanly(self, at_head):  # noqa: F811
        assert set(FIELDS) <= _columns("pingpong_playerrating")

        _migrate_to("0043_season_standing")
        assert not set(FIELDS) & _columns("pingpong_playerrating")

        _migrate_to(_leaf())
        assert set(FIELDS) <= _columns("pingpong_playerrating")


class TestNothingReadsTheShadow:
    """The shadow is inert: no view, template, cache or serializer touches it."""

    ALLOWED = frozenset(
        {
            "elo.py",
            "elo_replay.py",
            "glicko_adapter.py",
            "glicko2.py",
            "models.py",
            "rating_divergence.py",  # KAN-66: the offline harness is a deliberate reader
        }
    )

    def test_no_application_code_reads_it(self):
        root = Path(__file__).resolve().parent.parent
        pattern = re.compile(r"glicko_rating|\bvolatility\b|last_rated_on")
        offenders = []
        for path in root.rglob("*"):
            rel = path.relative_to(root)
            if (
                path.suffix not in {".py", ".html", ".js", ".txt"}
                or "migrations" in rel.parts
                or "tests" in rel.parts
                or path.name in self.ALLOWED
            ):
                continue
            if pattern.search(path.read_text(errors="ignore")):
                offenders.append(str(rel))
        assert offenders == []


@pytest.mark.django_db
class TestTheGuardedRegionIsPure:
    def test_plan_glicko_refuses_to_build_its_own_book(self):
        match, _, _ = _pending()

        with pytest.raises(ValueError, match="needs a book"):
            elo._plan_glicko(match, SINGLES, None, DEFAULT_PARAMS)

    def test_the_guarded_projection_runs_no_queries(self, django_assert_num_queries):
        club = ClubFactory()
        season = _season(club)
        match, _p1, _p2 = _pending(club=club, when=_at(2026, 2, 1))
        book = LiveBook(match, season)
        match.sides()  # memoised, as _lock_sides leaves it

        with django_assert_num_queries(0):
            for scope in elo.format_scopes(match, season):
                assert elo._plan_glicko(match, scope, book, DEFAULT_PARAMS) is not None

    def test_the_guard_wraps_the_pure_function_only(self):
        match, _, _ = _pending()
        book = LiveBook(match)
        with mock.patch.object(
            glicko_adapter,
            "projected_glicko_changes",
            side_effect=AssertionError("queries"),
        ):
            assert elo._plan_glicko(match, SINGLES, book, DEFAULT_PARAMS) is not None


@pytest.mark.django_db
class TestARealRetryAppliesTheShadowOnce:
    def test_a_flush_that_fails_after_the_rows_mutated_is_a_single_application(self):
        plain, a1, a2 = _pending(when=_at(2026, 2, 1))
        update_player_elo(plain)
        baseline = (_state(_row(a1)), _state(_row(a2)))

        retried, b1, b2 = _pending(when=_at(2026, 2, 1))
        real_flush, calls = LiveBook.flush, []

        def flaky_flush(self, match, changes):
            calls.append(1)
            if len(calls) == 1:
                # apply_format_scopes has already mutated the in-memory rows.
                assert changes[0].row.glicko_rating is not None
                raise elo.IntegrityError("conflict after the rows were mutated")
            return real_flush(self, match, changes)

        with mock.patch.object(LiveBook, "flush", flaky_flush):
            update_player_elo(retried)

        assert len(calls) == 2
        assert (_state(_row(b1)), _state(_row(b2))) == baseline
        assert _row(b1).matches == 1
        assert EloHistory.objects.filter(match=retried).count() == 2


@pytest.mark.django_db
class TestOutOfOrderPreview:
    def test_preview_equals_persisted_for_a_match_older_than_last_rated_on(self):
        match, p1, p2 = _pending(when=_at(2026, 3, 1))
        for p in (p1, p2):
            PlayerRatingFactory(
                player=p,
                matches=5,
                glicko_rating=1550.0,
                rd=80.0,
                volatility=0.06,
                last_rated_on=date(2026, 3, 10),
            )
        projection = glicko_adapter.projected_glicko_changes(
            Match.all_objects.get(pk=match.pk), SINGLES
        )

        update_player_elo(match)

        for update in projection.updates:
            row = _row(Player.objects.get(pk=update.player_id))
            assert (row.glicko_rating, row.rd, row.volatility) == tuple(update.new)
            assert row.last_rated_on == date(2026, 3, 10)
        assert (
            projection.updates[0].inflated.rd == projection.updates[0].old.rd
        )  # clamped


@pytest.mark.django_db
class TestBackfillWithRecalculateElo:
    """After migration 0044 existing rows have NULL shadow fields; the deploy
    runs `recalculate_elo --format singles`."""

    def _history(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        for day in (3, 3, 20):
            _finish([p1], [p2], when=_at(2026, 1, day))
        return p1, p2

    def _shadow(self):
        return {
            (r.player_id, r.season_id): _state(r)
            for r in PlayerRating.all_objects.all()
        }

    def test_it_rebuilds_the_shadow_without_touching_legacy_elo_and_is_idempotent(self):
        _p1, _p2 = self._history()
        live = self._shadow()
        PlayerRating.all_objects.update(
            glicko_rating=None, rd=None, volatility=None, last_rated_on=None
        )
        legacy = {p.pk: p.elo_rating for p in Player.objects.all()}
        history = list(EloHistory.objects.order_by("pk").values_list("pk", flat=True))

        call_command("recalculate_elo", format="singles")
        first = self._shadow()
        changes_after_first = RatingChange.objects.count()
        call_command("recalculate_elo", format="singles")

        assert first.keys() == live.keys()
        for key, expected in live.items():
            assert first[key][:3] == pytest.approx(expected[:3])
            assert first[key][3] == expected[3]
        assert self._shadow() == first  # idempotent
        assert RatingChange.objects.count() == changes_after_first
        assert {p.pk: p.elo_rating for p in Player.objects.all()} == legacy
        assert (
            list(EloHistory.objects.order_by("pk").values_list("pk", flat=True))
            == history
        )
