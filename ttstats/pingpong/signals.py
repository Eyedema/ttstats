from django.contrib.auth.models import User
from django.db.models import Count

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django_otp_webauthn.models import WebAuthnCredential

from .emails import send_match_confirmation_email, send_passkey_registered_email
from .models import Match, MatchConfirmation, UserProfile
from .elo import update_player_elo


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        userprofile = UserProfile.objects.create(user=instance)
        userprofile.create_verification_token()
        userprofile.save()
    else:
        # Ensure profile exists even for existing users
        # (in case they were created before signal was added)
        if not hasattr(instance, 'profile'):
            userprofile = UserProfile.objects.create(user=instance)
            userprofile.create_verification_token()
            userprofile.save()


@receiver(pre_save, sender=Match)
def track_match_winner_change(sender, instance, **kwargs):
    """Remember if winner is being set for the first time"""
    if not instance.pk:
        instance._winner_just_set = False
        return

    try:
        old_match = sender.objects.get(pk=instance.pk)
        instance._winner_just_set = (old_match.winner_id is None)
    except sender.DoesNotExist:
        instance._winner_just_set = False


@receiver(post_save, sender=Match)
def handle_match_completion(sender, instance, created, **kwargs):
    """Handle match completion tasks"""
    # Only process if winner was just set
    if not getattr(instance, "_winner_just_set", False) or not instance.winner:
        return

    # 1. Auto-confirm if needed
    if instance.should_auto_confirm():
        all_players = (instance.team1.players.all() | instance.team2.players.all())
        MatchConfirmation.objects.bulk_create(
            [MatchConfirmation(match=instance, player=player) for player in all_players],
            ignore_conflicts=True
        )
        # Reload instance to get updated confirmation fields
        instance.refresh_from_db()
        return

    # 2. Send confirmation emails (only to verified users who need to confirm)
    for player in (instance.team1.players.all() | instance.team2.players.all()):
        if (
                player.user
                and player.user.email
                and hasattr(player.user, 'profile')
                and player.user.profile.email_verified
                and player.id not in [c.player_id for c in instance.confirmations.all()]
        ):
            send_match_confirmation_email(instance, player)

    # 3. Update Elo ratings (only runs if confirmed)
    update_player_elo(instance)


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

    # Try to update Elo (has guards inside, safe to call anytime)
    update_player_elo(instance)


@receiver(post_save, sender=WebAuthnCredential)
def notify_passkey_registered(sender, instance, created, **kwargs):
    """Send email when new passkey is registered"""
    if created:
        send_passkey_registered_email(instance.user, instance.name)
