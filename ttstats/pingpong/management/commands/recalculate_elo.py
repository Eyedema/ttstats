"""
Management command to recalculate Elo ratings for all existing matches.
Run after initial Elo system deployment or to fix rating inconsistencies.

Usage: python manage.py recalculate_elo
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from pingpong.models import Player, Match, EloHistory
from pingpong.elo import update_player_elo


class Command(BaseCommand):
    help = 'Recalculate Elo ratings for all confirmed matches'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without making changes',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be saved'))

        # Get all confirmed matches, ordered chronologically
        matches = Match.objects.filter(
            winner__isnull=False,
            player1_confirmed=True,
            player2_confirmed=True,
        ).order_by('date_played', 'created_at')

        # Also filter out 2v2 matches if is_double field exists
        if hasattr(Match, 'is_double'):
            matches = matches.filter(is_double=False)

        match_count = matches.count()

        self.stdout.write(f'Found {match_count} confirmed matches to process')

        if not dry_run:
            with transaction.atomic():
                # Reset all player Elo ratings
                self.stdout.write('Resetting all player Elo ratings to 1500...')
                Player.objects.all().update(
                    elo_rating=1500,
                    elo_peak=1500,
                    matches_for_elo=0,
                )

                # Clear existing Elo history
                deleted_count = EloHistory.objects.all().count()
                EloHistory.objects.all().delete()
                self.stdout.write(f'Deleted {deleted_count} existing Elo history records')

                # Replay matches in chronological order
                self.stdout.write('Recalculating Elo ratings...')
                for i, match in enumerate(matches, 1):
                    update_player_elo(match)

                    if i % 50 == 0:
                        self.stdout.write(f'  Processed {i}/{match_count} matches...')

                self.stdout.write(self.style.SUCCESS(f'✓ Successfully recalculated Elo for {match_count} matches'))
        else:
            # Dry run: Show what would happen
            self.stdout.write('Would reset all player Elo ratings to 1500')
            self.stdout.write(f'Would delete {EloHistory.objects.count()} existing history records')
            self.stdout.write(f'Would recalculate Elo for {match_count} matches')

            # Show first 5 matches
            self.stdout.write('\nFirst 5 matches to process:')
            for match in matches[:5]:
                self.stdout.write(f'  - {match}')
