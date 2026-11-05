"""The fitted Glicko-2 parameters and the cold-start seeding (KAN-68).

The seed is a cold-start replay of the full history
(`recalculate_elo --format singles`), so seeding "from the replay" and
"from RD=350" are the same thing.
"""

from datetime import date, timedelta

import pytest
from django.core.management import call_command

from .. import glicko2
from ..glicko_adapter import DEFAULT_PARAMS, days_to_periods
from ..models import PlayerRating
from .conftest import PlayerFactory
from .test_glicko_shadow import _at, _finish, _row


class TestChosenParameters:
    def test_the_values_are_the_fitted_ones(self):
        assert DEFAULT_PARAMS.tau == 0.5
        assert DEFAULT_PARAMS.period_days == 7
        assert DEFAULT_PARAMS.season_rd_floor == 100.0
        assert DEFAULT_PARAMS.rd_max == 350

    def test_the_floor_is_a_regulars_rd_after_a_year_idle(self):
        regular = glicko2.Rating(1500, 70.0, DEFAULT_PARAMS.initial_volatility)
        periods = days_to_periods(date(2025, 1, 1), date(2026, 1, 1), DEFAULT_PARAMS)

        rd = glicko2.inflate(regular, periods, rd_max=DEFAULT_PARAMS.rd_max).rd

        assert rd == pytest.approx(DEFAULT_PARAMS.season_rd_floor, abs=5)
        assert rd < DEFAULT_PARAMS.rd_max


@pytest.mark.django_db
class TestSeedingByColdStartReplay:
    def _history(self):
        regular, rival, casual = (PlayerFactory() for _ in range(3))
        # Three old matches for the casual player.
        for day in (1, 2, 3):
            _finish([casual], [rival], when=_at(2025, 1, day))
        # Many recent matches for the regular, across several weeks.
        start = _at(2026, 3, 1)
        for n in range(20):
            _finish([regular], [rival], when=start + timedelta(days=3 * n))
        return regular, casual

    def _end_rd(self, player, on):
        row = _row(player)
        periods = days_to_periods(row.last_rated_on, on, DEFAULT_PARAMS)
        stored = glicko2.Rating(row.glicko_rating, row.rd, row.volatility)
        return glicko2.inflate(stored, periods, rd_max=DEFAULT_PARAMS.rd_max).rd

    def test_a_recently_active_player_ends_with_a_lower_rd_than_one_with_old_matches(
        self,
    ):
        regular, casual = self._history()
        PlayerRating.all_objects.update(
            glicko_rating=None, rd=None, volatility=None, last_rated_on=None
        )

        call_command("recalculate_elo", "--format", "singles")

        on = date(2026, 5, 1)
        assert self._end_rd(regular, on) < self._end_rd(casual, on)
        assert self._end_rd(casual, on) > 150  # three matches, a year ago

    def test_seeding_twice_changes_nothing(self):
        self._history()
        call_command("recalculate_elo", "--format", "singles")
        first = sorted(
            PlayerRating.all_objects.values_list(
                "pk", "glicko_rating", "rd", "volatility", "last_rated_on"
            )
        )

        call_command("recalculate_elo", "--format", "singles")

        again = sorted(
            PlayerRating.all_objects.values_list(
                "pk", "glicko_rating", "rd", "volatility", "last_rated_on"
            )
        )
        assert len(again) == len(first)
        for a, b in zip(first, again):
            assert a[1:3] == pytest.approx(b[1:3])
            assert a[4] == b[4]
