# pingpong/context_processors.py
from django.conf import settings
from django.core.cache import cache
from django.middleware.csrf import get_token
from django.urls import reverse

from . import cache_utils
from .managers import scope_to_active_club
from .models import Championship, Club, Match, Player

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

    # Every one of these keys is club-namespaced: they summarise
    # club-scoped queries, and an unnamespaced key means the first club to
    # render a page serves its numbers to every other.
    prefix = cache_utils.club_prefix()
    cache_key = f'{prefix}pending_matches_{player.pk}'
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

    live_key = f'{prefix}live_championships_count'
    live_championships_count = cache.get(live_key)
    if live_championships_count is None:
        # all_objects bypasses the *user* filter -- a badge counting only
        # the championships you entered is not a badge. Scoped, so it does
        # not also bypass the club filter.
        live_championships_count = scope_to_active_club(
            Championship.all_objects.filter(
                status=Championship.Status.IN_PROGRESS
            )
        ).count()
        cache.set(live_key, live_championships_count, 300)

    rank_key = f'{prefix}player_rank_{player.pk}'
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


def club_context(request):
    """The active club and the viewer's clubs, for every page render.

    Kept separate from `pingpong_context` for the same reason `push_context`
    is: that function early-returns in three places, and a user without a
    Player would lose the club name from their chrome on exactly the pages a
    half-set-up account sees.

    **Every value is lazy.** A context processor runs on every render, and
    until the S1.8 switcher exists no template reads any of this -- so
    resolving eagerly would add a query to every page in the app in exchange
    for nothing. `SimpleLazyObject` means the membership query fires on first
    *read*, and `get_user_clubs()` memoizes it, so a page that renders the club
    name and the switcher still costs exactly one query.

    `active_club` is None for anonymous users and for someone who belongs to no
    club yet -- every template using it must handle that, because a fresh
    signup is in that state until they accept an invite.
    """
    from django.utils.functional import SimpleLazyObject

    from ttstats.middleware import (
        get_current_club,
        get_current_membership,
        get_user_clubs,
    )

    if not request.user.is_authenticated:
        return {'active_club': None, 'user_clubs': [], 'has_multiple_clubs': False}

    def _is_club_admin():
        # Read off the memoized membership list rather than re-querying:
        # deciding whether to draw one menu row must not cost a query on every
        # page in the app.
        membership = get_current_membership()
        return bool(membership and membership.is_admin)

    return {
        'active_club': SimpleLazyObject(get_current_club),
        'user_clubs': SimpleLazyObject(get_user_clubs),
        'has_multiple_clubs': SimpleLazyObject(lambda: len(get_user_clubs()) > 1),
        # Whether to offer the Club settings row. A template cannot call
        # `club.user_can_administer(user)` -- Django templates take no
        # arguments -- and the alternative, a template filter, would put a
        # permission decision somewhere nobody thinks to audit.
        'club_admin': SimpleLazyObject(_is_club_admin),
    }
