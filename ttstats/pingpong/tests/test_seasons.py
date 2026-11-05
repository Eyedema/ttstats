"""seasons.py is pure: no Django, no database. These run without django_db."""
import math
from dataclasses import dataclass
from datetime import date

import pytest

from ..seasons import (
    SEASON_CARRY_FACTOR,
    ROLLOVER_GRACE_DAYS,
    SUCCESSOR_LEAD_DAYS,
    carry_over_seed,
    carry_over_seed_float,
    competition_ranks,
    first_season_bounds,
    is_due_for_rollover,
    needs_successor,
    next_season_bounds,
    resolve_season,
    season_name_for,
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


class TestCompetitionRanks:
    """Ties share a rank and the next rank skips: 1, 1, 3 (KAN-51)."""

    def test_no_ties(self):
        assert competition_ranks([1700, 1600, 1500]) == [1, 2, 3]

    def test_a_tie_for_first_shares_rank_one_and_skips_two(self):
        assert competition_ranks([1700, 1700, 1600]) == [1, 1, 3]

    def test_a_tie_in_the_middle(self):
        assert competition_ranks([1800, 1600, 1600, 1600, 1500]) == [1, 2, 2, 2, 5]

    def test_empty(self):
        assert competition_ranks([]) == []

    def test_input_must_be_best_first(self):
        with pytest.raises(ValueError):
            competition_ranks([1500, 1600])


class TestIsDueForRollover:
    def test_grace_is_three_days(self):
        assert ROLLOVER_GRACE_DAYS == 3

    @pytest.mark.parametrize("today, due", [
        (date(2026, 3, 31), False),  # last day of the season
        (date(2026, 4, 3), False),   # ends_on + 3: still inside the grace
        (date(2026, 4, 4), True),    # ends_on + 3 < today
    ])
    def test_due_only_after_the_grace_days(self, today, due):
        assert is_due_for_rollover(date(2026, 3, 31), today) is due


class TestNeedsSuccessor:
    def test_the_lead_is_fourteen_days(self):
        assert SUCCESSOR_LEAD_DAYS == 14

    @pytest.mark.parametrize("today,needed", [
        (date(2026, 3, 16), False),  # 15 days before ends_on
        (date(2026, 3, 17), True),   # exactly ends_on - 14
        (date(2026, 3, 31), True),
        (date(2026, 4, 2), True),    # already past: a backlog still needs one
    ])
    def test_needed_from_the_lead_day_on(self, today, needed):
        assert needs_successor(date(2026, 3, 31), today) is needed


class TestSeasonNameFor:
    def test_whole_months_in_one_year(self):
        assert season_name_for(date(2026, 4, 1), date(2026, 6, 30)) == "Apr–Jun 2026"

    def test_a_single_month(self):
        assert season_name_for(date(2026, 4, 1), date(2026, 4, 30)) == "Apr 2026"

    def test_across_a_year_end(self):
        assert season_name_for(date(2026, 11, 1), date(2027, 1, 31)) == "Nov 2026–Jan 2027"

    def test_odd_dates_name_the_days(self):
        assert season_name_for(date(2026, 5, 3), date(2026, 5, 9)) == "3 May–9 May 2026"


class TestCarryOverSeedFloat:
    """The unrounded seed the Glicko-2 shadow uses (KAN-67); no database."""

    @pytest.mark.parametrize("all_time, seed", [
        (1700, 1600.0), (1301, 1400.5), (1500, 1500.0), (1800, 1650.0),
    ])
    def test_it_does_not_round(self, all_time, seed):
        assert carry_over_seed_float(all_time) == seed

    def test_unrated_starts_at_the_mean(self):
        assert carry_over_seed_float(None) == 1500.0

    @pytest.mark.parametrize("all_time", [1301, 1500, 1623, 1701.7, None])
    def test_the_integer_seed_is_this_one_rounded_half_up(self, all_time):
        assert carry_over_seed(all_time) == math.floor(carry_over_seed_float(all_time) + 0.5)
