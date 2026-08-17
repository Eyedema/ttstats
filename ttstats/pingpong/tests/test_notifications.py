"""Tests for the notification event layer.

`notifications.py` decides *what* to say and *who* to say it to;
`push.send_to_user` is patched throughout so these tests assert on the
decisions rather than on the transport. test_push.py covers the transport.

The signal-level tests at the bottom are the ones that matter most: they pin
down "push preferred, email as fallback", which is the rule most likely to
regress into either double-notifying everyone or silently notifying nobody.
"""

import pytest
from unittest.mock import patch

from django.core import mail
from django.test import override_settings

from pingpong import notifications
from pingpong.models import (
    EloHistory, Match, NotificationKind, NotificationPreference, PushSubscription, Side,
)

from .conftest import GameFactory, MatchFactory, PlayerFactory, UserFactory, confirm_match

with_vapid = override_settings(
    VAPID_PUBLIC_KEY='test-public-key',
    VAPID_PRIVATE_KEY='test-private-key',
    VAPID_ADMIN_EMAIL='mailto:test@example.com',
)


def verified_player(name):
    """A player with a linked, email-verified user -- the only kind that gets
    confirmation notifications at all."""
    player = PlayerFactory(name=name, with_user=True)
    player.user.profile.email_verified = True
    player.user.profile.save()
    return player


def subscribe(player, endpoint=None):
    return PushSubscription.objects.create(
        user=player.user,
        endpoint=endpoint or f'https://push.example.com/{player.pk}',
        p256dh='p256dh-key',
        auth='auth-key',
    )


def play_out(match):
    """Drive a best-of-3 to 2-0 for side one, which sets the winner."""
    for game_number in (1, 2):
        GameFactory(
            match=match, game_number=game_number, team1_score=11, team2_score=5
        )
    match.refresh_from_db()
    return match


@pytest.mark.django_db
class TestNotifyMatchConfirmationNeeded:
    def test_names_the_opposing_side_not_the_player_themselves(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        match = MatchFactory(player1=p1, player2=p2)

        with patch('pingpong.push.send_to_user', return_value=1) as send:
            notifications.notify_match_confirmation_needed(match, [p1, p2])

        bodies = {c.kwargs['user'].pk if 'user' in c.kwargs else c.args[0].pk: c.kwargs['body']
                  for c in send.call_args_list}
        assert 'Bob' in bodies[p1.user.pk]
        assert 'Ada' in bodies[p2.user.pk]

    def test_returns_only_the_players_actually_reached(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        match = MatchFactory(player1=p1, player2=p2)

        # First call delivers, second reaches nobody.
        with patch('pingpong.push.send_to_user', side_effect=[1, 0]):
            reached = notifications.notify_match_confirmation_needed(match, [p1, p2])

        assert reached == {p1.pk}

    def test_skips_players_with_no_account(self):
        p1 = verified_player('Ada')
        p2 = PlayerFactory(name='Guest')  # no linked user
        match = MatchFactory(player1=p1, player2=p2)

        with patch('pingpong.push.send_to_user', return_value=1) as send:
            notifications.notify_match_confirmation_needed(match, [p1, p2])

        assert send.call_count == 1

    def test_tag_is_per_match_so_resends_replace(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        match = MatchFactory(player1=p1, player2=p2)

        with patch('pingpong.push.send_to_user', return_value=1) as send:
            notifications.notify_match_confirmation_needed(match, [p1])

        assert send.call_args.kwargs['tag'] == f'match-confirm-{match.pk}'


@pytest.mark.django_db
class TestNotifyMatchResult:
    def test_body_carries_the_elo_delta(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        match = MatchFactory(player1=p1, player2=p2, best_of=3)
        play_out(match)
        confirm_match(match)
        match.refresh_from_db()

        with patch('pingpong.push.send_to_user', return_value=1) as send:
            notifications.notify_match_result(match)

        winner_body = next(
            c.kwargs['body'] for c in send.call_args_list
            if c.args[0].pk == p1.user.pk
        )
        entry = EloHistory.objects.get(match=match, player=p1)
        assert 'You won' in winner_body
        assert str(entry.new_rating) in winner_body
        assert f'+{entry.rating_change}' in winner_body

    def test_loser_is_told_they_lost(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        match = MatchFactory(player1=p1, player2=p2, best_of=3)
        play_out(match)
        confirm_match(match)
        match.refresh_from_db()

        with patch('pingpong.push.send_to_user', return_value=1) as send:
            notifications.notify_match_result(match)

        loser_body = next(
            c.kwargs['body'] for c in send.call_args_list
            if c.args[0].pk == p2.user.pk
        )
        assert 'You lost' in loser_body

    def test_sends_nothing_without_elo_history(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        match = MatchFactory(player1=p1, player2=p2)

        with patch('pingpong.push.send_to_user') as send:
            reached = notifications.notify_match_result(match)

        assert reached == set()
        assert not send.called


@pytest.mark.django_db
class TestPlayersPassedOnLeaderboard:
    def test_finds_players_strictly_between_the_two_ratings(self):
        climber = PlayerFactory(name='Climber', with_user=True)
        passed = PlayerFactory(name='Passed', with_user=True, elo_rating=1510)
        ahead = PlayerFactory(name='Ahead', with_user=True, elo_rating=1600)

        result = notifications.players_passed_on_leaderboard(climber, 1500, 1520)

        assert list(result) == [passed]
        assert ahead not in result

    def test_a_tie_at_either_bound_is_not_an_overtake(self):
        climber = PlayerFactory(name='Climber', with_user=True)
        PlayerFactory(name='TiedBelow', with_user=True, elo_rating=1500)
        PlayerFactory(name='TiedAbove', with_user=True, elo_rating=1520)

        result = notifications.players_passed_on_leaderboard(climber, 1500, 1520)

        assert list(result) == []

    def test_empty_when_the_rating_did_not_rise(self):
        climber = PlayerFactory(name='Climber', with_user=True)
        PlayerFactory(name='Other', with_user=True, elo_rating=1490)

        assert list(
            notifications.players_passed_on_leaderboard(climber, 1500, 1480)
        ) == []

    def test_ignores_players_with_no_account(self):
        climber = PlayerFactory(name='Climber', with_user=True)
        PlayerFactory(name='Guest', elo_rating=1510)  # no user, unreachable

        assert list(
            notifications.players_passed_on_leaderboard(climber, 1500, 1520)
        ) == []


@pytest.mark.django_db
class TestNotifyLeaderboardOvertakes:
    def test_notifies_the_player_who_was_passed(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        bystander = verified_player('Cleo')
        match = MatchFactory(player1=p1, player2=p2, best_of=3)
        play_out(match)
        confirm_match(match)
        match.refresh_from_db()

        entry = EloHistory.objects.get(match=match, player=p1)
        # Park the bystander strictly inside the winner's climb.
        bystander.elo_rating = entry.old_rating + 1
        bystander.save()

        with patch('pingpong.push.send_to_user', return_value=1) as send:
            reached = notifications.notify_leaderboard_overtakes(match)

        assert reached == {bystander.pk}
        assert send.call_args.kwargs['kind'] == NotificationKind.LEADERBOARD_OVERTAKE
        assert 'Ada' in send.call_args.kwargs['body']

    def test_never_notifies_the_match_participants(self):
        # They are already getting a match-result push; two buzzes for one
        # game is how people mute an app.
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        match = MatchFactory(player1=p1, player2=p2, best_of=3)
        play_out(match)
        confirm_match(match)
        match.refresh_from_db()

        entry = EloHistory.objects.get(match=match, player=p1)
        p2.elo_rating = entry.old_rating + 1
        p2.save()

        with patch('pingpong.push.send_to_user', return_value=1) as send:
            reached = notifications.notify_leaderboard_overtakes(match)

        assert reached == set()
        assert not send.called


@pytest.mark.django_db
class TestNotifyMatchConfirmedOnce:
    def _confirmed_match(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        match = MatchFactory(player1=p1, player2=p2, best_of=3)
        play_out(match)
        confirm_match(match)
        match.refresh_from_db()
        return match

    def test_stamps_result_notified_at(self):
        match = self._confirmed_match()
        Match.all_objects.filter(pk=match.pk).update(result_notified_at=None)
        match.refresh_from_db()

        with patch('pingpong.push.send_to_user', return_value=1):
            notifications.notify_match_confirmed(match)

        match.refresh_from_db()
        assert match.result_notified_at is not None

    def test_second_call_sends_nothing(self):
        match = self._confirmed_match()
        Match.all_objects.filter(pk=match.pk).update(result_notified_at=None)
        match.refresh_from_db()

        with patch('pingpong.push.send_to_user', return_value=1):
            notifications.notify_match_confirmed(match)

        match.refresh_from_db()
        with patch('pingpong.push.send_to_user', return_value=1) as send:
            reached = notifications.notify_match_confirmed(match)

        assert reached == set()
        assert not send.called

    def test_a_stale_in_memory_copy_still_cannot_double_send(self):
        # The compare-and-set is what makes this safe: two requests holding
        # their own copy of the same match both see result_notified_at as
        # None, and only one of them wins the UPDATE.
        match = self._confirmed_match()
        Match.all_objects.filter(pk=match.pk).update(result_notified_at=None)
        first = Match.all_objects.get(pk=match.pk)
        second = Match.all_objects.get(pk=match.pk)

        with patch('pingpong.push.send_to_user', return_value=1) as send:
            notifications.notify_match_confirmed(first)
            notifications.notify_match_confirmed(second)

        # Two players in the match, so exactly one round of result pushes.
        assert send.call_count == 2

    def test_no_op_on_an_unconfirmed_match(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        match = MatchFactory(player1=p1, player2=p2, best_of=3)
        play_out(match)
        match.refresh_from_db()

        with patch('pingpong.push.send_to_user') as send:
            reached = notifications.notify_match_confirmed(match)

        assert reached == set()
        assert not send.called


@pytest.mark.django_db
class TestEmailFallback:
    """The push-preferred/email-fallback rule, exercised through the signals.

    This is the behaviour a future change is most likely to break, and it is
    invisible from either module in isolation.
    """

    @with_vapid
    def test_a_subscribed_player_gets_push_and_no_email(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        subscribe(p1)
        subscribe(p2)
        match = MatchFactory(player1=p1, player2=p2, best_of=3)
        mail.outbox.clear()

        with patch('pywebpush.webpush'):
            play_out(match)

        assert mail.outbox == []

    @with_vapid
    def test_an_unsubscribed_player_still_gets_the_email(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        subscribe(p1)  # p2 has no device
        match = MatchFactory(player1=p1, player2=p2, best_of=3)
        mail.outbox.clear()

        with patch('pywebpush.webpush'):
            play_out(match)

        recipients = [addr for m in mail.outbox for addr in m.to]
        assert p2.user.email in recipients
        assert p1.user.email not in recipients

    @with_vapid
    def test_a_failed_push_falls_back_to_email(self):
        # "Did not want it" and "could not get it" both have to end in the
        # user hearing about the match somehow.
        from pywebpush import WebPushException
        from unittest.mock import MagicMock

        exc = WebPushException('boom')
        exc.response = MagicMock(status_code=500)

        p1, p2 = verified_player('Ada'), verified_player('Bob')
        subscribe(p1)
        subscribe(p2)
        match = MatchFactory(player1=p1, player2=p2, best_of=3)
        mail.outbox.clear()

        with patch('pywebpush.webpush', side_effect=exc):
            play_out(match)

        recipients = [addr for m in mail.outbox for addr in m.to]
        assert p1.user.email in recipients
        assert p2.user.email in recipients

    @with_vapid
    def test_a_muted_kind_falls_back_to_email(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        subscribe(p1)
        subscribe(p2)
        prefs = NotificationPreference.for_user(p2.user)
        prefs.push_match_confirmation = False
        prefs.save()
        match = MatchFactory(player1=p1, player2=p2, best_of=3)
        mail.outbox.clear()

        with patch('pywebpush.webpush'):
            play_out(match)

        recipients = [addr for m in mail.outbox for addr in m.to]
        assert p2.user.email in recipients
        assert p1.user.email not in recipients

    def test_without_vapid_keys_everyone_gets_email_as_before(self):
        # The whole feature has to be a no-op on a server with no keys set,
        # which is dev, CI, and any deployment that has not configured it.
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        subscribe(p1)
        subscribe(p2)
        match = MatchFactory(player1=p1, player2=p2, best_of=3)
        mail.outbox.clear()

        with override_settings(VAPID_PUBLIC_KEY='', VAPID_PRIVATE_KEY=''):
            play_out(match)

        recipients = [addr for m in mail.outbox for addr in m.to]
        assert p1.user.email in recipients
        assert p2.user.email in recipients
