from django.db import models
from django.db.models import Q


class MatchManager(models.Manager):
    """    
    - Staff users: See all matches
    - Regular users: See only matches they participated in
    - Anonymous users: See no matches
    """

    def visible_to(self, user):
        # No user in context (e.g., management commands) or anonymous user (must see nothing)
        if not user or not user.is_authenticated:
            return self.none()

        # Staff users see everything
        if user.is_staff or user.is_superuser:
            return self.all()

        player = getattr(user, 'player', None)
        if not player:
            return self.none()

        # Regular users see only their matches
        return self.filter(
            Q(team1__players=player) | Q(team2__players=player)
        ).distinct()

    def get_queryset(self):
        """Automatically filter matches based on current user"""
        from ttstats.middleware import get_current_user

        return self.visible_to(get_current_user())


class PlayerManager(models.Manager):
    """    
    - Staff users: See all players
    - Regular users: See all players (read-only)
    """
    
    def get_queryset(self):
        """Players are visible to everyone (read-only for non-staff)"""
        return super().get_queryset()
    
    def editable_by(self, user):
        """Get players that user can edit"""
        qs = self.get_queryset()
        
        if not user or not user.is_authenticated:
            return qs.none()
        
        if user.is_staff or user.is_superuser:
            return qs
        
        # Users can only edit their own player profile
        try:
            return qs.filter(user=user)
        except AttributeError:
            return qs.none()


class GameManager(models.Manager):
    """
    Games are visible if their parent match is visible.
    """

    def visible_to(self, user):
        if not user.is_authenticated:
            return self.none()

        if user.is_staff or user.is_superuser:
            return self.all()

        player = getattr(user, 'player', None)
        if not player:
            return self.none()

        return self.filter(
            match__team1__players=player,
            match__team2__players=player
        ).distinct()

    def get_queryset(self):
        """Filter games based on match visibility"""
        from ttstats.middleware import get_current_user

        return self.visible_to(get_current_user())
