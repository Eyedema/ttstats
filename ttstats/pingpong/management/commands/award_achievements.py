"""
Management command to retroactively scan match history and award achievements.

Usage:
    python manage.py award_achievements              # Award all
    python manage.py award_achievements --dry-run    # Preview only
    python manage.py award_achievements --player 42  # Single player
"""

from django.core.management.base import BaseCommand
from django.db.models import Q

from pingpong.achievements import check_achievements_for_player
from pingpong.models import Achievement, Match, Player, PlayerAchievement


class Command(BaseCommand):
    help = 'Scan match history and award achievements retroactively'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would be awarded without saving',
        )
        parser.add_argument(
            '--player', type=int, default=None,
            help='Only process a specific player ID',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        player_id = options['player']

        if not Achievement.objects.exists():
            self.stderr.write('No achievements defined. Run migrations first.')
            return

        if player_id:
            players = Player.objects.filter(pk=player_id)
            if not players.exists():
                self.stderr.write(f'Player {player_id} not found.')
                return
        else:
            players = Player.objects.all()

        total_awarded = 0

        for player in players:
            self.stdout.write(f'Processing {player.name}...')

            # Get all confirmed matches in chronological order
            matches = (
                Match.all_objects
                .filter(
                    participants__player=player,
                    is_confirmed=True,
                    winner_side__isnull=False,
                )
                                .prefetch_related(
                    'participants__player',
                    'games', 'elo_history',
                )
                .order_by('date_played')
                .distinct()
            )

            if dry_run:
                # Simulate: collect what would be awarded
                already = set(
                    PlayerAchievement.objects.filter(player=player)
                    .values_list('achievement__slug', flat=True)
                )
                for match in matches:
                    from pingpong.achievements import _CHECKERS
                    for checker in _CHECKERS:
                        slugs = checker(player, match)
                        for slug in slugs:
                            if slug not in already:
                                self.stdout.write(
                                    f'  [DRY RUN] Would award: {slug} '
                                    f'(match #{match.pk})'
                                )
                                already.add(slug)
                                total_awarded += 1
            else:
                for match in matches:
                    new_awards = check_achievements_for_player(player, match)
                    for pa in new_awards:
                        self.stdout.write(
                            f'  Awarded: {pa.achievement.slug} '
                            f'(match #{match.pk})'
                        )
                        total_awarded += 1

        prefix = '[DRY RUN] ' if dry_run else ''
        self.stdout.write(
            self.style.SUCCESS(
                f'{prefix}Done. {total_awarded} achievement(s) awarded.'
            )
        )
