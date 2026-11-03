"""seasons.py is pure: no Django, no database. These run without django_db."""
from dataclasses import dataclass
from datetime import date

import pytest

from ..seasons import (
    SEASON_CARRY_FACTOR,
    carry_over_seed,
    first_season_bounds,
    next_season_bounds,
    resolve_season,
)


@dataclass
class S:
    starts_on: date
    ends_on: date
    name: str = ""


Q1 = S(date(2026, 1, 1), date(2026, 3, 31), "Q1")
Q2 = S(date(2026, 4, 1), date(2026, 6, 30), "Q2")
# A hand-made gap: Q3 starts a week late.
Q3 = S(date(2026, 7, 8), date(2026, 9, 30), "Q3")
SEASONS = [Q2, Q1, Q3]  # unsorted on purpose


class TestResolveSeason:
    @pytest.mark.parametrize("day, expected", [
        (date(2026, 2, 14), Q1),
        (date(2026, 5, 1), Q2),
        (date(2026, 8, 1), Q3),
    ])
    def test_a_day_inside_a_season_returns_it(self, day, expected):
        assert resolve_season(day, SEASONS) is expected

    @pytest.mark.parametrize("day, expected", [
        (date(2026, 1, 1), Q1),   # first day, inclusive
        (date(2026, 3, 31), Q1),  # last day, inclusive
        (date(2026, 4, 1), Q2),   # the next season's first day
    ])
    def test_both_boundaries_are_inclusive(self, day, expected):
        assert resolve_season(day, SEASONS) is expected

    def test_a_day_before_the_first_season_is_none(self):
        assert resolve_season(date(2025, 12, 31), SEASONS) is None

    def test_a_day_in_a_gap_between_seasons_is_none(self):
        """Defined behaviour: the match counts toward all-time ratings only."""
        assert resolve_season(date(2026, 7, 3), SEASONS) is None

    def test_a_day_after_the_last_season_is_none(self):
        assert resolve_season(date(2026, 10, 1), SEASONS) is None

    def test_no_seasons_is_none(self):
        assert resolve_season(date(2026, 1, 1), []) is None

    def test_overlapping_input_raises_rather_than_guessing(self):
        overlapping = [Q1, S(date(2026, 3, 1), date(2026, 4, 30))]
        with pytest.raises(ValueError):
            resolve_season(date(2026, 3, 15), overlapping)

    def test_accepts_a_datetime_and_uses_its_date(self):
        from datetime import datetime, timezone

        instant = datetime(2026, 3, 31, 23, 59, 59, tzinfo=timezone.utc)
        assert resolve_season(instant, SEASONS) is Q1


class TestNextSeasonBounds:
    def test_whole_months_roll_to_the_same_number_of_months(self):
        assert next_season_bounds(date(2026, 1, 1), date(2026, 3, 31)) == (
            date(2026, 4, 1), date(2026, 6, 30),
        )

    def test_whole_months_cross_a_year_end(self):
        assert next_season_bounds(date(2026, 10, 1), date(2026, 12, 31)) == (
            date(2027, 1, 1), date(2027, 3, 31),
        )

    def test_a_partial_month_season_rolls_to_the_same_number_of_days(self):
        starts, ends = next_season_bounds(date(2026, 1, 10), date(2026, 2, 8))
        assert starts == date(2026, 2, 9)
        assert (ends - starts) == (date(2026, 2, 8) - date(2026, 1, 10))

    def test_the_next_season_starts_the_day_after(self):
        starts, _ = next_season_bounds(date(2026, 5, 3), date(2026, 5, 9))
        assert starts == date(2026, 5, 10)


class TestFirstSeasonBounds:
    def test_default_is_three_calendar_months(self):
        assert first_season_bounds(date(2026, 1, 1)) == (date(2026, 1, 1), date(2026, 3, 31))

    def test_mid_month_start_clamps_short_months(self):
        assert first_season_bounds(date(2026, 11, 30)) == (date(2026, 11, 30), date(2027, 2, 27))


class TestCarryOverSeed:
    def test_factor_is_one_half(self):
        assert SEASON_CARRY_FACTOR == 0.5

    @pytest.mark.parametrize("all_time, seed", [
        (1500, 1500),
        (1700, 1600),
        (1300, 1400),
        (1601, 1551),  # 1550.5 rounds half up
        (1399, 1450),  # 1449.5 rounds half up
    ])
    def test_soft_reset_toward_the_mean(self, all_time, seed):
        assert carry_over_seed(all_time) == seed

    def test_an_unrated_player_starts_at_the_mean(self):
        assert carry_over_seed(None) == 1500
