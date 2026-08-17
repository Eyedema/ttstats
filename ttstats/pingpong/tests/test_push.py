"""Tests for the web push transport layer and its models.

Everything that touches the network goes through `pywebpush.webpush`, which is
patched here. The point of `push.py` being a thin transport is that these
tests can be exhaustive about failure modes without any of the event logic
getting in the way -- see test_notifications.py for the other half.
"""

import pytest
from unittest.mock import MagicMock, patch

from django.test import override_settings

from pingpong.models import NotificationKind, NotificationPreference, PushSubscription
from pingpong import push

from .conftest import UserFactory

# Fake keys: push.webpush_enabled() only checks that both are non-empty, and
# webpush() itself is always mocked, so these never have to be valid.
with_vapid = override_settings(
    VAPID_PUBLIC_KEY='test-public-key',
    VAPID_PRIVATE_KEY='test-private-key',
    VAPID_ADMIN_EMAIL='mailto:test@example.com',
)


def make_subscription(user, endpoint='https://push.example.com/abc'):
    return PushSubscription.objects.create(
        user=user, endpoint=endpoint, p256dh='p256dh-key', auth='auth-key'
    )


def webpush_error(status_code):
    """A WebPushException carrying an HTTP status, as pywebpush raises."""
    from pywebpush import WebPushException

    exc = WebPushException('boom')
    exc.response = MagicMock(status_code=status_code)
    return exc


@pytest.mark.django_db
class TestNotificationPreference:
    def test_for_user_creates_a_row_on_demand(self):
        user = UserFactory()
        NotificationPreference.objects.filter(user=user).delete()

        prefs = NotificationPreference.for_user(user)

        assert prefs.pk is not None
        assert NotificationPreference.objects.filter(user=user).count() == 1

    def test_for_user_is_idempotent(self):
        user = UserFactory()

        first = NotificationPreference.for_user(user)
        second = NotificationPreference.for_user(user)

        assert first.pk == second.pk

    def test_created_by_signal_on_user_creation(self):
        user = UserFactory()

        assert NotificationPreference.objects.filter(user=user).exists()

    def test_wants_reads_the_matching_field(self):
        prefs = NotificationPreference.for_user(UserFactory())
        prefs.push_match_result = False
        prefs.save()

        assert prefs.wants(NotificationKind.MATCH_CONFIRMATION) is True
        assert prefs.wants(NotificationKind.MATCH_RESULT) is False

    def test_overtake_is_off_by_default(self):
        # The one notification that fires without the user having done
        # anything, so it has to be opted into.
        prefs = NotificationPreference.for_user(UserFactory())

        assert prefs.wants(NotificationKind.LEADERBOARD_OVERTAKE) is False

    def test_unknown_kind_defaults_to_wanted(self):
        # A new notification type shipped without its preference field should
        # be noisy and get noticed, not silently deliver to nobody.
        prefs = NotificationPreference.for_user(UserFactory())

        assert prefs.wants('some_kind_that_does_not_exist') is True


@pytest.mark.django_db
class TestPushSubscriptionModel:
    def test_subscription_info_matches_pywebpush_shape(self):
        sub = make_subscription(UserFactory())

        assert sub.subscription_info == {
            'endpoint': 'https://push.example.com/abc',
            'keys': {'p256dh': 'p256dh-key', 'auth': 'auth-key'},
        }

    def test_endpoint_is_unique(self):
        from django.db import IntegrityError

        user = UserFactory()
        make_subscription(user)

        with pytest.raises(IntegrityError):
            make_subscription(UserFactory())


class TestWebpushEnabled:
    def test_false_without_keys(self):
        with override_settings(VAPID_PUBLIC_KEY='', VAPID_PRIVATE_KEY=''):
            assert push.webpush_enabled() is False

    def test_false_with_only_one_key(self):
        with override_settings(VAPID_PUBLIC_KEY='pub', VAPID_PRIVATE_KEY=''):
            assert push.webpush_enabled() is False

    @with_vapid
    def test_true_with_both_keys(self):
        assert push.webpush_enabled() is True


class TestBuildPayload:
    def test_tag_defaults_to_kind(self):
        payload = push.build_payload(
            title='T', body='B', url='/u/', kind='match_result'
        )

        assert payload['tag'] == 'match_result'

    def test_explicit_tag_wins(self):
        payload = push.build_payload(
            title='T', body='B', url='/u/', kind='match_result', tag='match-7'
        )

        assert payload['tag'] == 'match-7'


@pytest.mark.django_db
class TestSendToSubscription:
    @with_vapid
    def test_success_stamps_last_success_and_clears_failures(self):
        sub = make_subscription(UserFactory())
        sub.failure_count = 3
        sub.save()

        with patch('pywebpush.webpush') as mock_webpush:
            result = push.send_to_subscription(sub, {'title': 'hi'})

        assert result is True
        assert mock_webpush.called
        sub.refresh_from_db()
        assert sub.last_success_at is not None
        assert sub.failure_count == 0

    @with_vapid
    @pytest.mark.parametrize('status', [404, 410])
    def test_gone_status_prunes_the_subscription(self, status):
        # A dead device must not linger: it would keep the user looking
        # "reachable" and so keep suppressing their email fallback forever.
        sub = make_subscription(UserFactory())

        with patch('pywebpush.webpush', side_effect=webpush_error(status)):
            result = push.send_to_subscription(sub, {'title': 'hi'})

        assert result is False
        assert not PushSubscription.objects.filter(pk=sub.pk).exists()

    @with_vapid
    def test_transient_status_keeps_the_row_and_counts_the_failure(self):
        sub = make_subscription(UserFactory())

        with patch('pywebpush.webpush', side_effect=webpush_error(500)):
            result = push.send_to_subscription(sub, {'title': 'hi'})

        assert result is False
        sub.refresh_from_db()
        assert sub.failure_count == 1

    @with_vapid
    def test_unexpected_exception_is_swallowed(self):
        # A dead push service must never be able to fail a match save.
        sub = make_subscription(UserFactory())

        with patch('pywebpush.webpush', side_effect=OSError('DNS is down')):
            result = push.send_to_subscription(sub, {'title': 'hi'})

        assert result is False
        sub.refresh_from_db()
        assert sub.failure_count == 1

    def test_no_op_when_push_is_not_configured(self):
        sub = make_subscription(UserFactory())

        with override_settings(VAPID_PUBLIC_KEY='', VAPID_PRIVATE_KEY=''):
            with patch('pywebpush.webpush') as mock_webpush:
                result = push.send_to_subscription(sub, {'title': 'hi'})

        assert result is False
        assert not mock_webpush.called


@pytest.mark.django_db
class TestSendToUser:
    @with_vapid
    def test_delivers_to_every_device(self):
        user = UserFactory()
        make_subscription(user, 'https://push.example.com/one')
        make_subscription(user, 'https://push.example.com/two')

        with patch('pywebpush.webpush') as mock_webpush:
            delivered = push.send_to_user(
                user, kind=NotificationKind.MATCH_RESULT,
                title='T', body='B', url='/u/',
            )

        assert delivered == 2
        assert mock_webpush.call_count == 2

    @with_vapid
    def test_returns_zero_with_no_devices(self):
        user = UserFactory()

        delivered = push.send_to_user(
            user, kind=NotificationKind.MATCH_RESULT, title='T', body='B', url='/u/'
        )

        assert delivered == 0

    @with_vapid
    def test_muted_kind_is_not_sent(self):
        user = UserFactory()
        make_subscription(user)
        prefs = NotificationPreference.for_user(user)
        prefs.push_match_result = False
        prefs.save()

        with patch('pywebpush.webpush') as mock_webpush:
            delivered = push.send_to_user(
                user, kind=NotificationKind.MATCH_RESULT,
                title='T', body='B', url='/u/',
            )

        assert delivered == 0
        assert not mock_webpush.called

    @with_vapid
    def test_respect_preferences_false_bypasses_the_mute(self):
        # The "send a test" button: the user just pressed it, so they want
        # this one regardless of what they have muted.
        user = UserFactory()
        make_subscription(user)
        prefs = NotificationPreference.for_user(user)
        prefs.push_match_result = False
        prefs.save()

        with patch('pywebpush.webpush'):
            delivered = push.send_to_user(
                user, kind=NotificationKind.MATCH_RESULT,
                title='T', body='B', url='/u/', respect_preferences=False,
            )

        assert delivered == 1

    @with_vapid
    def test_none_user_is_not_an_error(self):
        assert push.send_to_user(
            None, kind=NotificationKind.MATCH_RESULT, title='T', body='B', url='/u/'
        ) == 0

    @with_vapid
    def test_partial_delivery_counts_only_what_worked(self):
        user = UserFactory()
        make_subscription(user, 'https://push.example.com/one')
        make_subscription(user, 'https://push.example.com/two')

        with patch('pywebpush.webpush', side_effect=[None, webpush_error(500)]):
            delivered = push.send_to_user(
                user, kind=NotificationKind.MATCH_RESULT,
                title='T', body='B', url='/u/',
            )

        assert delivered == 1


@pytest.mark.django_db
class TestUserHasPush:
    @with_vapid
    def test_true_with_a_subscription(self):
        user = UserFactory()
        make_subscription(user)

        assert push.user_has_push(user) is True

    @with_vapid
    def test_false_without_a_subscription(self):
        assert push.user_has_push(UserFactory()) is False

    def test_false_when_push_is_not_configured(self):
        user = UserFactory()
        make_subscription(user)

        with override_settings(VAPID_PUBLIC_KEY='', VAPID_PRIVATE_KEY=''):
            assert push.user_has_push(user) is False

    @with_vapid
    def test_false_for_a_muted_kind(self):
        # Muting a push is not the same as asking to hear nothing, so this
        # returning False is what keeps the email fallback firing.
        user = UserFactory()
        make_subscription(user)
        prefs = NotificationPreference.for_user(user)
        prefs.push_match_result = False
        prefs.save()

        assert push.user_has_push(user, NotificationKind.MATCH_RESULT) is False
        assert push.user_has_push(user, NotificationKind.MATCH_CONFIRMATION) is True
