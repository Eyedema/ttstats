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
        send_match_confirmation_email(m, m.side1_players.first())
        assert len(mail.outbox) == 1
        assert "WON" in mail.outbox[0].body

    def test_loss_result_player2(self):
        m = self._make_complete_match()
        mail.outbox.clear()
        send_match_confirmation_email(m, m.side2_players.first())
        assert len(mail.outbox) == 1
        assert "LOST" in mail.outbox[0].body

    def test_score_order_player1_is_winner(self):
        m = self._make_complete_match()
        mail.outbox.clear()
        send_match_confirmation_email(m, m.side1_players.first())
        # Player1 won so score should be "3-0" (their score first)
        assert "3-0" in mail.outbox[0].body

    def test_score_order_player2_is_loser(self):
        m = self._make_complete_match()
        mail.outbox.clear()
        send_match_confirmation_email(m, m.side2_players.first())
        # Player2 lost 0-3 (their score first)
        assert "0-3" in mail.outbox[0].body

    def test_score_orientation_is_asymmetric_not_a_sweep(self):
        """3-0/0-3 would also pass if the sides were swapped consistently.
        Pin an uneven scoreline so orientation is unambiguous.
        """
        m = MatchFactory()
        GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=m, game_number=2, team1_score=7, team2_score=11)
        GameFactory(match=m, game_number=3, team1_score=11, team2_score=9)
        GameFactory(match=m, game_number=4, team1_score=11, team2_score=6)
        m.refresh_from_db()

        mail.outbox.clear()
        send_match_confirmation_email(m, m.side1_players.first())
        assert "3-1" in mail.outbox[0].body

        mail.outbox.clear()
        send_match_confirmation_email(m, m.side2_players.first())
        assert "1-3" in mail.outbox[0].body

    def test_score_orientation_for_doubles_side2_player(self):
        players = [PlayerFactory(with_user=True) for _ in range(4)]
        m = MatchFactory(
            team1_players=players[:2], team2_players=players[2:], is_double=True
        )
        for n in (1, 2, 3):
            GameFactory(match=m, game_number=n, team1_score=11, team2_score=4)
        m.refresh_from_db()

        mail.outbox.clear()
        send_match_confirmation_email(m, players[2])
        body = mail.outbox[0].body
        assert "0-3" in body
        assert "LOST" in body

        mail.outbox.clear()
        send_match_confirmation_email(m, players[0])
        body = mail.outbox[0].body
        assert "3-0" in body
        assert "WON" in body

    def test_non_participant_gets_no_email(self):
        m = self._make_complete_match()
        outsider = PlayerFactory(with_user=True)
        mail.outbox.clear()
        send_match_confirmation_email(m, outsider)
        assert mail.outbox == []

    def test_email_subject(self):
        m = self._make_complete_match()
        mail.outbox.clear()
        send_match_confirmation_email(m, m.side1_players.first())
        assert "Match Complete" in mail.outbox[0].subject

    def test_email_recipient(self):
        m = self._make_complete_match()
        mail.outbox.clear()
        send_match_confirmation_email(m, m.side1_players.first())
        assert mail.outbox[0].to == [m.side1_players.first().user.email]

    def test_url_construction_default(self):
        m = self._make_complete_match()
        mail.outbox.clear()
        send_match_confirmation_email(m, m.side1_players.first())
        assert f"/pingpong/matches/{m.pk}/" in mail.outbox[0].body

    @override_settings(SITE_PROTOCOL="https", SITE_DOMAIN="example.com")
    def test_url_construction_with_settings(self):
        m = self._make_complete_match()
        mail.outbox.clear()
        send_match_confirmation_email(m, m.side1_players.first())
        assert f"https://example.com/pingpong/matches/{m.pk}/" in mail.outbox[0].body


@pytest.mark.django_db
class TestSendScheduledMatchEmail:
    def test_email_sent(self):
        sm = ScheduledMatchFactory()
        mail.outbox.clear()
        send_scheduled_match_email(sm, sm.side1_players.first())
        assert len(mail.outbox) == 1
        assert sm.side1_players.first().user.email in mail.outbox[0].to

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
        send_scheduled_match_email(sm, sm.side1_players.first())
        assert "The Club" in mail.outbox[0].body

    def test_location_display_tbd(self):
        sm = ScheduledMatchFactory(location=None)
        mail.outbox.clear()
        send_scheduled_match_email(sm, sm.side1_players.first())
        assert "TBD" in mail.outbox[0].body

    def test_date_time_formatting(self):
        from datetime import date, time

        sm = ScheduledMatchFactory(
            scheduled_date=date(2025, 6, 15),
            scheduled_time=time(14, 30),
        )
        mail.outbox.clear()
        send_scheduled_match_email(sm, sm.side1_players.first())
        body = mail.outbox[0].body
        assert "June 15, 2025" in body or "Sunday, June 15, 2025" in body
        assert "02:30 PM" in body

    def test_email_subject_contains_date(self):
        sm = ScheduledMatchFactory()
        mail.outbox.clear()
        send_scheduled_match_email(sm, sm.side1_players.first())
        assert "Match Scheduled" in mail.outbox[0].subject


@pytest.mark.django_db
class TestScheduledMatchEmailSides:
    """The opponent was hard-coded to team2, and the HTML used team.name --
    blank for almost every team.
    """

    def test_side_two_player_is_told_the_right_opponent(self):
        p1 = PlayerFactory(with_user=True, name="Ada")
        p2 = PlayerFactory(with_user=True, name="Bob")
        sm = ScheduledMatchFactory(player1=p1, player2=p2)

        mail.outbox.clear()
        send_scheduled_match_email(sm, p2)
        body = mail.outbox[0].body
        assert "Opponent: Ada" in body
        assert "Opponent: Bob" not in body

    def test_side_one_player_is_told_the_right_opponent(self):
        p1 = PlayerFactory(with_user=True, name="Ada")
        p2 = PlayerFactory(with_user=True, name="Bob")
        sm = ScheduledMatchFactory(player1=p1, player2=p2)

        mail.outbox.clear()
        send_scheduled_match_email(sm, p1)
        assert "Opponent: Bob" in mail.outbox[0].body

    def test_html_names_both_sides_instead_of_blank_team_names(self):
        p1 = PlayerFactory(with_user=True, name="Ada")
        p2 = PlayerFactory(with_user=True, name="Bob")
        sm = ScheduledMatchFactory(player1=p1, player2=p2)

        mail.outbox.clear()
        send_scheduled_match_email(sm, p1)
        html = mail.outbox[0].alternatives[0][0]
        assert "<strong>You:</strong> Ada" in html
        assert "<strong>Opponent:</strong> Bob" in html
