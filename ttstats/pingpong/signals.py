from django.contrib.auth.models import User
from django.db.models.signals import post_save, pre_save, post_delete
from django.dispatch import receiver
from django_otp_webauthn.models import WebAuthnCredential

from . import notifications
from .achievements import check_achievements_for_player
from .cache_utils import invalidate_match_caches, invalidate_player_caches
from .emails import send_match_confirmation_email, send_passkey_registered_email
from .models import (
    Game, Match, MatchConfirmation, NotificationPreference, Player,
    ScheduledMatch, UserProfile,
)
from .elo import update_player_elo
from .services import link_championship_entries


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        userprofile = UserProfile.objects.create(user=instance)
        userprofile.create_verification_token()
        userprofile.save()
        # Notification defaults. NotificationPreference.for_user() also
        # creates on demand, so a user who predates this feature is fine --
        # this just means the row exists before anything asks for it.
        NotificationPreference.objects.get_or_create(user=instance)
    else:
        # Ensure profile exists even for existing users
        # (in case they were created before signal was added)
        if not hasattr(instance, 'profile'):
            userprofile = UserProfile.objects.create(user=instance)
            userprofile.create_verification_token()
            userprofile.save()


@receiver(post_save, sender=Match)
def link_entries_on_match_save(sender, instance, **kwargs):
    """Attach championship entries to whichever sides match their members."""
    link_championship_entries(instance)


@receiver(post_save, sender=ScheduledMatch)
def link_entries_on_scheduled_match_save(sender, instance, **kwargs):
    link_championship_entries(instance)


@receiver(pre_save, sender=Match)
def track_match_winner_change(sender, instance, **kwargs):
    """Remember if winner is being set for the first time"""
    if not instance.pk:
        instance._winner_just_set = False
        return

    try:
        old_match = sender.objects.get(pk=instance.pk)
        instance._winner_just_set = (old_match.winner_side is None)
    except sender.DoesNotExist:
        instance._winner_just_set = False


@receiver(post_save, sender=Match)
def handle_match_completion(sender, instance, created, **kwargs):
    """Handle match completion tasks"""
    # Only process if winner was just set
    if not getattr(instance, "_winner_just_set", False) or not instance.winner_side:
        return

    # Live matches are mid-flight — the scoreboard endpoint flips is_live=False
    # before saving the winning Game, so when this signal fires for a "real"
    # finish, is_live will already be False here.
    if instance.is_live:
        return

    # 1. Auto-confirm if needed
    if instance.should_auto_confirm():
        all_players = instance.all_players
        MatchConfirmation.objects.bulk_create(
            [MatchConfirmation(match=instance, player=player) for player in all_players],
            ignore_conflicts=True
        )
        # Reload instance to get updated confirmation fields
        instance.refresh_from_db()
    else:
        # 2. Tell everyone who still has to confirm. Push first, then email
        #    only the people push did not reach -- a player with the PWA
        #    installed should get one buzz, not a buzz and a mail, while a
        #    player who never installed it must not silently stop hearing
        #    from us. `notify_*` returns the ids it actually delivered to,
        #    which is what makes that a single rule.
        already_confirmed = {c.player_id for c in instance.confirmations.all()}
        pending = [
            player for player in instance.all_players
            if (
                player.user
                and player.user.email
                and hasattr(player.user, 'profile')
                and player.user.profile.email_verified
                and player.id not in already_confirmed
            )
        ]

        pushed = notifications.notify_match_confirmation_needed(instance, pending)
        for player in pending:
            if player.pk not in pushed:
                send_match_confirmation_email(instance, player)

    # 3. Refresh the denormalized fields (winner, score caches, is_confirmed).
    #    recompute() persists with a queryset update, so no signal re-entry.
    instance.recompute()

    # 4. Update Elo ratings (only runs if confirmed)
    update_player_elo(instance)

    # 5. Check achievements (after Elo is updated)
    if instance.is_confirmed and instance.winner_side:
        all_players = list(instance.all_players)
        for p in all_players:
            check_achievements_for_player(p, instance)

    # 6. Result + leaderboard pushes. Self-guarding: only the first caller
    #    for a given match gets through (see notify_match_confirmed).
    notifications.notify_match_confirmed(instance)

    # Invalidate caches
    invalidate_match_caches(instance)


@receiver(post_save, sender=Match)
def update_elo_on_confirmation(sender, instance, created, **kwargs):
    """Update Elo ratings when match is confirmed"""
    # Skip if this is being triggered by handle_match_completion
    # (to avoid double-processing when winner is just set)
    if getattr(instance, "_winner_just_set", False):
        return

    # IMPORTANT: Refresh instance to ensure we have the latest confirmation
    # field values from the database. This fixes the issue where manual
    # confirmations via the match_confirm view don't trigger Elo updates.
    instance.refresh_from_db()

    if instance.is_live:
        return

    # Try to update Elo (has guards inside, safe to call anytime)
    update_player_elo(instance)

    # Same here: notify_match_confirmed is a no-op unless this is the first
    # time the match reached "confirmed, Elo applied".
    instance.refresh_from_db()
    notifications.notify_match_confirmed(instance)


@receiver(post_save, sender=MatchConfirmation)
def update_elo_on_match_confirmation(sender, instance, created, **kwargs):
    """Update Elo ratings when a player confirms a match"""
    if created:
        match = instance.match

        # Refresh the denormalized fields (no signal re-entry -- see recompute)
        match.recompute()

        # Try to update Elo for the match (has guards inside, safe to call anytime)
        update_player_elo(match)

        # Check achievements (after Elo is updated)
        if match.is_confirmed and match.winner_side:
            all_players = list(match.all_players)
            for p in all_players:
                check_achievements_for_player(p, match)

        # This is the path that fires when the last player confirms, so it is
        # usually the one that actually sends the result push.
        notifications.notify_match_confirmed(match)

        # Invalidate caches
        invalidate_match_caches(match)

        # Check if this completes a championship
        if hasattr(match, 'championship') and match.championship:
            match.championship.check_completion()


@receiver(post_save, sender=Game)
def invalidate_caches_on_game_save(sender, instance, **kwargs):
    """Invalidate caches when a game is saved (scores change)."""
    invalidate_match_caches(instance.match)


@receiver(post_save, sender=Player)
def invalidate_on_player_save(sender, instance, created, **kwargs):
    """Invalidate caches when player is created or updated."""
    invalidate_player_caches(instance)


@receiver(post_delete, sender=Player)
def invalidate_on_player_delete(sender, instance, **kwargs):
    """Invalidate caches when player is deleted."""
    invalidate_player_caches(instance)


@receiver(post_save, sender=WebAuthnCredential)
def notify_passkey_registered(sender, instance, created, **kwargs):
    """Send email when new passkey is registered"""
    if created:
        send_passkey_registered_email(instance.user, instance.name)
