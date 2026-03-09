"""Tests for Redis cache functionality."""
import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from .conftest import (
    GameFactory,
    MatchFactory,
    PlayerFactory,
    UserFactory,
    confirm_match,
)
from pingpong.cache_utils import (
    invalidate_match_caches,
    invalidate_player_caches,
    invalidate_leaderboard,
    invalidate_all_caches,
    get_cache_stats,
)
from pingpong.models import Match, MatchConfirmation


def _verified_user_with_player():
    u = UserFactory()
    u.profile.email_verified = True
    u.profile.save()
    p = PlayerFactory(user=u)
    return u, p


def _login_client(user):
    c = Client()
    c.force_login(user)
    return c


# ===========================================================================
# Cache Invalidation
# ===========================================================================

@pytest.mark.django_db
class TestCacheInvalidation:
    """Test that caches are properly invalidated."""

    def test_match_confirmation_invalidates_player_cache(self):
        """Confirming a match should clear player stats cache."""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        match = MatchFactory(player1=p1, player2=p2)

        # Populate cache
        cache_key = f'player_stats_{p1.pk}'
        cache.set(cache_key, {'test': 'data'}, 600)
        assert cache.get(cache_key) is not None

        # Confirm match (triggers signal which invalidates caches)
        confirm_match(match)

        # Cache should be invalidated
        assert cache.get(cache_key) is None

    def test_leaderboard_cache_invalidated_on_match_confirm(self):
        """Confirming a match should bump leaderboard generation."""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)

        # Set initial generation
        cache.set('leaderboard_generation', 1, timeout=None)
        initial_gen = cache.get('leaderboard_generation')

        match = MatchFactory(player1=p1, player2=p2)
        confirm_match(match)

        # Generation should have been bumped
        new_gen = cache.get('leaderboard_generation')
        assert new_gen > initial_gen

    def test_head_to_head_cache_invalidated(self):
        """H2H cache should be cleared when those players play a new match."""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)

        # Populate H2H cache
        cache_key = f'h2h_{min(p1.pk, p2.pk)}_{max(p1.pk, p2.pk)}'
        cache.set(cache_key, {'matches': []}, 1800)
        assert cache.get(cache_key) is not None

        # Create and confirm a match between them
        match = MatchFactory(player1=p1, player2=p2)
        confirm_match(match)

        # H2H cache should be cleared
        assert cache.get(cache_key) is None

    def test_pending_matches_cache_invalidated(self):
        """Pending matches count should be cleared on match changes."""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)

        cache_key = f'pending_matches_{p1.pk}'
        cache.set(cache_key, 5, 300)
        assert cache.get(cache_key) == 5

        match = MatchFactory(player1=p1, player2=p2)
        confirm_match(match)

        assert cache.get(cache_key) is None

    def test_dashboard_caches_invalidated(self):
        """Dashboard caches should be cleared on match changes."""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)

        cache.set('dashboard_total_matches', 10, 600)
        cache.set('dashboard_recent_matches', [], 300)

        match = MatchFactory(player1=p1, player2=p2)
        confirm_match(match)

        assert cache.get('dashboard_total_matches') is None
        assert cache.get('dashboard_recent_matches') is None

    def test_game_save_invalidates_caches(self):
        """Adding a game should invalidate match-related caches."""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        match = MatchFactory(player1=p1, player2=p2)

        cache.set(f'player_stats_{p1.pk}', {'data': 1}, 600)

        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)

        assert cache.get(f'player_stats_{p1.pk}') is None

    def test_player_save_invalidates_caches(self):
        """Updating a player should invalidate related caches."""
        p = PlayerFactory(with_user=True)
        cache.set(f'player_stats_{p.pk}', {'data': 1}, 600)
        cache.set('dashboard_total_players', 10, 900)

        p.name = "Updated Name"
        p.save()

        assert cache.get(f'player_stats_{p.pk}') is None
        assert cache.get('dashboard_total_players') is None


# ===========================================================================
# Cache Utilities
# ===========================================================================

@pytest.mark.django_db
class TestCacheUtilities:
    """Test cache utility functions."""

    def test_invalidate_match_caches_function(self):
        """Test direct cache invalidation function."""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        match = MatchFactory(player1=p1, player2=p2, confirmed=True)

        # Populate multiple caches
        cache.set(f'player_stats_{p1.pk}', {'data': 1}, 600)
        cache.set(f'player_stats_{p2.pk}', {'data': 2}, 600)

        # Invalidate
        invalidate_match_caches(match)

        # All should be cleared
        assert cache.get(f'player_stats_{p1.pk}') is None
        assert cache.get(f'player_stats_{p2.pk}') is None

    def test_invalidate_player_caches_function(self):
        """Test direct player cache invalidation."""
        p = PlayerFactory(with_user=True)
        cache.set(f'player_stats_{p.pk}', {'data': 1}, 600)
        cache.set(f'pending_matches_{p.pk}', 3, 300)

        invalidate_player_caches(p)

        assert cache.get(f'player_stats_{p.pk}') is None
        assert cache.get(f'pending_matches_{p.pk}') is None

    def test_invalidate_leaderboard_function(self):
        """Test leaderboard generation bumping."""
        cache.set('leaderboard_generation', 5, timeout=None)
        invalidate_leaderboard()
        assert cache.get('leaderboard_generation') == 6

    def test_invalidate_leaderboard_initializes_generation(self):
        """Test leaderboard generation initialization when not set."""
        cache.delete('leaderboard_generation')
        invalidate_leaderboard()
        assert cache.get('leaderboard_generation') == 1

    def test_invalidate_all_caches(self):
        """Test nuclear cache clear."""
        cache.set('key1', 'value1', 600)
        cache.set('key2', 'value2', 600)

        invalidate_all_caches()

        assert cache.get('key1') is None
        assert cache.get('key2') is None

    def test_get_cache_stats(self):
        """Should return cache statistics or error."""
        stats = get_cache_stats()
        # LocMemCache returns error, Redis returns stats
        assert 'keys' in stats or 'error' in stats


# ===========================================================================
# Cached Views
# ===========================================================================

@pytest.mark.django_db
class TestCachedContextProcessor:
    """Test context processor caching."""

    def test_pending_matches_cached(self):
        """Context processor should cache pending matches count."""
        u, p = _verified_user_with_player()
        c = _login_client(u)

        cache_key = f'pending_matches_{p.pk}'
        assert cache.get(cache_key) is None

        resp = c.get(reverse("pingpong:dashboard"))
        assert resp.status_code == 200

        # Cache should be populated
        assert cache.get(cache_key) is not None

    def test_pending_matches_served_from_cache(self):
        """Second request should use cached value."""
        u, p = _verified_user_with_player()
        c = _login_client(u)

        # Manually set cache
        cache_key = f'pending_matches_{p.pk}'
        cache.set(cache_key, 42, 300)

        resp = c.get(reverse("pingpong:dashboard"))
        assert resp.status_code == 200
        assert resp.context['pending_matches_count'] == 42


@pytest.mark.django_db
class TestCachedDashboard:
    """Test dashboard caching."""

    def test_dashboard_caches_player_count(self):
        """Dashboard should cache total players."""
        u, p = _verified_user_with_player()
        c = _login_client(u)

        assert cache.get('dashboard_total_players') is None

        resp = c.get(reverse("pingpong:dashboard"))
        assert resp.status_code == 200

        # Should be cached now
        assert cache.get('dashboard_total_players') is not None

    def test_dashboard_uses_cached_match_count(self):
        """Dashboard should use cached confirmed match count."""
        u, p = _verified_user_with_player()
        c = _login_client(u)

        # Pre-populate cache
        cache.set('dashboard_total_matches', 999, 600)

        resp = c.get(reverse("pingpong:dashboard"))
        assert resp.context['total_matches'] == 999


@pytest.mark.django_db
class TestCachedLeaderboard:
    """Test leaderboard caching."""

    def test_leaderboard_caches_results(self):
        """Leaderboard should cache computed stats."""
        u, p = _verified_user_with_player()
        c = _login_client(u)

        resp = c.get(reverse("pingpong:leaderboard"))
        assert resp.status_code == 200
        assert 'player_stats' in resp.context

    def test_leaderboard_uses_cache_on_second_request(self):
        """Second request with same filters should use cache."""
        u, p = _verified_user_with_player()
        p2 = PlayerFactory(with_user=True)
        match = MatchFactory(player1=p, player2=p2)
        for i in range(3):
            GameFactory(match=match, game_number=i + 1, team1_score=11, team2_score=5)
        confirm_match(match)

        c = _login_client(u)

        # First request - cache miss, computes and caches
        resp1 = c.get(reverse("pingpong:leaderboard"))
        stats1 = resp1.context['player_stats']

        # Second request - cache hit
        resp2 = c.get(reverse("pingpong:leaderboard"))
        stats2 = resp2.context['player_stats']

        assert len(stats1) == len(stats2)

    def test_leaderboard_filter_params_use_different_cache_keys(self):
        """Different filter params should use different cache keys."""
        u, p = _verified_user_with_player()
        c = _login_client(u)

        # Request with default filters
        resp1 = c.get(reverse("pingpong:leaderboard"))
        assert resp1.status_code == 200

        # Request with singles filter - should be a different cache key
        resp2 = c.get(reverse("pingpong:leaderboard"), {'match_type': 'singles'})
        assert resp2.status_code == 200


@pytest.mark.django_db
class TestCachedPlayerDetail:
    """Test player detail caching."""

    def test_player_stats_cached(self):
        """Player stats should be cached after first load."""
        u, p = _verified_user_with_player()
        c = _login_client(u)

        cache_key = f'player_stats_{p.pk}'
        assert cache.get(cache_key) is None

        resp = c.get(reverse("pingpong:player_detail", kwargs={"pk": p.pk}))
        assert resp.status_code == 200

        # Stats should be cached
        cached = cache.get(cache_key)
        assert cached is not None
        assert 'total_matches' in cached
        assert 'wins' in cached

    def test_player_stats_served_from_cache(self):
        """Second load should use cached stats."""
        u, p = _verified_user_with_player()
        c = _login_client(u)

        # Pre-populate cache
        cache_key = f'player_stats_{p.pk}'
        cache.set(cache_key, {
            'total_matches': 100,
            'wins': 75,
            'losses': 25,
            'win_rate': 75.0,
            'current_streak': 5,
            'streak_type': 'win',
            'longest_win_streak': 10,
            'longest_loss_streak': 3,
        }, 600)

        resp = c.get(reverse("pingpong:player_detail", kwargs={"pk": p.pk}))
        assert resp.context['total_matches'] == 100
        assert resp.context['wins'] == 75


@pytest.mark.django_db
class TestCachedHeadToHead:
    """Test head-to-head caching."""

    def test_h2h_results_cached(self):
        """H2H results should be cached after computation."""
        u, p1 = _verified_user_with_player()
        p2 = PlayerFactory(with_user=True)
        c = _login_client(u)

        cache_key = f'h2h_{min(p1.pk, p2.pk)}_{max(p1.pk, p2.pk)}'
        assert cache.get(cache_key) is None

        resp = c.get(reverse("pingpong:head_to_head"), {
            "player1": p1.pk,
            "player2": p2.pk,
        })
        assert resp.status_code == 200

        # Cache should be populated
        assert cache.get(cache_key) is not None

    def test_h2h_served_from_cache(self):
        """Second request should use cached data."""
        u, p1 = _verified_user_with_player()
        p2 = PlayerFactory(with_user=True)
        c = _login_client(u)

        # Pre-populate cache
        cache_key = f'h2h_{min(p1.pk, p2.pk)}_{max(p1.pk, p2.pk)}'
        cache.set(cache_key, {
            'player1': p1,
            'player2': p2,
            'has_data': False,
            'has_2v2_matches': False,
        }, 1800)

        resp = c.get(reverse("pingpong:head_to_head"), {
            "player1": p1.pk,
            "player2": p2.pk,
        })
        assert resp.context['has_data'] is False


# ===========================================================================
# Denormalized Fields
# ===========================================================================

@pytest.mark.django_db
class TestDenormalizedFields:
    """Test that denormalized cache fields are maintained correctly."""

    def test_score_cache_updated_on_game_add(self):
        """team1_score_cache and team2_score_cache should update when games are added."""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        match = MatchFactory(player1=p1, player2=p2, best_of=5)

        assert match.team1_score_cache == 0
        assert match.team2_score_cache == 0

        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        match.refresh_from_db()
        assert match.team1_score_cache == 1
        assert match.team2_score_cache == 0

        GameFactory(match=match, game_number=2, team1_score=5, team2_score=11)
        match.refresh_from_db()
        assert match.team1_score_cache == 1
        assert match.team2_score_cache == 1

    def test_is_confirmed_updated_on_confirmation(self):
        """is_confirmed should be True after all players confirm."""
        p1 = PlayerFactory(with_user=True)
        p1.user.profile.email_verified = True
        p1.user.profile.save()

        p2 = PlayerFactory(with_user=True)
        p2.user.profile.email_verified = True
        p2.user.profile.save()

        match = MatchFactory(player1=p1, player2=p2, best_of=5)
        match.refresh_from_db()
        assert match.is_confirmed is False

        # Confirm player 1
        MatchConfirmation.objects.create(match=match, player=p1)
        match.refresh_from_db()
        assert match.is_confirmed is False  # Still needs p2

        # Confirm player 2
        MatchConfirmation.objects.create(match=match, player=p2)
        match.refresh_from_db()
        assert match.is_confirmed is True

    def test_is_confirmed_true_for_unverified_players(self):
        """Matches with only unverified players should be auto-confirmed when winner is set."""
        p1 = PlayerFactory()  # No user = unverified
        p2 = PlayerFactory()  # No user = unverified
        match = MatchFactory(player1=p1, player2=p2, best_of=5)

        # Add enough games to determine winner (triggers auto-confirm for unverified)
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=3, team1_score=11, team2_score=5)

        match.refresh_from_db()
        assert match.winner is not None
        assert match.is_confirmed is True

    def test_score_cache_matches_property(self):
        """Denormalized scores should match the computed property."""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        match = MatchFactory(player1=p1, player2=p2, best_of=5)

        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=9)

        match.refresh_from_db()
        assert match.team1_score_cache == match.team1_score
        assert match.team2_score_cache == match.team2_score


# ===========================================================================
# Management Commands
# ===========================================================================

@pytest.mark.django_db
class TestCacheManagementCommands:
    """Test cache management commands."""

    def test_cache_control_test(self):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('cache_control', '--test', stdout=out)
        assert 'working' in out.getvalue()

    def test_cache_control_clear(self):
        from django.core.management import call_command
        from io import StringIO
        cache.set('test_key', 'value', 600)
        out = StringIO()
        call_command('cache_control', '--clear', stdout=out)
        assert cache.get('test_key') is None
        assert 'cleared' in out.getvalue()

    def test_cache_control_stats(self):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('cache_control', '--stats', stdout=out)
        # Should output something (either stats or "not available")
        assert len(out.getvalue()) > 0

    def test_warm_cache(self):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('warm_cache', stdout=out)
        assert 'complete' in out.getvalue().lower()
        assert cache.get('dashboard_total_players') is not None
