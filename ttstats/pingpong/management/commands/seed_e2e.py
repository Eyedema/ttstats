"""Seed a deterministic fixture for the Playwright end-to-end suite.

The e2e suite drives the real app in a real browser engine, so it needs a
known user and a known live match to point at. Refuses to run unless DEBUG is
on, because it creates an account with a published password.
"""
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from pingpong import live_scoring as ls, services
from pingpong.models import Club, ClubInvite, ClubMembership, ClubRole, Match, Player

E2E_USERNAME = "e2e"
E2E_PASSWORD = "e2e-local-only"
E2E_OPPONENT = "e2e-opponent"


class Command(BaseCommand):
    help = "Create the fixture the Playwright suite expects (DEBUG only)."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_e2e refuses to run with DEBUG=False.")

        # Idempotent: the suite re-seeds before every run, and a live match
        # left over from a previous run would make the scoreboard specs
        # start from a non-zero score.
        Match.all_objects.filter(is_live=True, scorekeeper__name="E2E Player").delete()

        user, _ = User.objects.get_or_create(
            username=E2E_USERNAME, defaults={"email": "e2e@example.test"}
        )
        user.set_password(E2E_PASSWORD)
        user.save()
        user.profile.email_verified = True
        user.profile.save()

        player, _ = Player.objects.get_or_create(name="E2E Player")
        player.user = user
        player.save()

        opponent_user, _ = User.objects.get_or_create(
            username=E2E_OPPONENT, defaults={"email": "e2e-opp@example.test"}
        )
        opponent_user.profile.email_verified = True
        opponent_user.profile.save()
        opponent, _ = Player.objects.get_or_create(name="E2E Opponent")
        opponent.user = opponent_user
        opponent.save()

        match = Match.all_objects.create(
            best_of=5,
            is_live=True,
            scorekeeper=player,
            live_state=ls.initial_state(5),
        )
        services.set_match_sides(match, [player], [opponent])

        # A club, so the invite specs have something to point at. Idempotent
        # like everything else here: the suite re-seeds before every run.
        club, _ = Club.objects.get_or_create(
            slug="e2e-club", defaults={"name": "E2E Club"}
        )
        ClubMembership.objects.get_or_create(
            club=club, user=user, defaults={"role": ClubRole.OWNER}
        )
        Player.objects.filter(pk__in=[player.pk, opponent.pk]).update(club=club)

        # One live invite and one revoked one, so the suite can render both the
        # working and the dead page without mutating state mid-run.
        ClubInvite.objects.filter(club=club).delete()
        live_invite = ClubInvite.issue(club, user, max_uses=50, note="e2e")
        dead_invite = ClubInvite.issue(club, user, note="e2e revoked")
        dead_invite.revoke()

        # The suite reads these off stdout rather than hard-coding a pk.
        self.stdout.write(f"E2E_USERNAME={E2E_USERNAME}")
        self.stdout.write(f"E2E_PASSWORD={E2E_PASSWORD}")
        self.stdout.write(f"E2E_MATCH_PK={match.pk}")
        self.stdout.write(f"E2E_INVITE_TOKEN={live_invite.token}")
        self.stdout.write(f"E2E_REVOKED_INVITE_TOKEN={dead_invite.token}")
