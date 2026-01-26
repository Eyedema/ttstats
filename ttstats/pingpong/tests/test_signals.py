import pytest
from django.core import mail

from pingpong.models import Match, Player, UserProfile
from .conftest import GameFactory, MatchFactory, PlayerFactory, UserFactory


@pytest.mark.django_db
class TestCreateUserProfileSignal:
    def test_profile_created_on_user_creation(self):
        user = UserFactory()
        assert hasattr(user, "profile")
        assert isinstance(user.profile, UserProfile)

    def test_verification_token_generated(self):
        user = UserFactory()
        assert user.profile.email_verification_token != ""
        assert len(user.profile.email_verification_token) == 32
        assert user.profile.email_verification_sent_at is not None


@pytest.mark.django_db
class TestTrackMatchWinnerChange:
    def test_new_match_winner_just_set_false(self):
        m = MatchFactory()
        assert getattr(m, "_winner_just_set", False) is False

    def test_winner_set_first_time(self):
        m = MatchFactory(best_of=3)
        # Add enough games to trigger winner
        GameFactory(match=m, game_number=1, player1_score=11, player2_score=5)
        GameFactory(match=m, game_number=2, player1_score=11, player2_score=9)
        m.refresh_from_db()
        assert m.winner is not None

    def test_winner_unchanged_no_retrigger(self):
        m = MatchFactory()
        m.player1.user.profile.email_verified = True
        m.player1.user.profile.save()
        m.player2.user.profile.email_verified = True
        m.player2.user.profile.save()
        mail.outbox.clear()

        # Set winner via games
        GameFactory(match=m, game_number=1, player1_score=11, player2_score=5)
        GameFactory(match=m, game_number=2, player1_score=11, player2_score=9)
        GameFactory(match=m, game_number=3, player1_score=11, player2_score=7)
        m.refresh_from_db()
        initial_email_count = len(mail.outbox)

        # Re-save without changing winner
        m.notes = "Updated"
        m.save()
        assert len(mail.outbox) == initial_email_count


@pytest.mark.django_db
class TestHandleMatchCompletion:
    def _make_verified_match(self):
        m = MatchFactory()
        m.player1.user.profile.email_verified = True
        m.player1.user.profile.save()
        m.player2.user.profile.email_verified = True
        m.player2.user.profile.save()
        mail.outbox.clear()
        return m

    def _make_unverified_match(self):
        m = MatchFactory()
        m.player1.user.profile.email_verified = False
        m.player1.user.profile.save()
        m.player2.user.profile.email_verified = False
        m.player2.user.profile.save()
        mail.outbox.clear()
        return m

    def _add_winning_games(self, m):
        GameFactory(match=m, game_number=1, player1_score=11, player2_score=5)
        GameFactory(match=m, game_number=2, player1_score=11, player2_score=9)
        GameFactory(match=m, game_number=3, player1_score=11, player2_score=7)

    def test_auto_confirm_for_unverified(self):
        m = self._make_unverified_match()
        self._add_winning_games(m)
        m.refresh_from_db()
        assert m.player1_confirmed is True
        assert m.player2_confirmed is True
        assert len(mail.outbox) == 0

    def test_emails_sent_for_verified(self):
        m = self._make_verified_match()
        self._add_winning_games(m)
        m.refresh_from_db()
        assert m.player1_confirmed is False
        assert m.player2_confirmed is False
        assert len(mail.outbox) == 2
        recipients = {e.to[0] for e in mail.outbox}
        assert m.player1.user.email in recipients
        assert m.player2.user.email in recipients

    def test_auto_confirm_mixed_verified(self):
        m = MatchFactory()
        m.player1.user.profile.email_verified = True
        m.player1.user.profile.save()
        m.player2.user.profile.email_verified = False
        m.player2.user.profile.save()
        mail.outbox.clear()

        self._add_winning_games(m)
        m.refresh_from_db()
        assert m.player1_confirmed is True
        assert m.player2_confirmed is True
        assert len(mail.outbox) == 0

    def test_no_action_without_winner(self):
        m = self._make_verified_match()
        m.notes = "Some notes"
        m.save()
        assert len(mail.outbox) == 0

    def test_no_double_emails(self):
        m = self._make_verified_match()
        self._add_winning_games(m)
        initial = len(mail.outbox)
        # Save again without changing winner
        m.refresh_from_db()
        m.notes = "Extra note"
        m.save()
        assert len(mail.outbox) == initial
