"""Pure unit tests for pingpong.match_state -- no database, no django_db."""
import pytest

from pingpong.match_state import (
    confirmation_complete,
    games_to_win,
    should_auto_confirm,
    side_confirmed,
    winner_side,
)


class TestGamesToWin:
    @pytest.mark.parametrize(
        "best_of,expected", [(1, 1), (3, 2), (5, 3), (7, 4), (9, 5), (11, 6)]
    )
    def test_majority_of_best_of(self, best_of, expected):
        assert games_to_win(best_of) == expected


class TestWinnerSide:
    @pytest.mark.parametrize("best_of,wins", [(3, 2), (5, 3), (7, 4)])
    def test_side1_wins_on_reaching_threshold(self, best_of, wins):
        assert winner_side(wins, 0, best_of) == 1

    @pytest.mark.parametrize("best_of,wins", [(3, 2), (5, 3), (7, 4)])
    def test_side2_wins_on_reaching_threshold(self, best_of, wins):
        assert winner_side(0, wins, best_of) == 2

    def test_undecided_below_threshold(self):
        assert winner_side(2, 2, 7) is None

    def test_undecided_with_no_games_played(self):
        assert winner_side(0, 0, 5) is None

    def test_side1_takes_precedence_when_both_somehow_qualify(self):
        """Data corruption shouldn't crash; side 1 wins the tie-break."""
        assert winner_side(3, 3, 5) == 1

    def test_overshooting_the_threshold_still_wins(self):
        assert winner_side(4, 1, 5) == 1


class TestSideConfirmed:
    def test_all_verified_players_confirmed(self):
        assert side_confirmed({1, 2}, {1, 2, 9}) is True

    def test_missing_one_confirmation(self):
        assert side_confirmed({1, 2}, {1}) is False

    def test_side_with_no_verified_players_is_vacuously_confirmed(self):
        assert side_confirmed(set(), set()) is True
        assert side_confirmed([], [7]) is True

    def test_accepts_any_iterable(self):
        assert side_confirmed([1, 1, 2], (2, 1)) is True


class TestConfirmationComplete:
    def test_both_sides_confirmed(self):
        assert confirmation_complete({1}, {2}, {1, 2}) is True

    def test_one_side_outstanding(self):
        assert confirmation_complete({1}, {2}, {1}) is False

    def test_all_players_unverified_counts_as_complete(self):
        assert confirmation_complete([], [], []) is True

    def test_one_unverified_side_still_needs_the_other(self):
        assert confirmation_complete([], {2}, []) is False
        assert confirmation_complete([], {2}, {2}) is True


class TestShouldAutoConfirm:
    def test_no_winner_means_no(self):
        assert should_auto_confirm(
            has_winner=False,
            already_confirmed=False,
            side1_has_verified=False,
            side2_has_verified=True,
        ) is False

    def test_already_confirmed_means_no(self):
        assert should_auto_confirm(
            has_winner=True,
            already_confirmed=True,
            side1_has_verified=False,
            side2_has_verified=True,
        ) is False

    def test_one_unverified_side_triggers_auto_confirm(self):
        assert should_auto_confirm(
            has_winner=True,
            already_confirmed=False,
            side1_has_verified=False,
            side2_has_verified=True,
        ) is True

    def test_both_sides_verified_requires_real_confirmations(self):
        assert should_auto_confirm(
            has_winner=True,
            already_confirmed=False,
            side1_has_verified=True,
            side2_has_verified=True,
        ) is False

    def test_both_sides_unverified_is_already_confirmed_so_returns_false(self):
        """The documented trap: when nobody is verified the match is already
        confirmed, so this returns False and the caller must still persist
        is_confirmed itself.
        """
        assert should_auto_confirm(
            has_winner=True,
            already_confirmed=True,
            side1_has_verified=False,
            side2_has_verified=False,
        ) is False
