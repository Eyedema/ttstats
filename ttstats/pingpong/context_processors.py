# pingpong/context_processors.py
from django.core.cache import cache
from django.db import models

from .models import Match


def pingpong_context(request):
    if not request.user.is_authenticated:
        return {'pending_matches_count': 0}

    player = getattr(request.user, 'player', None)
    if not player:
        return {'pending_matches_count': 0}

    # Try cache first (5 minute TTL)
    cache_key = f'pending_matches_{player.pk}'
    cached_count = cache.get(cache_key)

    if cached_count is not None:
        return {'pending_matches_count': cached_count}

    # Cache miss - use denormalized is_confirmed field for DB-level filtering
    pending_matches_count = Match.objects.filter(
        models.Q(team1__players=player) | models.Q(team2__players=player),
        is_confirmed=False,
        winner__isnull=False,
    ).distinct().count()

    # Cache for 5 minutes
    cache.set(cache_key, pending_matches_count, 300)

    return {'pending_matches_count': pending_matches_count}
