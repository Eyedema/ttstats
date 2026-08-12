"""Cache invalidation utilities for TTStats."""
import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)


def invalidate_match_caches(match):
    """
    Invalidate all caches related to a match.
    Call from signals when matches are created/updated/confirmed.
    """
    from .models import Side

    keys_to_delete = []

    side1 = list(match.players_on(Side.ONE))
    side2 = list(match.players_on(Side.TWO))

    # Invalidate player-specific caches
    for player in side1 + side2:
        keys_to_delete.append(f'player_stats_{player.pk}')
        keys_to_delete.append(f'pending_matches_{player.pk}')

    # Invalidate head-to-head cache (singles only)
    if len(side1) == 1 and len(side2) == 1:
        p1, p2 = side1[0], side2[0]
        cache_key = f'h2h_{min(p1.pk, p2.pk)}_{max(p1.pk, p2.pk)}'
        keys_to_delete.append(cache_key)

    # Invalidate global caches
    keys_to_delete.extend([
        'dashboard_total_players',
        'dashboard_total_matches',
        'dashboard_recent_matches',
    ])

    # Invalidate leaderboard by bumping generation counter
    invalidate_leaderboard()

    cache.delete_many(keys_to_delete)
    logger.debug('Invalidated %d cache keys for match %s', len(keys_to_delete), match.pk)


def invalidate_player_caches(player):
    """
    Invalidate all caches related to a player.
    Call when player is created/updated/deleted.
    """
    keys_to_delete = [
        f'player_stats_{player.pk}',
        f'pending_matches_{player.pk}',
        'dashboard_total_players',
    ]

    # Invalidate leaderboard
    invalidate_leaderboard()

    cache.delete_many(keys_to_delete)
    logger.debug('Invalidated caches for player %s', player.pk)


def invalidate_leaderboard():
    """Invalidate all leaderboard cache variants by bumping generation."""
    try:
        cache.incr('leaderboard_generation')
    except ValueError:
        cache.set('leaderboard_generation', 1, timeout=None)


def invalidate_all_caches():
    """Clear all TTStats caches. Use for testing or major data migrations."""
    cache.clear()


def get_cache_stats():
    """Get cache statistics for debugging. Requires django-redis backend."""
    try:
        from django_redis import get_redis_connection
        con = get_redis_connection("default")
        info = con.info()
        hits = info.get('keyspace_hits', 0)
        misses = info.get('keyspace_misses', 0)
        total = hits + misses
        return {
            'keys': con.dbsize(),
            'memory': info.get('used_memory_human', 'Unknown'),
            'hits': hits,
            'misses': misses,
            'hit_rate': (hits / total * 100) if total > 0 else 0,
        }
    except ImportError:
        return {'error': 'django-redis not available (using LocMemCache)'}
    except Exception as e:
        return {'error': str(e)}
