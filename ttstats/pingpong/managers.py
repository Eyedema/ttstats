from django.db import models
from django.db.models import Exists, OuterRef, Q


class MatchManager(models.Manager):
    """
    - Staff users: See all matches
    - Regular users: See only matches they participated in
    - Anonymous users: See no matches

    Live matches (is_live=True) are excluded from every default query so they
    stay invisible to leaderboards, Elo, head-to-head, championships, etc.
    until the scorekeeper finishes them. Use ``Match.live_objects`` (or
    ``Match.all_objects``) to query live matches directly.
    """

    def get_queryset(self):
        """Automatically filter matches based on current user"""
        from ttstats.middleware import get_current_user

        qs = super().get_queryset().filter(is_live=False)
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

        # Regular users see only their matches + championship matches they participate in
        try:
            from .models import Championship
            user_player = user.player
            championship_qs = Championship.all_objects.filter(
                pk=OuterRef('championship_id'),
                participants__players=user_player,
            )
            return qs.filter(
                Q(team1__players=user_player) |
                Q(team2__players=user_player) |
                Exists(championship_qs)
            ).distinct()
        except AttributeError:
            # User has no linked player
            return qs.none()


class LiveMatchManager(models.Manager):
    """Queryset over in-progress live matches only.

    Bypasses the row-level user filter so a scorekeeper can always look up
    their own live match by pk — view layer is responsible for the
    scorekeeper check.
    """

    def get_queryset(self):
        return super().get_queryset().filter(is_live=True)


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
    Games of live matches are hidden until the match completes — same
    rationale as MatchManager.
    """

    def get_queryset(self):
        """Filter games based on match visibility"""
        from ttstats.middleware import get_current_user

        qs = super().get_queryset().filter(match__is_live=False)
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
                Q(match__team1__players=user_player)
                | Q(match__team2__players=user_player)
            )
        except AttributeError:
            return qs.none()


class ChampionshipManager(models.Manager):
    """
    Manager for championships.
    - Staff users: See all championships
    - Regular users: See public championships + ones they participate in or created
    - Anonymous users: See no championships
    """

    def get_queryset(self):
        """Automatically filter championships based on current user"""
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

        # Regular users see public + their championships
        try:
            user_player = user.player
            return qs.filter(
                Q(is_public=True) |
                Q(participants__players=user_player) |
                Q(created_by=user_player)
            ).distinct()
        except AttributeError:
            return qs.filter(is_public=True)


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

        # Regular users see only their scheduled matches + championship matches they participate in
        try:
            from .models import Championship
            user_player = user.player
            championship_qs = Championship.all_objects.filter(
                pk=OuterRef('championship_id'),
                participants__players=user_player,
            )
            return qs.filter(
                Q(team1__players=user_player) |
                Q(team2__players=user_player) |
                Exists(championship_qs)
            ).distinct()
        except AttributeError:
            # User has no linked player
            return qs.none()
