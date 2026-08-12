"""Pure unit tests for the round-robin scheduler -- no database."""
from collections import Counter

import pytest

from pingpong.championship_scheduling import (
    round_robin_double_rounds,
    round_robin_rounds,
)


def _all_pairs(rounds):
    return [pair for _, pairings in rounds for pair in pairings]


class TestRoundRobinRounds:
    @pytest.mark.parametrize("n", [0, 1])
    def test_fewer_than_two_competitors_yields_nothing(self, n):
        assert round_robin_rounds(range(n)) == []

    @pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 7, 8, 9])
    def test_every_pair_meets_exactly_once(self, n):
        competitors = list(range(n))
        pairs = _all_pairs(round_robin_rounds(competitors))

        unordered = Counter(frozenset(p) for p in pairs)
        expected = {
            frozenset((a, b))
            for i, a in enumerate(competitors)
            for b in competitors[i + 1:]
        }
        assert set(unordered) == expected
        assert set(unordered.values()) == {1}

    @pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 7])
    def test_nobody_plays_themselves(self, n):
        assert all(a != b for a, b in _all_pairs(round_robin_rounds(range(n))))

    @pytest.mark.parametrize("n", [4, 6, 8])
    def test_even_counts_have_no_byes(self, n):
        rounds = round_robin_rounds(range(n))
        assert len(rounds) == n - 1
        for _, pairings in rounds:
            assert len(pairings) == n // 2
            played = [c for pair in pairings for c in pair]
            assert sorted(played) == list(range(n))

    @pytest.mark.parametrize("n", [3, 5, 7])
    def test_odd_counts_sit_exactly_one_competitor_out_each_round(self, n):
        rounds = round_robin_rounds(range(n))
        assert len(rounds) == n
        byes = Counter()
        for _, pairings in rounds:
            assert len(pairings) == (n - 1) // 2
            played = {c for pair in pairings for c in pair}
            missing = set(range(n)) - played
            assert len(missing) == 1
            byes[missing.pop()] += 1
        # Everyone sits out the same number of times.
        assert set(byes.values()) == {1}

    def test_round_numbers_start_at_one_and_are_contiguous(self):
        numbers = [n for n, _ in round_robin_rounds(range(6))]
        assert numbers == list(range(1, 6))

    def test_accepts_any_hashable_competitor(self):
        pairs = _all_pairs(round_robin_rounds(["ada", "bob", "cy"]))
        assert {frozenset(p) for p in pairs} == {
            frozenset({"ada", "bob"}),
            frozenset({"ada", "cy"}),
            frozenset({"bob", "cy"}),
        }


class TestRoundRobinDoubleRounds:
    def test_empty_when_too_few_competitors(self):
        assert round_robin_double_rounds([1]) == []

    @pytest.mark.parametrize("n", [2, 3, 4, 5, 6])
    def test_every_pair_meets_exactly_twice(self, n):
        pairs = _all_pairs(round_robin_double_rounds(range(n)))
        counts = Counter(frozenset(p) for p in pairs)
        assert set(counts.values()) == {2}

    @pytest.mark.parametrize("n", [2, 4, 6])
    def test_second_leg_mirrors_the_first(self, n):
        rounds = round_robin_double_rounds(range(n))
        half = len(rounds) // 2
        for (_, first), (_, second) in zip(rounds[:half], rounds[half:]):
            assert second == [(b, a) for a, b in first]

    def test_round_numbers_continue_across_legs(self):
        numbers = [n for n, _ in round_robin_double_rounds(range(4))]
        assert numbers == [1, 2, 3, 4, 5, 6]

    @pytest.mark.parametrize("n", [2, 3, 4, 5])
    def test_each_competitor_plays_home_and_away_against_everyone(self, n):
        ordered = Counter(_all_pairs(round_robin_double_rounds(range(n))))
        for (a, b), count in ordered.items():
            assert count == 1
            assert ordered[(b, a)] == 1
