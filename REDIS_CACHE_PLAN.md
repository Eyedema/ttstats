# Redis Cache Implementation Plan - TTStats

## Executive Summary

**YES, Redis caching will significantly benefit this project.**

### Critical Findings:
1. **Context processor runs on EVERY page load** - fetches all matches for logged-in users (10-50ms per request)
2. **Leaderboard view is extremely expensive** - O(n) complexity with Python filtering, can take 500-2000ms
3. **Model properties trigger hidden queries** - `team1_score`, `team2_score`, `match_confirmed` run COUNT queries each access
4. **No cache framework configured** - Only Python-level caching in MatchListView

### Expected Performance Gains:
- **Context Processor**: 10-50x faster (every page benefits)
- **Leaderboard**: 50-200x faster (500-2000ms → 10ms)
- **Dashboard**: 10-30x faster (100-300ms → 10ms)
- **HeadToHead**: 10-25x faster (200-500ms → 20ms)

---

## Phase 1: Infrastructure Setup (Priority: CRITICAL)

### 1.1 Add Dependencies

**File:** `requirements.txt`

```diff
+ redis==5.0.1
+ django-redis==5.4.0
```

### 1.2 Configure Redis

**File:** `ttstats/settings/base.py`

```python
# Redis Cache Configuration
CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/1'),
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            'PARSER_CLASS': 'redis.connection.HiredisParser',
            'SOCKET_CONNECT_TIMEOUT': 5,
            'SOCKET_TIMEOUT': 5,
            'CONNECTION_POOL_KWARGS': {
                'max_connections': 50,
                'retry_on_timeout': True
            },
            'COMPRESSOR': 'django_redis.compressors.zlib.ZlibCompressor',
        },
        'KEY_PREFIX': 'ttstats',
        'TIMEOUT': 300,  # 5 minutes default
    }
}

# Session storage (bonus: faster sessions)
SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
SESSION_CACHE_ALIAS = 'default'
```

### 1.3 Docker Compose Configuration

**File:** `compose.dev.yml`

```yaml
services:
  redis:
    image: redis:7-alpine
    container_name: ttstats_redis_dev
    ports:
      - "6379:6379"
    volumes:
      - redis_data_dev:/data
    command: redis-server --appendonly yes
    networks:
      - ttstats_network

  web:
    # ... existing config ...
    environment:
      - REDIS_URL=redis://redis:6379/1
    depends_on:
      - db
      - redis

volumes:
  redis_data_dev:
  # ... existing volumes ...
```

**File:** `compose.prod.yml` (similar changes)

```yaml
services:
  redis:
    image: redis:7-alpine
    container_name: ttstats_redis_prod
    volumes:
      - redis_data_prod:/data
    command: redis-server --appendonly yes --requirepass ${REDIS_PASSWORD}
    networks:
      - ttstats_network
    restart: unless-stopped

  web:
    environment:
      - REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/1

volumes:
  redis_data_prod:
```

### 1.4 Environment Variables

**File:** `.env.dev`
```bash
REDIS_URL=redis://redis:6379/1
```

**File:** `.env.prod.example`
```bash
REDIS_URL=redis://:your_secure_password_here@redis:6379/1
REDIS_PASSWORD=your_secure_password_here
```

### 1.5 Testing Infrastructure

```bash
# Test Redis connection
docker compose -f compose.dev.yml exec web python manage.py shell
>>> from django.core.cache import cache
>>> cache.set('test_key', 'Hello Redis!', 30)
>>> cache.get('test_key')
'Hello Redis!'
>>> cache.delete('test_key')
```

---

## Phase 2: Database Schema Improvements (Priority: HIGH)

### 2.1 Add Denormalized Fields to Match Model

**Why:** Avoid repeated COUNT queries for scores and confirmation status

**Migration:** `0017_match_denormalized_cache_fields.py`

```python
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [
        ('pingpong', '0016_scheduledmatch_match_link'),
    ]

    operations = [
        migrations.AddField(
            model_name='match',
            name='is_confirmed',
            field=models.BooleanField(default=False, db_index=True),
        ),
        migrations.AddField(
            model_name='match',
            name='team1_score_cache',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='match',
            name='team2_score_cache',
            field=models.IntegerField(default=0),
        ),
    ]
```

**Data migration:** `0018_populate_match_cache_fields.py`

```python
from django.db import migrations

def populate_cache_fields(apps, schema_editor):
    Match = apps.get_model('pingpong', 'Match')
    Game = apps.get_model('pingpong', 'Game')

    for match in Match.objects.all():
        # Update scores
        match.team1_score_cache = Game.objects.filter(
            match=match, winner=match.team1
        ).count()
        match.team2_score_cache = Game.objects.filter(
            match=match, winner=match.team2
        ).count()

        # Update confirmation status (simplified - full logic in app code)
        team1_players = match.team1.players.filter(user__profile__email_verified=True)
        team2_players = match.team2.players.filter(user__profile__email_verified=True)
        team1_confirmations = match.confirmations.filter(
            id__in=[p.id for p in team1_players]
        ).count()
        team2_confirmations = match.confirmations.filter(
            id__in=[p.id for p in team2_players]
        ).count()

        match.is_confirmed = (
            team1_confirmations == team1_players.count() and
            team2_confirmations == team2_players.count() and
            team1_players.count() > 0 and
            team2_players.count() > 0
        )

        match.save()

class Migration(migrations.Migration):
    dependencies = [
        ('pingpong', '0017_match_denormalized_cache_fields'),
    ]

    operations = [
        migrations.RunPython(populate_cache_fields, migrations.RunPython.noop),
    ]
```

### 2.2 Update Match Model Properties

**File:** `ttstats/pingpong/models.py`

```python
class Match(models.Model):
    # ... existing fields ...
    is_confirmed = models.BooleanField(default=False, db_index=True)
    team1_score_cache = models.IntegerField(default=0)
    team2_score_cache = models.IntegerField(default=0)

    @property
    def team1_score(self):
        """Use cached value, fallback to live query"""
        if self.pk and self.team1_score_cache is not None:
            return self.team1_score_cache
        return self.games.filter(winner=self.team1).count()

    @property
    def team2_score(self):
        """Use cached value, fallback to live query"""
        if self.pk and self.team2_score_cache is not None:
            return self.team2_score_cache
        return self.games.filter(winner=self.team2).count()

    @property
    def match_confirmed(self):
        """Use cached value, fallback to live check"""
        if self.pk and hasattr(self, '_is_confirmed_cached'):
            return self._is_confirmed_cached
        if self.pk and self.is_confirmed is not None:
            return self.is_confirmed
        # Fallback to live calculation
        return self.team1_confirmed and self.team2_confirmed

    def update_cache_fields(self):
        """Update all cached fields - call from signals"""
        self.team1_score_cache = self.games.filter(winner=self.team1).count()
        self.team2_score_cache = self.games.filter(winner=self.team2).count()
        self.is_confirmed = self._calculate_confirmation_status()

    def _calculate_confirmation_status(self):
        """Private method to calculate actual confirmation status"""
        team1_players = self.team1.players.filter(user__profile__email_verified=True)
        team2_players = self.team2.players.filter(user__profile__email_verified=True)

        if not team1_players.exists() or not team2_players.exists():
            return False

        team1_ids = {p.id for p in team1_players}
        team2_ids = {p.id for p in team2_players}
        confirmed_ids = {c.id for c in self.confirmations.all()}

        return team1_ids.issubset(confirmed_ids) and team2_ids.issubset(confirmed_ids)
```

### 2.3 Update Signals to Maintain Cache Fields

**File:** `ttstats/pingpong/signals.py`

```python
from django.core.cache import cache

@receiver(post_save, sender=Game)
def update_match_scores_on_game_save(sender, instance, **kwargs):
    """Update match score cache when game is saved"""
    match = instance.match
    match.update_cache_fields()
    match.save(update_fields=['team1_score_cache', 'team2_score_cache', 'is_confirmed'])

    # Invalidate related caches
    invalidate_match_caches(match)

@receiver(post_save, sender=MatchConfirmation)
def update_match_confirmation_cache(sender, instance, created, **kwargs):
    """Update match confirmation status when player confirms"""
    if created:
        match = instance.match
        match.update_cache_fields()
        match.save(update_fields=['is_confirmed'])

        # Invalidate related caches
        invalidate_match_caches(match)

def invalidate_match_caches(match):
    """Invalidate all caches related to a match"""
    # Get all players in the match
    all_players = set()
    all_players.update(match.team1.players.all())
    all_players.update(match.team2.players.all())

    # Invalidate player-specific caches
    for player in all_players:
        cache.delete(f'player_stats_{player.pk}')
        cache.delete(f'pending_matches_{player.pk}')
        if hasattr(player, 'user'):
            cache.delete(f'pending_matches_{player.user.pk}')

    # Invalidate head-to-head caches (singles only)
    if match.team1.players.count() == 1 and match.team2.players.count() == 1:
        p1 = match.team1.players.first()
        p2 = match.team2.players.first()
        cache_key = f'h2h_{min(p1.pk, p2.pk)}_{max(p2.pk, p2.pk)}'
        cache.delete(cache_key)

    # Invalidate team caches
    cache.delete(f'team_stats_{match.team1.pk}')
    cache.delete(f'team_stats_{match.team2.pk}')

    # Invalidate global caches
    cache.delete('leaderboard_data')
    cache.delete('dashboard_stats')
    cache.delete('total_players')
    cache.delete('total_confirmed_matches')
```

### 2.4 Add Denormalized Stats to Player (Optional - Phase 3)

```python
class Player(models.Model):
    # ... existing fields ...
    total_confirmed_matches = models.IntegerField(default=0, db_index=True)
    total_wins = models.IntegerField(default=0)
    total_losses = models.IntegerField(default=0)
    last_stats_update = models.DateTimeField(auto_now=True)
```

---

## Phase 3: View-Level Caching (Priority: HIGH)

### 3.1 Context Processor (CRITICAL - Every Page)

**File:** `ttstats/pingpong/context_processors.py`

**Current:** Lines 8-27 run on every request
**Impact:** 10-50ms per page load

```python
from django.core.cache import cache

def pingpong_context(request):
    """Add pending matches count to context - CACHED"""
    if not request.user.is_authenticated:
        return {'pending_matches_count': 0}

    player = getattr(request.user, 'player', None)
    if not player:
        return {'pending_matches_count': 0}

    # Try cache first (5 minute TTL)
    cache_key = f'pending_matches_{player.pk}'
    cached_count = cache.get(cache_key)

    if cached_count is not None:
        return {'pending_matches_count': cached_count}

    # Cache miss - calculate and store
    all_matches = Match.objects.filter(
        models.Q(team1__players=player) | models.Q(team2__players=player),
        is_confirmed=False  # Use database field instead of property!
    ).select_related('team1', 'team2').prefetch_related(
        'team1__players__user__profile',
        'team2__players__user__profile',
        'confirmations'
    ).distinct()

    # Much simpler now - just filter by is_confirmed field
    pending_matches_count = all_matches.count()

    # Cache for 5 minutes
    cache.set(cache_key, pending_matches_count, 300)

    return {'pending_matches_count': pending_matches_count}
```

**Cache Invalidation:** Handled by `invalidate_match_caches()` signal

### 3.2 LeaderboardView (MOST EXPENSIVE)

**File:** `ttstats/pingpong/views.py`

**Current:** Lines 615-675, O(n) complexity, 500-2000ms
**Solution:** Cache entire result for 10-15 minutes

```python
from django.views.decorators.cache import cache_page
from django.utils.decorators import method_decorator
from django.core.cache import cache

class LeaderboardView(LoginRequiredMixin, TemplateView):
    template_name = "pingpong/leaderboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Try cache first (10 minute TTL)
        cache_key = 'leaderboard_data'
        cached_data = cache.get(cache_key)

        if cached_data is not None:
            context['player_stats'] = cached_data
            context['cached'] = True
            context['cache_time'] = cache.ttl(cache_key)  # Time until expiry
            return context

        # Cache miss - calculate stats
        # Use database filtering instead of Python filtering!
        player_stats_qs = Player.objects.select_related('user').prefetch_related(
            'teams',
            'teams__matches_as_team1',
            'teams__matches_as_team2',
            'teams__matches_as_team1__games',
            'teams__matches_as_team2__games',
            'teams__matches_as_team1__winner__players',
            'teams__matches_as_team2__winner__players'
        )

        player_stats = []
        for player in player_stats_qs:
            # Get all confirmed matches (use database field!)
            all_matches = set()
            for team in player.teams.all():
                all_matches.update(
                    m for m in team.matches_as_team1.all() if m.is_confirmed
                )
                all_matches.update(
                    m for m in team.matches_as_team2.all() if m.is_confirmed
                )

            confirmed_matches = list(all_matches)
            total_matches = len(confirmed_matches)

            if total_matches == 0:
                continue

            wins = len([
                m for m in confirmed_matches
                if m.winner and player in m.winner.players.all()
            ])
            losses = total_matches - wins
            win_rate = (wins / total_matches * 100) if total_matches > 0 else 0

            # Use cached scores instead of property queries
            total_games = sum(
                m.team1_score_cache + m.team2_score_cache
                for m in confirmed_matches
            )

            player_stats.append({
                'player': player,
                'matches': total_matches,
                'wins': wins,
                'losses': losses,
                'win_rate': win_rate,
                'elo_rating': player.elo_rating,
                'total_games': total_games,
            })

        # Sort by Elo rating
        player_stats.sort(key=lambda x: x['elo_rating'], reverse=True)

        # Cache for 10 minutes
        cache.set(cache_key, player_stats, 600)

        context['player_stats'] = player_stats
        context['cached'] = False
        return context
```

**Template update** to show cache status:

```django
{# templates/pingpong/leaderboard.html #}
{% if cached %}
<div class="bg-blue-50 border border-blue-200 rounded p-2 mb-4 text-sm">
    Cached data (refreshes in {{ cache_time|default:"?" }} seconds)
</div>
{% endif %}
```

### 3.3 DashboardView

**File:** `ttstats/pingpong/views.py`

**Current:** Lines 265-291, fetches ALL matches
**Solution:** Cache counts and recent matches separately

```python
class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "pingpong/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Cache total players (rarely changes, 15 min TTL)
        total_players = cache.get('total_players')
        if total_players is None:
            total_players = Player.objects.count()
            cache.set('total_players', total_players, 900)

        # Cache total confirmed matches (10 min TTL)
        total_matches = cache.get('total_confirmed_matches')
        if total_matches is None:
            # Use database field, not Python filtering!
            total_matches = Match.objects.filter(is_confirmed=True).count()
            cache.set('total_confirmed_matches', total_matches, 600)

        # Recent matches (5 min TTL, smaller dataset)
        recent_matches = cache.get('recent_matches')
        if recent_matches is None:
            recent_matches = Match.objects.prefetch_related(
                "team1__players__user__profile",
                "team2__players__user__profile",
                "confirmations",
            ).order_by("-date_played")[:5]
            # Convert to list to cache
            recent_matches = list(recent_matches)
            cache.set('recent_matches', recent_matches, 300)

        context.update({
            'total_players': total_players,
            'total_matches': total_matches,
            'recent_matches': recent_matches,
        })

        return context
```

### 3.4 PlayerDetailView

**File:** `ttstats/pingpong/views.py`

**Current:** Lines 169-262, multiple loops through matches
**Solution:** Cache computed stats per player

```python
class PlayerDetailView(LoginRequiredMixin, DetailView):
    model = Player
    template_name = "pingpong/player_detail.html"
    context_object_name = "player"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        player = self.object

        # Try cache first (10 minute TTL per player)
        cache_key = f'player_stats_{player.pk}'
        cached_stats = cache.get(cache_key)

        if cached_stats is not None:
            context.update(cached_stats)
            return context

        # Cache miss - calculate stats
        all_matches = Match.objects.filter(
            Q(team1__players=player) | Q(team2__players=player),
            is_confirmed=True  # Use database field!
        ).select_related('team1', 'team2', 'winner').prefetch_related(
            'team1__players',
            'team2__players',
            'games',  # Prefetch games for score caching
        ).order_by('-date_played').distinct()

        confirmed_matches = list(all_matches)

        # Add attributes to matches (single pass)
        for match in confirmed_matches:
            is_team1 = player in match.team1.players.all()
            # Use cached scores!
            match.p1_score = match.team1_score_cache if is_team1 else match.team2_score_cache
            match.p2_score = match.team2_score_cache if is_team1 else match.team1_score_cache
            match.player_won = match.winner and player in match.winner.players.all()

        # Calculate stats
        wins = len([m for m in confirmed_matches if m.player_won])
        losses = len(confirmed_matches) - wins
        win_rate = (wins / len(confirmed_matches) * 100) if confirmed_matches else 0

        # Calculate streaks
        streaks = self._calculate_streaks(confirmed_matches)

        # Build stats dict for caching
        stats = {
            'matches': confirmed_matches,
            'total_matches': len(confirmed_matches),
            'wins': wins,
            'losses': losses,
            'win_rate': win_rate,
            **streaks,
        }

        # Cache for 10 minutes
        cache.set(cache_key, stats, 600)

        context.update(stats)
        return context
```

### 3.5 HeadToHeadStatsView

**File:** `ttstats/pingpong/views.py`

**Current:** Lines 678-934, very expensive calculations
**Solution:** Cache per player pair with 30 min TTL

```python
class HeadToHeadStatsView(LoginRequiredMixin, TemplateView):
    template_name = "pingpong/head_to_head.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        player1_id = self.request.GET.get("player1")
        player2_id = self.request.GET.get("player2")

        if not player1_id or not player2_id:
            # Show player selection form
            context["players"] = Player.objects.all().order_by("name")
            return context

        # Normalize cache key (always smaller ID first)
        cache_key = f'h2h_{min(player1_id, player2_id)}_{max(player1_id, player2_id)}'
        cached_stats = cache.get(cache_key)

        if cached_stats is not None:
            context.update(cached_stats)
            return context

        # Cache miss - calculate head-to-head stats
        player1 = get_object_or_404(Player, pk=player1_id)
        player2 = get_object_or_404(Player, pk=player2_id)

        # Rest of calculation logic using is_confirmed field...
        all_matches = (
            Match.objects.annotate(
                team1_player_count=Count('team1__players', distinct=True),
                team2_player_count=Count('team2__players', distinct=True)
            )
            .filter(
                team1_player_count=1,
                team2_player_count=1,
                is_confirmed=True,  # Database field!
            )
            .filter(
                Q(team1__players=player1, team2__players=player2) |
                Q(team1__players=player2, team2__players=player1)
            )
            .select_related("team1", "team2", "winner")
            .prefetch_related("games")
            .distinct()
            .order_by("date_played")
        )

        matches = list(all_matches)

        # ... expensive calculations ...

        # Build stats dict
        stats = {
            'player1': player1,
            'player2': player2,
            'matches': matches,
            # ... all calculated stats ...
        }

        # Cache for 30 minutes (matchups change infrequently)
        cache.set(cache_key, stats, 1800)

        context.update(stats)
        return context
```

---

## Phase 4: Cache Invalidation Strategy (Priority: CRITICAL)

### 4.1 Comprehensive Cache Invalidation Function

**File:** `ttstats/pingpong/cache_utils.py` (NEW)

```python
"""Cache invalidation utilities for TTStats"""
from django.core.cache import cache
from django.db.models import Q

def invalidate_match_caches(match):
    """
    Invalidate all caches related to a match.
    Call this from signals when matches are created/updated/confirmed.
    """
    # Get all players involved
    all_players = set()
    all_players.update(match.team1.players.all())
    all_players.update(match.team2.players.all())

    # Invalidate player-specific caches
    for player in all_players:
        cache.delete(f'player_stats_{player.pk}')
        if hasattr(player, 'user') and player.user:
            cache.delete(f'pending_matches_{player.pk}')

    # Invalidate head-to-head cache (singles only)
    if match.team1.players.count() == 1 and match.team2.players.count() == 1:
        p1 = match.team1.players.first()
        p2 = match.team2.players.first()
        if p1 and p2:
            cache_key = f'h2h_{min(p1.pk, p2.pk)}_{max(p1.pk, p2.pk)}'
            cache.delete(cache_key)

    # Invalidate team caches
    cache.delete(f'team_stats_{match.team1.pk}')
    cache.delete(f'team_stats_{match.team2.pk}')

    # Invalidate global caches
    cache.delete('leaderboard_data')
    cache.delete('dashboard_stats')
    cache.delete('recent_matches')
    cache.delete('total_confirmed_matches')

def invalidate_player_caches(player):
    """
    Invalidate all caches related to a player.
    Call when player is created/updated/deleted.
    """
    cache.delete(f'player_stats_{player.pk}')
    if hasattr(player, 'user') and player.user:
        cache.delete(f'pending_matches_{player.pk}')

    # Invalidate global caches
    cache.delete('leaderboard_data')
    cache.delete('total_players')

def invalidate_all_caches():
    """
    Nuclear option - clear all TTStats caches.
    Use for testing or major data migrations.
    """
    cache.clear()

def get_cache_stats():
    """
    Get cache statistics for debugging.
    Requires redis backend with specific commands.
    """
    try:
        from django_redis import get_redis_connection
        con = get_redis_connection("default")
        info = con.info()
        return {
            'keys': con.dbsize(),
            'memory': info.get('used_memory_human', 'Unknown'),
            'hits': info.get('keyspace_hits', 0),
            'misses': info.get('keyspace_misses', 0),
            'hit_rate': (
                info.get('keyspace_hits', 0) /
                (info.get('keyspace_hits', 0) + info.get('keyspace_misses', 1))
                * 100
            ),
        }
    except Exception as e:
        return {'error': str(e)}
```

### 4.2 Update Signals to Use Cache Utils

**File:** `ttstats/pingpong/signals.py`

```python
from .cache_utils import invalidate_match_caches, invalidate_player_caches

@receiver(post_save, sender=Game)
def update_match_scores_on_game_save(sender, instance, **kwargs):
    """Update match cache fields and invalidate caches when game is saved"""
    match = instance.match
    match.update_cache_fields()
    match.save(update_fields=['team1_score_cache', 'team2_score_cache', 'is_confirmed'])
    invalidate_match_caches(match)

@receiver(post_save, sender=MatchConfirmation)
def update_match_confirmation_cache(sender, instance, created, **kwargs):
    """Update match confirmation and invalidate caches when player confirms"""
    if created:
        match = instance.match
        old_status = match.is_confirmed
        match.update_cache_fields()
        match.save(update_fields=['is_confirmed'])

        # Only invalidate if confirmation status changed
        if old_status != match.is_confirmed:
            invalidate_match_caches(match)

@receiver(post_save, sender=Match)
def invalidate_on_match_save(sender, instance, created, **kwargs):
    """Invalidate caches when match is created or updated"""
    if created:
        invalidate_match_caches(instance)

@receiver(post_save, sender=Player)
def invalidate_on_player_save(sender, instance, created, **kwargs):
    """Invalidate caches when player is created or updated"""
    invalidate_player_caches(instance)

@receiver(post_delete, sender=Match)
def invalidate_on_match_delete(sender, instance, **kwargs):
    """Invalidate caches when match is deleted"""
    invalidate_match_caches(instance)

@receiver(post_delete, sender=Player)
def invalidate_on_player_delete(sender, instance, **kwargs):
    """Invalidate caches when player is deleted"""
    invalidate_player_caches(instance)
```

---

## Phase 5: Management Commands (Priority: MEDIUM)

### 5.1 Cache Management Command

**File:** `ttstats/pingpong/management/commands/cache_control.py` (NEW)

```python
"""Management command for cache operations"""
from django.core.management.base import BaseCommand
from django.core.cache import cache
from pingpong.cache_utils import invalidate_all_caches, get_cache_stats

class Command(BaseCommand):
    help = 'Manage Redis cache for TTStats'

    def add_arguments(self, parser):
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear all caches',
        )
        parser.add_argument(
            '--stats',
            action='store_true',
            help='Show cache statistics',
        )
        parser.add_argument(
            '--test',
            action='store_true',
            help='Test cache connectivity',
        )

    def handle(self, *args, **options):
        if options['clear']:
            self.stdout.write('Clearing all caches...')
            invalidate_all_caches()
            self.stdout.write(self.style.SUCCESS('All caches cleared'))

        elif options['stats']:
            self.stdout.write('Cache Statistics:')
            stats = get_cache_stats()
            if 'error' in stats:
                self.stdout.write(self.style.ERROR(f'Error: {stats["error"]}'))
            else:
                self.stdout.write(f'  Total keys: {stats["keys"]}')
                self.stdout.write(f'  Memory used: {stats["memory"]}')
                self.stdout.write(f'  Cache hits: {stats["hits"]}')
                self.stdout.write(f'  Cache misses: {stats["misses"]}')
                self.stdout.write(f'  Hit rate: {stats["hit_rate"]:.2f}%')

        elif options['test']:
            self.stdout.write('Testing cache connectivity...')
            try:
                cache.set('test_key', 'test_value', 30)
                value = cache.get('test_key')
                cache.delete('test_key')

                if value == 'test_value':
                    self.stdout.write(self.style.SUCCESS('Cache is working!'))
                else:
                    self.stdout.write(self.style.ERROR('Cache test failed'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Cache error: {e}'))

        else:
            self.stdout.write(self.style.WARNING('Use --clear, --stats, or --test'))
```

**Usage:**
```bash
python manage.py cache_control --stats   # View cache statistics
python manage.py cache_control --clear   # Clear all caches
python manage.py cache_control --test    # Test connectivity
```

### 5.2 Warm Cache Command

**File:** `ttstats/pingpong/management/commands/warm_cache.py` (NEW)

```python
"""Pre-populate frequently accessed caches"""
from django.core.management.base import BaseCommand
from django.core.cache import cache
from pingpong.models import Player, Match
from pingpong.views import LeaderboardView

class Command(BaseCommand):
    help = 'Warm frequently accessed caches'

    def handle(self, *args, **options):
        self.stdout.write('Warming caches...')

        # Warm total players count
        total_players = Player.objects.count()
        cache.set('total_players', total_players, 900)
        self.stdout.write(f'  ✓ Total players: {total_players}')

        # Warm total confirmed matches
        total_matches = Match.objects.filter(is_confirmed=True).count()
        cache.set('total_confirmed_matches', total_matches, 600)
        self.stdout.write(f'  ✓ Total matches: {total_matches}')

        # Warm leaderboard (expensive!)
        self.stdout.write('  Computing leaderboard...')
        view = LeaderboardView()
        context = view.get_context_data()
        self.stdout.write(f'  ✓ Leaderboard: {len(context["player_stats"])} players')

        # Warm recent matches
        recent_matches = list(
            Match.objects.prefetch_related(
                "team1__players",
                "team2__players",
            ).order_by("-date_played")[:5]
        )
        cache.set('recent_matches', recent_matches, 300)
        self.stdout.write(f'  ✓ Recent matches: {len(recent_matches)}')

        self.stdout.write(self.style.SUCCESS('Cache warming complete!'))
```

**Usage:**
```bash
python manage.py warm_cache  # Pre-populate caches (run after deployments)
```

---

## Phase 6: Testing Strategy (Priority: HIGH)

### 6.1 Cache Integration Tests

**File:** `ttstats/pingpong/tests/test_cache.py` (NEW)

```python
"""Test cache functionality"""
import pytest
from django.core.cache import cache
from .conftest import UserFactory, PlayerFactory, MatchFactory, confirm_match
from pingpong.cache_utils import (
    invalidate_match_caches,
    invalidate_player_caches,
    get_cache_stats
)

@pytest.mark.django_db
class TestCacheInvalidation:
    """Test that caches are properly invalidated"""

    def test_match_confirmation_invalidates_player_cache(self):
        """Confirming a match should clear player stats cache"""
        player = PlayerFactory(with_user=True)
        match = MatchFactory(player1=player, player2=PlayerFactory())

        # Populate cache
        cache_key = f'player_stats_{player.pk}'
        cache.set(cache_key, {'test': 'data'}, 600)
        assert cache.get(cache_key) is not None

        # Confirm match (triggers signal)
        confirm_match(match)

        # Cache should be invalidated
        assert cache.get(cache_key) is None

    def test_leaderboard_cache_invalidated_on_match_confirm(self):
        """Confirming a match should clear leaderboard cache"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        match = MatchFactory(player1=p1, player2=p2)

        # Populate leaderboard cache
        cache.set('leaderboard_data', [{'player': p1}], 600)
        assert cache.get('leaderboard_data') is not None

        # Confirm match
        confirm_match(match)

        # Leaderboard cache should be cleared
        assert cache.get('leaderboard_data') is None

    def test_head_to_head_cache_invalidated(self):
        """H2H cache should be cleared when those players play"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        match = MatchFactory(player1=p1, player2=p2)

        # Populate H2H cache
        cache_key = f'h2h_{min(p1.pk, p2.pk)}_{max(p1.pk, p2.pk)}'
        cache.set(cache_key, {'matches': []}, 1800)
        assert cache.get(cache_key) is not None

        # Confirm match
        confirm_match(match)

        # H2H cache should be cleared
        assert cache.get(cache_key) is None

@pytest.mark.django_db
class TestCachedViews:
    """Test that views properly use cache"""

    def test_context_processor_uses_cache(self, client):
        """Context processor should cache pending matches count"""
        user = UserFactory()
        player = PlayerFactory(user=user)
        client.force_login(user)

        # First request - cache miss
        cache_key = f'pending_matches_{player.pk}'
        assert cache.get(cache_key) is None

        response = client.get('/pingpong/')
        assert response.status_code == 200

        # Cache should now be populated
        cached_count = cache.get(cache_key)
        assert cached_count is not None
        assert cached_count == 0

    def test_leaderboard_caching(self, client):
        """Leaderboard should cache results"""
        user = UserFactory()
        PlayerFactory(user=user)
        client.force_login(user)

        # Clear cache
        cache.delete('leaderboard_data')

        # First request - cache miss
        response1 = client.get('/pingpong/leaderboard/')
        assert 'player_stats' in response1.context

        # Second request - cache hit (should be faster)
        response2 = client.get('/pingpong/leaderboard/')
        assert response2.context['player_stats'] == response1.context['player_stats']

        # Cache should exist
        assert cache.get('leaderboard_data') is not None

@pytest.mark.django_db
class TestCacheUtilities:
    """Test cache utility functions"""

    def test_get_cache_stats(self):
        """Should return cache statistics"""
        stats = get_cache_stats()

        # Should have keys (or error if Redis unavailable)
        assert 'keys' in stats or 'error' in stats

    def test_invalidate_match_caches_function(self):
        """Test direct cache invalidation function"""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        match = MatchFactory(player1=p1, player2=p2, confirmed=True)

        # Populate multiple caches
        cache.set(f'player_stats_{p1.pk}', {'data': 1}, 600)
        cache.set(f'player_stats_{p2.pk}', {'data': 2}, 600)
        cache.set('leaderboard_data', [], 600)

        # Invalidate
        invalidate_match_caches(match)

        # All should be cleared
        assert cache.get(f'player_stats_{p1.pk}') is None
        assert cache.get(f'player_stats_{p2.pk}') is None
        assert cache.get('leaderboard_data') is None
```

### 6.2 Performance Benchmarking Tests

**File:** `ttstats/pingpong/tests/test_cache_performance.py` (NEW)

```python
"""Benchmark cache performance improvements"""
import pytest
import time
from django.core.cache import cache
from .conftest import PlayerFactory, MatchFactory, GameFactory, confirm_match

@pytest.mark.django_db
class TestCachePerformance:
    """Verify cache improves performance"""

    def test_leaderboard_cache_performance(self, client):
        """Leaderboard should be significantly faster with cache"""
        # Create test data
        for _ in range(10):
            p1 = PlayerFactory(with_user=True)
            p2 = PlayerFactory(with_user=True)
            match = MatchFactory(player1=p1, player2=p2, best_of=5)
            for i in range(3):
                GameFactory(
                    match=match,
                    game_number=i+1,
                    team1_score=11,
                    team2_score=5
                )
            confirm_match(match)

        user = PlayerFactory(with_user=True, user__username='tester')
        client.force_login(user.user)

        # Clear cache
        cache.delete('leaderboard_data')

        # First request (cache miss)
        start = time.time()
        response1 = client.get('/pingpong/leaderboard/')
        time_uncached = time.time() - start

        # Second request (cache hit)
        start = time.time()
        response2 = client.get('/pingpong/leaderboard/')
        time_cached = time.time() - start

        # Cached should be significantly faster
        assert time_cached < time_uncached * 0.5  # At least 2x faster
        assert response1.status_code == 200
        assert response2.status_code == 200

    def test_context_processor_cache_performance(self, client):
        """Context processor should benefit from caching"""
        user = PlayerFactory(with_user=True, user__username='player').user
        client.force_login(user)

        # Create some matches
        for _ in range(5):
            MatchFactory(player1=user.player, player2=PlayerFactory())

        # Clear cache
        cache.delete(f'pending_matches_{user.player.pk}')

        # First request (cache miss)
        start = time.time()
        response1 = client.get('/pingpong/')
        time_uncached = time.time() - start

        # Second request (cache hit)
        start = time.time()
        response2 = client.get('/pingpong/')
        time_cached = time.time() - start

        # Should show improvement
        assert time_cached <= time_uncached
        assert response1.status_code == 200
```

### 6.3 Run Cache Tests

```bash
# Run all cache tests
cd ttstats && python -m pytest pingpong/tests/test_cache.py -v

# Run performance benchmarks
cd ttstats && python -m pytest pingpong/tests/test_cache_performance.py -v

# Run with coverage
cd ttstats && coverage run -m pytest pingpong/tests/test_cache*.py && coverage report
```

---

## Phase 7: Monitoring & Debugging (Priority: MEDIUM)

### 7.1 Cache Debug Middleware

**File:** `ttstats/ttstats/middleware.py`

```python
# Add to existing middleware file

import time
from django.conf import settings
from django.core.cache import cache

class CacheDebugMiddleware:
    """Add cache statistics to response headers (dev only)"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not settings.DEBUG:
            return self.get_response(request)

        # Record start time
        start_time = time.time()

        # Get initial cache stats
        try:
            from django_redis import get_redis_connection
            con = get_redis_connection("default")
            initial_info = con.info()
            initial_hits = initial_info.get('keyspace_hits', 0)
            initial_misses = initial_info.get('keyspace_misses', 0)
        except:
            initial_hits = initial_misses = 0

        # Process request
        response = self.get_response(request)

        # Calculate timing
        duration = time.time() - start_time

        # Get final cache stats
        try:
            final_info = con.info()
            final_hits = final_info.get('keyspace_hits', 0)
            final_misses = final_info.get('keyspace_misses', 0)

            request_hits = final_hits - initial_hits
            request_misses = final_misses - initial_misses

            # Add headers
            response['X-Cache-Hits'] = str(request_hits)
            response['X-Cache-Misses'] = str(request_misses)
            response['X-Request-Time'] = f'{duration:.3f}s'
        except:
            pass

        return response
```

**Enable in settings:**

```python
# settings/dev.py
MIDDLEWARE = [
    # ... existing middleware ...
    'ttstats.middleware.CacheDebugMiddleware',  # Add last
]
```

### 7.2 Cache Admin Panel

**File:** `ttstats/pingpong/admin.py`

```python
# Add cache statistics to admin dashboard

from django.contrib import admin
from django.urls import path
from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required
from .cache_utils import get_cache_stats

class CacheAdminView:
    """Custom admin view for cache statistics"""

    @staff_member_required
    def cache_stats_view(request):
        stats = get_cache_stats()

        context = {
            'site_title': 'Cache Statistics',
            'title': 'Redis Cache Statistics',
            'stats': stats,
        }

        return render(request, 'admin/cache_stats.html', context)

# Register custom URL in admin
def get_urls_override(original_get_urls):
    def get_urls():
        urls = original_get_urls()
        custom_urls = [
            path('cache-stats/', CacheAdminView.cache_stats_view, name='cache_stats'),
        ]
        return custom_urls + urls
    return get_urls

admin.site.get_urls = get_urls_override(admin.site.get_urls)
```

**Template:** `ttstats/pingpong/templates/admin/cache_stats.html` (NEW)

```django
{% extends "admin/base_site.html" %}

{% block content %}
<h1>Redis Cache Statistics</h1>

{% if stats.error %}
    <p class="errornote">Error: {{ stats.error }}</p>
{% else %}
    <table>
        <tr><th>Total Keys</th><td>{{ stats.keys }}</td></tr>
        <tr><th>Memory Used</th><td>{{ stats.memory }}</td></tr>
        <tr><th>Cache Hits</th><td>{{ stats.hits }}</td></tr>
        <tr><th>Cache Misses</th><td>{{ stats.misses }}</td></tr>
        <tr><th>Hit Rate</th><td>{{ stats.hit_rate|floatformat:2 }}%</td></tr>
    </table>

    <p><a href="{% url 'admin:index' %}" class="button">Back to Admin</a></p>
{% endif %}
{% endblock %}
```

### 7.3 Logging Configuration

**File:** `ttstats/settings/base.py`

```python
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class': 'logging.FileHandler',
            'filename': 'logs/cache.log',
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'pingpong.cache': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'django_redis': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}
```

**Add logging to cache utils:**

```python
# cache_utils.py
import logging

logger = logging.getLogger('pingpong.cache')

def invalidate_match_caches(match):
    logger.info(f'Invalidating caches for match {match.pk}')
    # ... rest of function ...
```

---

## Phase 8: Implementation Checklist

### Pre-Implementation
- [ ] Review plan with team
- [ ] Set up Redis locally for testing
- [ ] Create feature branch: `git checkout -b feature/redis-cache`

### Phase 1: Infrastructure (Day 1)
- [ ] Add redis and django-redis to requirements.txt
- [ ] Update compose.dev.yml with Redis service
- [ ] Update compose.prod.yml with Redis service
- [ ] Configure CACHES in settings/base.py
- [ ] Add REDIS_URL to .env files
- [ ] Test Redis connectivity
- [ ] Verify sessions work with Redis

### Phase 2: Database Schema (Day 2)
- [ ] Create migration 0017 (add cache fields)
- [ ] Create migration 0018 (populate cache fields)
- [ ] Run migrations on dev database
- [ ] Update Match.update_cache_fields() method
- [ ] Update Match properties to use cache fields
- [ ] Test cache fields work correctly

### Phase 3: Signal Updates (Day 3)
- [ ] Create cache_utils.py with invalidation functions
- [ ] Update signals.py to use cache utils
- [ ] Add logging to signal handlers
- [ ] Test signal-based invalidation
- [ ] Verify no circular import issues

### Phase 4: View Caching (Days 4-5)
- [ ] Update context_processors.py (CRITICAL)
- [ ] Update LeaderboardView
- [ ] Update DashboardView
- [ ] Update PlayerDetailView
- [ ] Update HeadToHeadStatsView
- [ ] Update TeamDetailView (if needed)
- [ ] Add cache status indicators to templates

### Phase 5: Testing (Day 6)
- [ ] Create test_cache.py
- [ ] Create test_cache_performance.py
- [ ] Run all cache tests
- [ ] Run full test suite
- [ ] Fix any regressions
- [ ] Verify 90%+ test coverage

### Phase 6: Management Commands (Day 7)
- [ ] Create cache_control.py command
- [ ] Create warm_cache.py command
- [ ] Test commands work correctly
- [ ] Document command usage

### Phase 7: Monitoring (Day 8)
- [ ] Add CacheDebugMiddleware
- [ ] Create cache admin view
- [ ] Set up cache logging
- [ ] Test monitoring tools

### Phase 8: Documentation & Deployment (Day 9-10)
- [ ] Update CLAUDE.md with cache strategy
- [ ] Add cache troubleshooting guide
- [ ] Update README with Redis setup
- [ ] Create deployment runbook
- [ ] Test on staging environment
- [ ] Performance benchmark before/after
- [ ] Deploy to production
- [ ] Monitor for 24 hours
- [ ] Document any issues

---

## Performance Targets

| Metric | Before | After | Target |
|--------|--------|-------|--------|
| Context Processor (avg) | 25ms | <2ms | 10x improvement |
| Leaderboard (cold) | 1000ms | 1000ms | Same (cache miss) |
| Leaderboard (warm) | 1000ms | <20ms | 50x improvement |
| Dashboard | 200ms | <15ms | 13x improvement |
| PlayerDetail | 100ms | <20ms | 5x improvement |
| HeadToHead | 300ms | <30ms | 10x improvement |
| Overall page load | 500ms | <100ms | 5x improvement |

---

## Cache Key Naming Convention

```
Format: {prefix}_{entity}_{identifier}_{variant?}

Examples:
- ttstats_player_stats_42
- ttstats_h2h_5_12
- ttstats_pending_matches_7
- ttstats_leaderboard_data
- ttstats_recent_matches
- ttstats_team_stats_15
```

**Prefix:** Set in settings (CACHES['default']['KEY_PREFIX'])
**Entity:** player, match, team, leaderboard, etc.
**Identifier:** Primary key or composite key
**Variant:** Optional descriptor (stats, matches, etc.)

---

## Cache TTL Strategy

| Cache Type | TTL | Reason |
|------------|-----|--------|
| Context processor | 5 min | Balance freshness vs performance |
| Leaderboard | 10 min | Changes infrequently, expensive to compute |
| Dashboard stats | 10 min | Global counts change slowly |
| Recent matches | 5 min | More dynamic, users expect freshness |
| Player stats | 10 min | Changes only on match confirm |
| Head-to-head | 30 min | Very stable, expensive to compute |
| Team stats | 10 min | Similar to player stats |
| Session data | Session | Django default |

**Strategy:** Short TTLs + aggressive invalidation = Fresh data + high cache hit rate

---

## Rollback Plan

If caching causes issues in production:

1. **Immediate mitigation:**
   ```bash
   python manage.py cache_control --clear
   ```

2. **Disable caching temporarily:**
   ```python
   # settings/prod.py
   CACHES = {
       'default': {
           'BACKEND': 'django.core.cache.backends.dummy.DummyCache',
       }
   }
   ```

3. **Revert database migrations:**
   ```bash
   python manage.py migrate pingpong 0016
   ```

4. **Monitor logs:**
   ```bash
   tail -f logs/cache.log
   docker compose logs -f redis
   ```

---

## Security Considerations

1. **Redis Password:** Always use password in production
2. **Network Isolation:** Redis should not be exposed externally
3. **Sensitive Data:** Don't cache passwords, tokens, or PII
4. **Cache Timing Attacks:** Cache keys should not leak sensitive info
5. **Redis Persistence:** Use AOF for durability in production

---

## Cost Analysis

**Infrastructure:**
- Development: Free (local Docker)
- Production: ~$5-10/month (Redis Cloud 250MB)
- Or self-hosted on existing VPS: $0

**Developer Time:**
- Implementation: ~10 days (1 developer)
- Testing: Included
- Maintenance: ~1 hour/month

**Return on Investment:**
- 5-50x performance improvement
- Better user experience
- Reduced database load
- Lower infrastructure costs (can use smaller DB)

**Break-even:** Immediate (if self-hosted), 1-2 months (if using Redis Cloud)

---

## Next Steps

1. **Review this plan** with team/stakeholders
2. **Set up local Redis** for development
3. **Start with Phase 1** (Infrastructure)
4. **Test each phase** before moving to next
5. **Monitor performance** throughout implementation
6. **Document learnings** for future reference

---

## Questions to Answer Before Implementation

1. **Redis hosting:** Self-hosted on VPS or managed service (Redis Cloud, AWS ElastiCache)?
2. **Backup strategy:** How often to backup Redis data? (Recommendation: daily snapshots)
3. **Monitoring:** Integrate with existing monitoring (New Relic, DataDog, etc.)?
4. **Cache warming:** Run warm_cache on every deployment or cron job?
5. **Testing strategy:** Performance test on production-like dataset?

---

## References

- [Django Cache Framework](https://docs.djangoproject.com/en/5.0/topics/cache/)
- [django-redis Documentation](https://github.com/jazzband/django-redis)
- [Redis Best Practices](https://redis.io/docs/manual/patterns/)
- [Caching Strategies](https://aws.amazon.com/caching/best-practices/)

---

**Document Version:** 1.0
**Last Updated:** 2026-02-02
**Author:** Claude (AI Assistant)
**Status:** Ready for Review
