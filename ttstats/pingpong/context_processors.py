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

    # This counts matches in which the user participates and excludes those where both teams have confirmed
    pending_matches_count = Match.objects.filter(
        models.Q(team1__players=player) | models.Q(team2__players=player)
    ).exclude(
        team1_confirmed=True,
        team2_confirmed=True
    ).distinct().count()

    return {'pending_matches_count': pending_matches_count}

