"""Characterization tests for pingpong.cache_utils.

The module had no test file. These pin the current key-naming and
invalidation behaviour before the Team traversal inside
``invalidate_match_caches`` is rewritten to use match participants.
"""
import pytest
from django.core.cache import cache

from pingpong.cache_utils import (
    invalidate_leaderboard,
    invalidate_match_caches,
    invalidate_player_caches,
)
from .conftest import MatchFactory, PlayerFactory


def _seed(*keys):
    for key in keys:
        cache.set(key, "present", timeout=None)


@pytest.mark.django_db
class TestInvalidateMatchCaches:
    def test_clears_player_keys_for_every_participant(self):
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        m = MatchFactory(player1=p1, player2=p2)

        keys = [
            f"player_stats_{p1.pk}",
            f"pending_matches_{p1.pk}",
            f"player_stats_{p2.pk}",
            f"pending_matches_{p2.pk}",
        ]
        _seed(*keys)

        invalidate_match_caches(m)

        for key in keys:
            assert cache.get(key) is None

    def test_clears_player_keys_for_all_four_in_doubles(self):
        players = [PlayerFactory(with_user=True) for _ in range(4)]
        m = MatchFactory(
            team1_players=players[:2], team2_players=players[2:], is_double=True
        )
        keys = [f"player_stats_{p.pk}" for p in players]
        _seed(*keys)

        invalidate_match_caches(m)

        for key in keys:
            assert cache.get(key) is None

    def test_clears_head_to_head_key_for_singles(self):
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        m = MatchFactory(player1=p1, player2=p2)

        key = f"h2h_{min(p1.pk, p2.pk)}_{max(p1.pk, p2.pk)}"
        _seed(key)

        invalidate_match_caches(m)

        assert cache.get(key) is None

    def test_does_not_clear_head_to_head_key_for_doubles(self):
        players = [PlayerFactory(with_user=True) for _ in range(4)]
        m = MatchFactory(
            team1_players=players[:2], team2_players=players[2:], is_double=True
        )
        pks = sorted(p.pk for p in players)
        key = f"h2h_{pks[0]}_{pks[-1]}"
        _seed(key)

        invalidate_match_caches(m)

        assert cache.get(key) == "present"

    def test_clears_dashboard_keys(self):
        m = MatchFactory()
        keys = [
            "dashboard_total_players",
            "dashboard_total_matches",
            "dashboard_recent_matches",
        ]
        _seed(*keys)

        invalidate_match_caches(m)

        for key in keys:
            assert cache.get(key) is None

    def test_bumps_leaderboard_generation(self):
        m = MatchFactory()
        cache.set("leaderboard_generation", 4, timeout=None)

        invalidate_match_caches(m)

        assert cache.get("leaderboard_generation") == 5


@pytest.mark.django_db
class TestInvalidatePlayerCaches:
    def test_clears_player_and_dashboard_keys(self):
        p = PlayerFactory()
        keys = [
            f"player_stats_{p.pk}",
            f"pending_matches_{p.pk}",
            "dashboard_total_players",
        ]
        _seed(*keys)

        invalidate_player_caches(p)

        for key in keys:
            assert cache.get(key) is None

    def test_leaves_other_players_untouched(self):
        p = PlayerFactory()
        other = PlayerFactory()
        _seed(f"player_stats_{other.pk}")

        invalidate_player_caches(p)

        assert cache.get(f"player_stats_{other.pk}") == "present"

    def test_bumps_leaderboard_generation(self):
        p = PlayerFactory()
        cache.set("leaderboard_generation", 1, timeout=None)

        invalidate_player_caches(p)

        assert cache.get("leaderboard_generation") == 2


class TestInvalidateLeaderboard:
    def test_initialises_generation_when_missing(self):
        cache.delete("leaderboard_generation")

        invalidate_leaderboard()

        assert cache.get("leaderboard_generation") == 1

    def test_increments_existing_generation(self):
        cache.set("leaderboard_generation", 7, timeout=None)

        invalidate_leaderboard()

        assert cache.get("leaderboard_generation") == 8
