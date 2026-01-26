from django.contrib.auth.models import User
from django.db.models import Count

from .emails import send_match_confirmation_email
from .models import Match, MatchConfirmation
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
