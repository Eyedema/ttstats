"""Web Push transport.

This module knows how to get a payload onto a device and nothing about what
the payloads mean -- `notifications.py` owns that. The split matters because
everything here talks to the network and has to be mocked in tests, while the
event logic should be testable without any mocking at all.

Delivery is synchronous, inside the request that triggered it. That is a
deliberate choice for a friend-group deployment: there is no Celery here and
the VPS has no room for one, a match involves at most eight subscriptions, and
push services answer in well under a second. `WEBPUSH_TIMEOUT` bounds the
damage if one hangs. If this ever grows past a handful of players per event,
the fix is a queue behind `send_to_subscription`, not a rewrite of the callers.

Nothing in here is allowed to raise into a caller. A dead push service must
never be able to fail a match save.
"""

import json
import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# How long to wait on a single push service before giving up, in seconds.
WEBPUSH_TIMEOUT = getattr(settings, 'WEBPUSH_TIMEOUT', 5)

# A push service returns these when the subscription is permanently gone --
# the user uninstalled the PWA, cleared site data, or the browser rotated it.
# Any other status is treated as transient and the row is kept.
GONE_STATUS_CODES = (404, 410)


def webpush_enabled():
    """False when VAPID keys are unset, which makes every send a no-op.

    Read through a function rather than importing the setting once, so tests
    can flip it with `override_settings`.
    """
    return bool(getattr(settings, 'VAPID_PRIVATE_KEY', '') and
                getattr(settings, 'VAPID_PUBLIC_KEY', ''))


def build_payload(*, title, body, url, kind, tag=None):
    """The JSON the service worker's push handler receives.

    `tag` lets a later notification replace an earlier one on screen instead
    of stacking -- two confirmation reminders for the same match should be one
    line in the shade, not two.
    """
    return {
        'title': title,
        'body': body,
        'url': url,
        'kind': kind,
        'tag': tag or kind,
    }


def send_to_subscription(subscription, payload):
    """Deliver one payload to one device. Returns True on success.

    Prunes the subscription when the push service says it is permanently
    gone, so dead devices do not accumulate and do not keep a user looking
    "reachable" to the email-fallback check.
    """
    if not webpush_enabled():
        return False

    # Imported lazily: pywebpush pulls in cryptography and http_ece, and this
    # module is imported by signals at startup on every deployment, including
    # ones with no VAPID keys configured at all.
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info=subscription.subscription_info,
            data=json.dumps(payload),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={'sub': settings.VAPID_ADMIN_EMAIL},
            timeout=WEBPUSH_TIMEOUT,
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if status in GONE_STATUS_CODES:
            logger.info(
                "Pruning dead push subscription %s for user %s (HTTP %s)",
                subscription.pk, subscription.user_id, status,
            )
            subscription.delete()
        else:
            subscription.failure_count += 1
            subscription.save(update_fields=['failure_count'])
            logger.warning(
                "Push to subscription %s failed (HTTP %s): %s",
                subscription.pk, status, exc,
            )
        return False
    except Exception:
        # Network errors, DNS failures, a malformed stored key. Never let one
        # bubble into the signal that is trying to save a match.
        subscription.failure_count += 1
        subscription.save(update_fields=['failure_count'])
        logger.exception("Unexpected error pushing to subscription %s", subscription.pk)
        return False

    subscription.last_success_at = timezone.now()
    subscription.failure_count = 0
    subscription.save(update_fields=['last_success_at', 'failure_count'])
    return True


def send_to_user(user, *, kind, title, body, url, tag=None, respect_preferences=True):
    """Push to every device a user has registered. Returns the number delivered.

    Returns 0 -- not an error -- when the user has opted out, has no devices,
    or push is not configured. Callers use that to decide whether to fall back
    to email, so the distinction between "did not want it" and "could not get
    it" is deliberately collapsed: in both cases the user did not get a push.
    """
    from .models import NotificationPreference

    if not webpush_enabled() or user is None or not user.is_authenticated:
        return 0

    if respect_preferences and not NotificationPreference.for_user(user).wants(kind):
        return 0

    payload = build_payload(title=title, body=body, url=url, kind=kind, tag=tag)
    delivered = 0
    for subscription in user.push_subscriptions.all():
        if send_to_subscription(subscription, payload):
            delivered += 1
    return delivered


def user_has_push(user, kind=None):
    """True if a push would plausibly reach this user, used to suppress email.

    Deliberately conservative: it checks for a live subscription *and* that
    the user has not muted this kind. A user who muted match results still
    gets the email, because muting a push is not the same as asking to hear
    nothing.
    """
    from .models import NotificationPreference

    if not webpush_enabled() or user is None or not user.is_authenticated:
        return False
    if not user.push_subscriptions.exists():
        return False
    if kind is not None and not NotificationPreference.for_user(user).wants(kind):
        return False
    return True
