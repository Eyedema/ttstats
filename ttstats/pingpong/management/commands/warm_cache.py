"""Pre-populate frequently accessed caches after deployment."""
from django.core.management.base import BaseCommand
from django.core.cache import cache

from pingpong.models import Match, Player


class Command(BaseCommand):
    help = 'Warm frequently accessed caches'

    def handle(self, *args, **options):
        self.stdout.write('Warming caches...')

        # Warm total players count
        total_players = Player.objects.count()
        cache.set('dashboard_total_players', total_players, 900)
        self.stdout.write(f'  Total players: {total_players}')

        # Warm total confirmed matches
        total_matches = Match.objects.filter(is_confirmed=True).count()
        cache.set('dashboard_total_matches', total_matches, 600)
        self.stdout.write(f'  Total confirmed matches: {total_matches}')

        # Warm recent matches
        recent_matches = list(Match.objects.all().order_by("-date_played")[:5])
        cache.set('dashboard_recent_matches', recent_matches, 300)
        self.stdout.write(f'  Recent matches: {len(recent_matches)}')

        self.stdout.write(self.style.SUCCESS('Cache warming complete!'))
