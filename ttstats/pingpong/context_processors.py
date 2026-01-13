# pingpong/context_processors.py
from django.db import models
from .models import Match, Player


def pingpong_context(request):
    pending_matches_count = 0
    if request.user.is_authenticated:
        # Trova il Player collegato all'utente
        try:
            player = Player.objects.get(user=request.user)
            pending_matches_count = Match.objects.filter(
                (models.Q(player1=player) & models.Q(player1_confirmed=False)) | (models.Q(player2=player) & models.Q(player2_confirmed=False))
            ).count()
        except Player.DoesNotExist:
            pending_matches_count = 0

    return {'pending_matches_count': pending_matches_count}

