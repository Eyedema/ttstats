from django.contrib.auth.models import User

from .emails import send_match_confirmation_email
from .models import Match
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import UserProfile


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
        try:
            old_match = Match.objects.get(pk=instance.pk)
            # Compare: was None, now has value?
            instance._winner_just_set = (
                old_match.winner is None and instance.winner is not None
            )
        except Match.DoesNotExist:
            instance._winner_just_set = False
    else:  # New match
        instance._winner_just_set = False


@receiver(post_save, sender=Match)
def handle_match_completion(sender, instance, created, **kwargs):
    """Handle match completion tasks"""
    # Only process if winner was just set
    if not getattr(instance, "_winner_just_set", False):
        return

    if not instance.winner:
        return

    # 1. Auto-confirm if needed
    if instance.should_auto_confirm():
        Match.objects.filter(pk=instance.pk).update(
            player1_confirmed=True, player2_confirmed=True
        )

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
