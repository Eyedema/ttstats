# pingpong/context_processors.py
from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.middleware.csrf import get_token
from django.urls import reverse

from .models import Match


def pingpong_context(request):
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

    # Cache miss - use denormalized is_confirmed field for DB-level filtering
    pending_matches_count = Match.objects.filter(
        participants__player=player,
        is_confirmed=False,
        winner_side__isnull=False,
    ).distinct().count()

    # Cache for 5 minutes
    cache.set(cache_key, pending_matches_count, 300)

    return {'pending_matches_count': pending_matches_count}


def push_context(request):
    """Config for push.js, rendered into base.html via json_script.

    Kept separate from pingpong_context, which early-returns in three places
    and would drop this on anonymous requests -- push.js runs on the login
    page too, since re-asserting an existing subscription should not require
    being logged in to have already happened.

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
