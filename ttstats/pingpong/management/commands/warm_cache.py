"""Pre-populate frequently accessed caches after deployment.

**Warms per club.** Cache keys carry a club namespace (see `cache_utils`), so a
single unscoped pass would write keys nobody ever reads and leave every real
club cold -- warming that reports success and achieves nothing.

The club is applied as an explicit filter rather than by faking a request.
The managers' contract is "no user means unfiltered", and a management command
has no user -- so a club in thread-local context alone would leave every query
unscoped and write each club's key with the whole database's numbers. A
populated cache that is wrong is worse than a cold one.
"""
from django.core.management.base import BaseCommand
from django.core.cache import cache

from pingpong import cache_utils, flags
from pingpong.models import Club, Match, Player


class Command(BaseCommand):
    help = 'Warm frequently accessed caches, per club'

    def handle(self, *args, **options):
        self.stdout.write('Warming caches...')

        if not flags.is_enabled('clubs'):
            # Flag off: one unscoped pass, byte-identical to the pre-tenancy
            # command. This is what production runs until the cutover.
            self._warm(None)
        else:
            clubs = list(Club.objects.all())
            if not clubs:
                self.stdout.write(self.style.WARNING('  No clubs to warm.'))
            for club in clubs:
                self.stdout.write(f'  {club.name}:')
                self._warm(club)

        self.stdout.write(self.style.SUCCESS('Cache warming complete!'))

    def _warm(self, club):
        """Warm one club's keys.

        The club is applied as an **explicit filter**, not by pushing it into
        thread-local context. The managers' contract is "no user means
        unfiltered", and a management command has no user -- so setting only
        the club would leave every query unscoped and write each club's key
        with the whole database's numbers. That is worse than not warming at
        all: it is a populated cache that is wrong.
        """
        prefix = cache_utils.club_prefix(club)

        players = Player.objects.all()
        matches = Match.objects.all()
        if club is not None:
            players = players.filter(club=club)
            matches = matches.filter(club=club)

        total_players = players.count()
        cache.set(f'{prefix}dashboard_total_players', total_players, 900)
        self.stdout.write(f'    Total players: {total_players}')

        total_matches = matches.filter(is_confirmed=True).count()
        cache.set(f'{prefix}dashboard_total_matches', total_matches, 600)
        self.stdout.write(f'    Total confirmed matches: {total_matches}')

        recent_matches = list(matches.order_by("-date_played")[:5])
        cache.set(f'{prefix}dashboard_recent_matches', recent_matches, 300)
        self.stdout.write(f'    Recent matches: {len(recent_matches)}')
