from django.contrib.auth.models import User
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django_otp_webauthn.models import WebAuthnCredential

from .emails import send_match_confirmation_email, send_passkey_registered_email
from .models import Match, UserProfile
from .elo import update_player_elo


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        userprofile = UserProfile.objects.create(user=instance)
        userprofile.create_verification_token()
        userprofile.save()


@receiver(pre_save, sender=Match)
def track_match_winner_change(sender, instance, **kwargs):
    """Remember if winner is being set for the first time"""
    if instance.pk:  # Match already exists in DB
        old_match = Match.objects.get(pk=instance.pk)
        instance._winner_just_set = (
            old_match.winner is None and instance.winner is not None
        )
    else:  # New match
        instance._winner_just_set = False


@receiver(post_save, sender=Match)
def handle_match_completion(sender, instance, created, **kwargs):
    """Handle match completion tasks"""
    # Only process if winner was just set
    if not getattr(instance, "_winner_just_set", False):
        return

    # 1. Auto-confirm if needed
    if instance.should_auto_confirm():
        Match.objects.filter(pk=instance.pk).update(
            player1_confirmed=True, player2_confirmed=True
        )
        # Reload instance to get updated confirmation fields
        instance.refresh_from_db()

    # 2. Send confirmation emails (only to verified users who need to confirm)
    else:
        if (
            instance.player1.user
            and instance.player1.user.email
            and not instance.player1_confirmed
        ):
            send_match_confirmation_email(instance, instance.player1, instance.player2)

        if (
            instance.player2.user
            and instance.player2.user.email
            and not instance.player2_confirmed
        ):
            send_match_confirmation_email(instance, instance.player2, instance.player1)

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
