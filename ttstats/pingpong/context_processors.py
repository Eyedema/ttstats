# pingpong/context_processors.py
from django.conf import settings
from django.core.cache import cache
from django.middleware.csrf import get_token
from django.urls import reverse

from .models import Championship, Match, Player

# --- The navigation spine ---------------------------------------------------
# Nine identical sidebar links became four daily destinations in a bottom tab
# bar -- Today, Play, Table, Cups -- with everything occasional in the drawer.
# Play is a button, not a page of links to browse: it is the only thing in the
# app that *starts* something.
#
# The mapping is by url_name rather than by path prefix so that a URL moving
# does not silently unhighlight its tab. A destination that is not a tab (the
# drawer's Calendar, Head to head, Everyone, All matches) deliberately
# highlights nothing -- claiming a tab the user did not tap is worse than
# showing no selection at all.
TAB_FOR_URL_NAME = {
    'dashboard': 'today',

    'play': 'play',
    'match_add': 'play',
    'match_edit': 'play',
    'match_schedule': 'play',
    'game_add': 'play',
    'live_scoreboard': 'play',
    'scheduled_match_convert': 'play',
    'scheduled_match_edit': 'play',

    'leaderboard': 'table',

    'championship_list': 'cups',
    'championship_detail': 'cups',
    'championship_create': 'cups',
    'championship_edit': 'cups',
}


def pingpong_context(request):
    """Badge counts and the active tab, for every authenticated page render."""
    empty = {
        'pending_matches_count': 0,
        'live_matches_count': 0,
        'nav_tab': None,
        'nav_player': None,
        'nav_player_rank': None,
        'live_championships_count': 0,
    }

    resolver = getattr(request, 'resolver_match', None)
    nav_tab = TAB_FOR_URL_NAME.get(resolver.url_name) if resolver else None

    if not request.user.is_authenticated:
        return {**empty, 'nav_tab': nav_tab}

    player = getattr(request.user, 'player', None)
    if not player:
        return {**empty, 'nav_tab': nav_tab}

    cache_key = f'pending_matches_{player.pk}'
    pending_matches_count = cache.get(cache_key)
    if pending_matches_count is None:
        # Denormalized is_confirmed, so this filters in the DB rather than in
        # Python over every match the user can see.
        pending_matches_count = Match.objects.filter(
            participants__player=player,
            is_confirmed=False,
            winner_side__isnull=False,
        ).distinct().count()
        cache.set(cache_key, pending_matches_count, 300)

    # Not cached: amber means live *right now*, and a five-minute-stale amber
    # dot is worse than none. It is one indexed boolean lookup.
    live_matches_count = Match.live_objects.filter(
        participants__player=player, is_live=True
    ).distinct().count()

    live_championships_count = cache.get('live_championships_count')
    if live_championships_count is None:
        live_championships_count = Championship.all_objects.filter(
            status=Championship.Status.IN_PROGRESS
        ).count()
        cache.set('live_championships_count', live_championships_count, 300)

    rank_key = f'player_rank_{player.pk}'
    rank = cache.get(rank_key)
    if rank is None:
        rank = Player.objects.filter(elo_rating__gt=player.elo_rating).count() + 1
        cache.set(rank_key, rank, 300)

    return {
        'pending_matches_count': pending_matches_count,
        'live_matches_count': live_matches_count,
        'nav_tab': nav_tab,
        'nav_player': player,
        'nav_player_rank': rank,
        'live_championships_count': live_championships_count,
    }


def push_context(request):
    """Config for push.js, rendered into base.html via json_script.

    Kept separate from pingpong_context, which early-returns in three places
    and would drop this for any user without a Player -- leaving push.js with
    no config on exactly the pages a half-set-up account sees.

    Only the *public* VAPID key goes in here. It is handed to the browser by
    design; the private key must never reach a template.
    """
    return {
        'push_config': {
            'enabled': bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY),
            'vapidPublicKey': settings.VAPID_PUBLIC_KEY,
            # The CSRF cookie is HttpOnly, so JS cannot read the token from
            # document.cookie. Same reason base.html sets hx-headers.
            'csrfToken': get_token(request),
            'urls': {
                'serviceWorker': reverse('service_worker'),
                'subscribe': reverse('pingpong:push_subscribe'),
                'unsubscribe': reverse('pingpong:push_unsubscribe'),
                'test': reverse('pingpong:push_test'),
            },
        }
    }
