from django.db import models
from django.db.models import Q


class MatchManager(models.Manager):
    """    
    - Staff users: See all matches
    - Regular users: See only matches they participated in
    - Anonymous users: See no matches
    """
    
    def get_queryset(self):
        """Automatically filter matches based on current user"""
        from ttstats.middleware import get_current_user
        
        qs = super().get_queryset()
        user = get_current_user()
        
        # No user in context (e.g., management commands)
        if not user:
            return qs
        
        # Anonymous users see nothing
        if not user.is_authenticated:
            return qs.none()
        
        # Staff users see everything
        if user.is_staff or user.is_superuser:
            return qs
        
        # Regular users see only their matches
        try:
            user_player = user.player
            return qs.filter(
                Q(player1=user_player) | Q(player2=user_player)
            )
        except AttributeError:
            # User has no linked player
            return qs.none()


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

    def get_queryset(self):
        """Filter games based on match visibility"""
        from ttstats.middleware import get_current_user

        qs = super().get_queryset()
        user = get_current_user()

        if not user:
            return qs

        if not user.is_authenticated:
            return qs.none()

        if user.is_staff or user.is_superuser:
            return qs

        # Filter to games from matches user can see
        try:
            user_player = user.player
            return qs.filter(
                Q(match__player1=user_player) | Q(match__player2=user_player)
            )
        except AttributeError:
            return qs.none()


class ScheduledMatchManager(models.Manager):
    """
    Manager for scheduled matches.
    - Staff users: See all scheduled matches
    - Regular users: See only scheduled matches they're participating in
    - Anonymous users: See no scheduled matches
    """

    def get_queryset(self):
        """Automatically filter scheduled matches based on current user"""
        from ttstats.middleware import get_current_user

        qs = super().get_queryset()
        user = get_current_user()

        # No user in context (e.g., management commands)
        if not user:
            return qs

        # Anonymous users see nothing
        if not user.is_authenticated:
            return qs.none()

        # Staff users see everything
        if user.is_staff or user.is_superuser:
            return qs

        # Regular users see only their scheduled matches
        try:
            user_player = user.player
            return qs.filter(Q(player1=user_player) | Q(player2=user_player))
        except AttributeError:
            # User has no linked player
            return qs.none()
