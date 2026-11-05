"""Tests for the notification event layer.

`notifications.py` decides *what* to say and *who* to say it to;
`push.send_to_user` is patched throughout so these tests assert on the
decisions rather than on the transport. test_push.py covers the transport.

The signal-level tests at the bottom are the ones that matter most: they pin
down "push preferred, email as fallback", which is the rule most likely to
regress into either double-notifying everyone or silently notifying nobody.
"""

from datetime import date, datetime, time, timezone as dt_timezone

import pytest
from unittest.mock import patch

from django.core import mail
from django.test import override_settings

from pingpong import notifications, ratings
from pingpong.models import (
    Match, NotificationKind, NotificationPreference, PlayerRating, PushSubscription,
    RatingChange,
)
from pingpong.rating_pipeline import RatingScope

from .conftest import (
    GameFactory, MatchFactory, PlayerFactory, PlayerRatingFactory, SeasonFactory,
    confirm_match,
)

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


def confirmed_singles(p1, p2, **kwargs):
    """A best-of-3 won 2-0 by `p1`, confirmed, so its ratings are applied."""
    match = MatchFactory(player1=p1, player2=p2, best_of=3, **kwargs)
    play_out(match)
    confirm_match(match)
    match.refresh_from_db()
    return match


def _change(match, player, season=None):
    """The player's RatingChange for `match` in the season (or all-time) table."""
    return RatingChange.objects.get(match=match, player=player, rating__season=season)


def _noon(day):
    return datetime.combine(day, time(12), tzinfo=dt_timezone.utc)


def body_for(send, player):
    return next(
        c.kwargs['body'] for c in send.call_args_list if c.args[0].pk == player.user.pk
    )


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
    def test_body_carries_the_rating_delta(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        match = confirmed_singles(p1, p2)

        with patch('pingpong.push.send_to_user', return_value=1) as send:
            notifications.notify_match_result(match)

        winner_body = body_for(send, p1)
        change = _change(match, p1)
        assert 'You won' in winner_body
        assert change.glicko_change > 0
        assert winner_body.endswith(
            f" Rating {round(change.glicko_new_rating)} ({change.glicko_change:+d})."
        )

    def test_loser_is_told_they_lost(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        match = confirmed_singles(p1, p2)

        with patch('pingpong.push.send_to_user', return_value=1) as send:
            notifications.notify_match_result(match)

        loser_body = body_for(send, p2)
        change = _change(match, p2)
        assert 'You lost' in loser_body
        assert f"({change.glicko_change:+d})." in loser_body
        assert change.glicko_change < 0

    def test_reads_the_matchs_season_table_not_the_all_time_one(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        season = SeasonFactory()
        match = confirmed_singles(
            p1, p2, club=season.club, date_played=_noon(season.starts_on)
        )
        seasonal = _change(match, p1, season=season)
        RatingChange.objects.filter(match=match, rating__season__isnull=True).update(
            glicko_new_rating=999.0, glicko_change=-77
        )

        with patch('pingpong.push.send_to_user', return_value=1) as send:
            notifications.notify_match_result(match)

        body = body_for(send, p1)
        assert f" Rating {round(seasonal.glicko_new_rating)} ({seasonal.glicko_change:+d})." in body
        assert '999' not in body and '-77' not in body

    def test_a_skipped_projection_still_gets_the_result_without_a_rating(self):
        # NULL Glicko columns = the projection was skipped for this player.
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        match = confirmed_singles(p1, p2)
        RatingChange.objects.filter(match=match, player=p1).update(
            glicko_old_rating=None, glicko_new_rating=None, glicko_change=None
        )

        with patch('pingpong.push.send_to_user', return_value=1) as send:
            reached = notifications.notify_match_result(match)

        assert reached == {p1.pk, p2.pk}
        assert body_for(send, p1) == 'You won 2-0.'
        assert 'Rating' in body_for(send, p2)

    def test_sends_nothing_without_rating_changes(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        match = MatchFactory(player1=p1, player2=p2)

        with patch('pingpong.push.send_to_user') as send:
            reached = notifications.notify_match_result(match)

        assert reached == set()
        assert not send.called


def _rated(player, glicko_rating, **kwargs):
    kwargs.setdefault('format', 'singles')
    return PlayerRatingFactory(
        player=player, glicko_rating=glicko_rating, rd=80.0, volatility=0.06,
        last_rated_on=date(2026, 1, 1), matches=5, **kwargs,
    )


ALL_TIME_SINGLES = RatingScope(None, 'singles')


@pytest.mark.django_db
class TestPlayersBetween:
    """`ratings.players_between`: who a climb from old to new passed, in one table."""

    def test_finds_players_strictly_between_the_two_ratings(self):
        climber = PlayerFactory(name='Climber', with_user=True)
        passed = PlayerFactory(name='Passed', with_user=True)
        ahead = PlayerFactory(name='Ahead', with_user=True)
        _rated(passed, 1510.0)
        _rated(ahead, 1600.0)

        result = ratings.players_between(ALL_TIME_SINGLES, climber, 1500.0, 1520.0)

        assert list(result) == [passed]

    def test_a_tie_at_either_bound_is_not_an_overtake(self):
        climber = PlayerFactory(name='Climber', with_user=True)
        _rated(PlayerFactory(name='TiedBelow', with_user=True), 1500.0)
        _rated(PlayerFactory(name='TiedAbove', with_user=True), 1520.0)

        assert list(ratings.players_between(ALL_TIME_SINGLES, climber, 1500.0, 1520.0)) == []

    def test_empty_when_the_rating_did_not_rise(self):
        climber = PlayerFactory(name='Climber', with_user=True)
        _rated(PlayerFactory(name='Other', with_user=True), 1490.0)

        assert list(ratings.players_between(ALL_TIME_SINGLES, climber, 1500.0, 1480.0)) == []

    def test_ignores_players_with_no_account(self):
        climber = PlayerFactory(name='Climber', with_user=True)
        _rated(PlayerFactory(name='Guest'), 1510.0)  # no user, unreachable

        assert list(ratings.players_between(ALL_TIME_SINGLES, climber, 1500.0, 1520.0)) == []

    def test_ignores_unrated_rows_and_the_climber(self):
        climber = PlayerFactory(name='Climber', with_user=True)
        PlayerRatingFactory(player=PlayerFactory(name='Unrated', with_user=True))
        _rated(climber, 1510.0)

        assert list(ratings.players_between(ALL_TIME_SINGLES, climber, 1500.0, 1520.0)) == []

    def test_only_reads_the_scopes_own_table(self):
        climber = PlayerFactory(name='Climber', with_user=True)
        season = SeasonFactory()
        _rated(PlayerFactory(name='Doubles', with_user=True), 1510.0, format='doubles')
        _rated(PlayerFactory(name='Seasonal', with_user=True), 1510.0, season=season)
        all_time = PlayerFactory(name='AllTime', with_user=True)
        _rated(all_time, 1510.0)

        assert list(ratings.players_between(ALL_TIME_SINGLES, climber, 1500.0, 1520.0)) == [all_time]


@pytest.mark.django_db
class TestNotifyLeaderboardOvertakes:
    def test_notifies_the_player_who_was_passed(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        bystander = verified_player('Cleo')
        match = confirmed_singles(p1, p2)

        change = _change(match, p1)
        assert change.glicko_new_rating > change.glicko_old_rating
        # Park the bystander strictly inside the winner's climb.
        _rated(bystander, change.glicko_old_rating + 1)

        with patch('pingpong.push.send_to_user', return_value=1) as send:
            reached = notifications.notify_leaderboard_overtakes(match)

        assert reached == {bystander.pk}
        assert send.call_args.kwargs['kind'] == NotificationKind.LEADERBOARD_OVERTAKE
        assert 'Ada' in send.call_args.kwargs['body']

    def test_uses_the_matchs_season_table(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        in_season, all_time_only = verified_player('Cleo'), verified_player('Dora')
        season = SeasonFactory()
        match = confirmed_singles(
            p1, p2, club=season.club, date_played=_noon(season.starts_on)
        )
        change = _change(match, p1, season=season)
        _rated(in_season, change.glicko_old_rating + 1, season=season)
        _rated(all_time_only, change.glicko_old_rating + 1)

        with patch('pingpong.push.send_to_user', return_value=1):
            reached = notifications.notify_leaderboard_overtakes(match)

        assert reached == {in_season.pk}

    def test_never_notifies_the_match_participants(self):
        # They are already getting a match-result push; two buzzes for one
        # game is how people mute an app.
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        match = confirmed_singles(p1, p2)

        change = _change(match, p1)
        PlayerRating.all_objects.filter(player=p2, season=None, format='singles').update(
            glicko_rating=change.glicko_old_rating + 1
        )

        with patch('pingpong.push.send_to_user', return_value=1) as send:
            reached = notifications.notify_leaderboard_overtakes(match)

        assert reached == set()
        assert not send.called

    def test_a_skipped_projection_passes_nobody(self):
        p1, p2 = verified_player('Ada'), verified_player('Bob')
        bystander = verified_player('Cleo')
        match = confirmed_singles(p1, p2)
        change = _change(match, p1)
        _rated(bystander, change.glicko_old_rating + 1)
        RatingChange.objects.filter(match=match, player=p1).update(
            glicko_old_rating=None, glicko_new_rating=None, glicko_change=None
        )

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


def _season_ended(player, **overrides):
    kwargs = dict(
        season_name='Autumn 2026',
        standings=[('Singles', 2), ('Doubles', 5)],
        url='/pingpong/leaderboard/',
    )
    kwargs.update(overrides)
    return notifications.notify_season_ended(player, **kwargs)


@pytest.mark.django_db
class TestNotifySeasonEnded:
    """KAN-38: the season-rollover push. Takes plain values, not a Season, so
    the rollover job owns the domain and this module owns only the wording."""

    def test_reports_the_final_ranks_in_one_push(self):
        player = verified_player('Ada')

        with patch('pingpong.push.send_to_user', return_value=1) as send:
            reached = _season_ended(player)

        assert reached == {player.pk}
        assert send.call_count == 1
        kwargs = send.call_args.kwargs
        assert kwargs['kind'] == NotificationKind.SEASON_ENDED
        assert 'Autumn 2026' in kwargs['title']
        assert '#2 in Singles' in kwargs['body']
        assert '#5 in Doubles' in kwargs['body']
        assert kwargs['url'] == '/pingpong/leaderboard/'

    def test_player_without_an_account_is_not_reached(self):
        guest = PlayerFactory(name='Guest')

        with patch('pingpong.push.send_to_user') as send:
            assert _season_ended(guest) == set()

        assert not send.called

    def test_undelivered_push_is_not_reached(self):
        player = verified_player('Ada')

        with patch('pingpong.push.send_to_user', return_value=0):
            assert _season_ended(player) == set()

    @with_vapid
    def test_muted_kind_is_not_reached_so_the_caller_emails(self):
        from pingpong import push

        player = verified_player('Ada')
        subscribe(player)
        prefs = NotificationPreference.for_user(player.user)
        prefs.push_season_ended = False
        prefs.save()

        with patch('pywebpush.webpush') as mock_webpush:
            reached = _season_ended(player)

        assert reached == set()
        assert not mock_webpush.called
        assert push.user_has_push(player.user, NotificationKind.SEASON_ENDED) is False

    @with_vapid
    def test_second_send_the_same_day_is_rate_limited(self):
        player = verified_player('Ada')
        subscribe(player)

        with patch('pywebpush.webpush') as mock_webpush:
            first = _season_ended(player)
            second = _season_ended(player, season_name='Another season')

        assert first == {player.pk}
        assert second == set()
        assert mock_webpush.call_count == 1


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
