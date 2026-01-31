# pingpong/context_processors.py
from django.db import models
from django.db.models import Q

from .models import Match, Player


def pingpong_context(request):
    if not request.user.is_authenticated:
        return {'pending_matches_count': 0}

    player = getattr(request.user, 'player', None)
    if not player:
        return {'pending_matches_count': 0}

    # Fetch all matches for the player with comprehensive prefetching
    all_matches = Match.objects.filter(
        models.Q(team1__players=player) | models.Q(team2__players=player)
    ).select_related('team1', 'team2').prefetch_related(
        'team1__players__user__profile',
        'team2__players__user__profile',
        'confirmations'
    ).distinct()

    # Filter in Python to find pending matches (not fully confirmed)
    pending_matches = [m for m in all_matches if not m.match_confirmed]
    pending_matches_count = len(pending_matches)

    return {'pending_matches_count': pending_matches_count}

