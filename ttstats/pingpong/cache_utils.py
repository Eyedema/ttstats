"""Cache invalidation utilities for TTStats.

**Every key carries the club it was built for.** A correctly-scoped query served
from a cache entry another club warmed is still a leak, and it is the one kind
of leak a test suite running against a cold cache will never see. So the club id
goes in the key *prefix* -- prefix rather than suffix so `cache_control --stats`
can group by club and a future targeted flush is possible.

The generation-counter pattern is kept, because it is the right design: it
*versions* keys rather than deleting every filter variant, which would otherwise
mean enumerating the cross-product of match_type x date_filter x top_x. It is
now per club, so a match in Club A no longer invalidates Club B's leaderboard.

With the `clubs` flag off, `club_prefix()` returns the empty string and every
key is byte-identical to its pre-tenancy shape.
"""
import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

# Used when there IS a viewer and they belong to no club. It must not collide
# with a real club's namespace -- a club id is an integer, so this never can --
# and it must not collide with the flag-off namespace either, or a clubless
# user could be served the unscoped payload.
NO_CLUB = "noclub"


def club_prefix(club=None):
    """The namespace component for `club`, or for the current request's club.

    Returns "" when the flag is off, so keys keep their pre-tenancy shape and
    hit rates are unchanged.
    """
    from pingpong import flags
    from ttstats.middleware import get_current_club, get_current_user

    if not flags.is_enabled("clubs", get_current_user()):
        return ""

    if club is None:
        club = get_current_club()

    return f"club{club.pk}:" if club is not None else f"{NO_CLUB}:"


def leaderboard_generation_key(club=None):
    """The per-club generation counter.

    Deliberately not `leaderboard_generation` with a suffix: the old global key
    must never be readable by a new-format request. A deploy leaves old keys in
    Redis, and an ambiguous overlap is how one club gets served another's
    payload for the remainder of a TTL.
    """
    prefix = club_prefix(club)
    return f"{prefix}leaderboard_generation" if prefix else "leaderboard_generation"


def invalidate_match_caches(match):
    """
    Invalidate all caches related to a match.
    Call from signals when matches are created/updated/confirmed.
    """
    from .models import Side

    side1 = list(match.players_on(Side.ONE))
    side2 = list(match.players_on(Side.TWO))

    # A player's stats key is namespaced by the club whose data it summarises,
    # which for a player is their own club -- not the club of whoever is
    # looking. Otherwise the same player has one cache entry per viewer.
    keys_to_delete = []
    for player in side1 + side2:
        prefix = club_prefix(player.club)
        keys_to_delete.append(f'{prefix}player_stats_{player.pk}')
        keys_to_delete.append(f'{prefix}pending_matches_{player.pk}')
        keys_to_delete.append(f'{prefix}player_rank_{player.pk}')

    # Invalidate head-to-head cache (singles only)
    if len(side1) == 1 and len(side2) == 1:
        p1, p2 = side1[0], side2[0]
        prefix = club_prefix(p1.club)
        keys_to_delete.append(
            f'{prefix}h2h_{min(p1.pk, p2.pk)}_{max(p1.pk, p2.pk)}'
        )

    # Invalidate the club-wide dashboard caches.
    for club in _clubs_touched_by(match, side1 + side2):
        prefix = club_prefix(club)
        keys_to_delete.extend([
            f'{prefix}dashboard_total_players',
            f'{prefix}dashboard_total_matches',
            f'{prefix}dashboard_recent_matches',
            f'{prefix}live_championships_count',
        ])
        invalidate_leaderboard(club=club)

    cache.delete_many(keys_to_delete)
    logger.debug('Invalidated %d cache keys for match %s', len(keys_to_delete), match.pk)


def _clubs_touched_by(match, players):
    """Every club with a stake in this match.

    A cross-club match invalidates **both** clubs. The UI forbids building one,
    but they are representable at the DB level by design (KAN-12), and a
    half-invalidated cross-club match leaves one side reading a stale table.

    Returns [None] when nothing has a club, which yields the flag-off /
    no-club namespace and keeps the pre-tenancy behaviour intact.
    """
    clubs = {p.club for p in players if p.club_id}
    if match.club_id:
        clubs.add(match.club)
    return list(clubs) or [None]


def invalidate_player_caches(player):
    """
    Invalidate all caches related to a player.
    Call when player is created/updated/deleted.
    """
    prefix = club_prefix(player.club)
    keys_to_delete = [
        f'{prefix}player_stats_{player.pk}',
        f'{prefix}pending_matches_{player.pk}',
        f'{prefix}player_rank_{player.pk}',
        f'{prefix}dashboard_total_players',
    ]

    invalidate_leaderboard(club=player.club)

    cache.delete_many(keys_to_delete)
    logger.debug('Invalidated caches for player %s', player.pk)


def invalidate_leaderboard(club=None):
    """Invalidate one club's leaderboard variants by bumping its generation.

    `club=None` with the flag on means the current request's club; with the
    flag off it means the single global counter, exactly as before.
    """
    key = leaderboard_generation_key(club)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=None)


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


def get_cache_stats_by_club():
    """Key counts grouped by club namespace. Requires django-redis.

    This is why the club id is a prefix rather than a suffix: `club7:*` is a
    scannable pattern, and `*:club7` is not.
    """
    try:
        from django_redis import get_redis_connection

        con = get_redis_connection("default")
        counts = {}
        for raw in con.scan_iter(match="*", count=500):
            key = raw.decode(errors="ignore") if isinstance(raw, bytes) else raw
            # django-redis prefixes with "<KEY_PREFIX>:<version>:"
            tail = key.split(":", 2)[-1]
            namespace = tail.split(":", 1)[0] if ":" in tail else "(global)"
            if not (namespace.startswith("club") or namespace == NO_CLUB):
                namespace = "(global)"
            counts[namespace] = counts.get(namespace, 0) + 1
        return counts
    except ImportError:
        return {'error': 'django-redis not available (using LocMemCache)'}
    except Exception as e:
        return {'error': str(e)}
