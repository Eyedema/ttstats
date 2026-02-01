import pytest
from django.core import mail
from django.test import override_settings

from pingpong.emails import send_match_confirmation_email, send_scheduled_match_email
from .conftest import (
    GameFactory,
    LocationFactory,
    MatchFactory,
    PlayerFactory,
    ScheduledMatchFactory,
)


@pytest.mark.django_db
class TestSendMatchConfirmationEmail:
    def _make_complete_match(self):
        m = MatchFactory()
        GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m, game_number=2, team1_score=11, team2_score=9)
        GameFactory(match=m, game_number=3, team1_score=11, team2_score=7)
        m.refresh_from_db()
        return m

    def test_win_result_player1(self):
        m = self._make_complete_match()
        mail.outbox.clear()
        send_match_confirmation_email(m, m.player1)
        assert len(mail.outbox) == 1
        assert "WON" in mail.outbox[0].body

    def test_loss_result_player2(self):
        m = self._make_complete_match()
        mail.outbox.clear()
        send_match_confirmation_email(m, m.player2)
        assert len(mail.outbox) == 1
        assert "LOST" in mail.outbox[0].body

    def test_score_order_player1_is_winner(self):
        m = self._make_complete_match()
        mail.outbox.clear()
        send_match_confirmation_email(m, m.player1)
        # Player1 won so score should be "3-0" (their score first)
        assert "3-0" in mail.outbox[0].body

    def test_score_order_player2_is_loser(self):
        m = self._make_complete_match()
        mail.outbox.clear()
        send_match_confirmation_email(m, m.player2)
        # Player2 lost 0-3 (their score first)
        assert "0-3" in mail.outbox[0].body

    def test_email_subject(self):
        m = self._make_complete_match()
        mail.outbox.clear()
        send_match_confirmation_email(m, m.player1)
        assert "Match Complete" in mail.outbox[0].subject

    def test_email_recipient(self):
        m = self._make_complete_match()
        mail.outbox.clear()
        send_match_confirmation_email(m, m.player1)
        assert mail.outbox[0].to == [m.player1.user.email]

    def test_url_construction_default(self):
        m = self._make_complete_match()
        mail.outbox.clear()
        send_match_confirmation_email(m, m.player1)
        assert f"/pingpong/matches/{m.pk}/" in mail.outbox[0].body

    @override_settings(SITE_PROTOCOL="https", SITE_DOMAIN="example.com")
    def test_url_construction_with_settings(self):
        m = self._make_complete_match()
        mail.outbox.clear()
        send_match_confirmation_email(m, m.player1)
        assert f"https://example.com/pingpong/matches/{m.pk}/" in mail.outbox[0].body


@pytest.mark.django_db
class TestSendScheduledMatchEmail:
    def test_email_sent(self):
        sm = ScheduledMatchFactory()
        mail.outbox.clear()
        send_scheduled_match_email(sm, sm.player1)
        assert len(mail.outbox) == 1
        assert sm.player1.user.email in mail.outbox[0].to

    def test_no_user_early_return(self):
        p1 = PlayerFactory()  # no user
        p2 = PlayerFactory(with_user=True)
        sm = ScheduledMatchFactory(player1=p1, player2=p2)
        mail.outbox.clear()
        send_scheduled_match_email(sm, p1)
        assert len(mail.outbox) == 0

    def test_no_email_early_return(self):
        u = PlayerFactory(with_user=True)
        u.user.email = ""
        u.user.save()
        p2 = PlayerFactory(with_user=True)
        sm = ScheduledMatchFactory(player1=u, player2=p2)
        mail.outbox.clear()
        send_scheduled_match_email(sm, u)
        assert len(mail.outbox) == 0

    def test_location_display_with_location(self):
        loc = LocationFactory(name="The Club")
        sm = ScheduledMatchFactory(location=loc)
        mail.outbox.clear()
        send_scheduled_match_email(sm, sm.player1)
        assert "The Club" in mail.outbox[0].body

    def test_location_display_tbd(self):
        sm = ScheduledMatchFactory(location=None)
        mail.outbox.clear()
        send_scheduled_match_email(sm, sm.player1)
        assert "TBD" in mail.outbox[0].body

    def test_date_time_formatting(self):
        from datetime import date, time

        sm = ScheduledMatchFactory(
            scheduled_date=date(2025, 6, 15),
            scheduled_time=time(14, 30),
        )
        mail.outbox.clear()
        send_scheduled_match_email(sm, sm.player1)
        body = mail.outbox[0].body
        assert "June 15, 2025" in body or "Sunday, June 15, 2025" in body
        assert "02:30 PM" in body

    def test_email_subject_contains_date(self):
        sm = ScheduledMatchFactory()
        mail.outbox.clear()
        send_scheduled_match_email(sm, sm.player1)
        assert "Match Scheduled" in mail.outbox[0].subject
