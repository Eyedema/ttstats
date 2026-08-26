"""Club invite links (KAN-18 / S1.6).

This is the story that makes onboarding a second group self-serve, so the
integration test that matters is the whole anonymous round trip: admin
generates a link, a stranger opens it, signs up, and comes out a member.

The other thing under test is that a *dead* link fails clearly and safely.
A forwarded invite is the expected failure, not an exotic one -- so expired,
revoked, used-up and already-a-member each have their own path, and none of
them may 500 or silently grant membership.
"""
import pytest
from datetime import timedelta
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from pingpong.models import (
    ClubInvite,
    ClubMembership,
    ClubRole,
    Player,
)
from ttstats.middleware import ACTIVE_CLUB_SESSION_KEY, _thread_locals

from .conftest import ClubFactory, ClubMembershipFactory, PlayerFactory, UserFactory

SETTINGS_URL = reverse("pingpong:club_settings")


@pytest.fixture(autouse=True)
def _clean_context():
    for attr in ("user", "session", "club", "user_clubs", "flag_memo", "request_path"):
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)
    yield
    for attr in ("user", "session", "club", "user_clubs", "flag_memo", "request_path"):
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)


@pytest.fixture(autouse=True)
def clubs_on(settings):
    settings.FEATURE_FLAGS = {"clubs": True, "tenancy_canary": False}


# The accept endpoint's rate limit is left ENABLED for every test here rather
# than disabled and re-enabled for one. The autouse `_clear_cache` fixture
# resets the counter between tests, and no test below makes more than a couple
# of accept requests -- so leaving it on means these tests exercise the real
# production configuration instead of one assembled for their convenience.


def _member(club, role=ClubRole.MEMBER, **kwargs):
    user = UserFactory(**kwargs)
    PlayerFactory(user=user, club=club)
    if role is not None:
        ClubMembershipFactory(club=club, user=user, role=role)
    return user


def _client(user=None):
    client = Client()
    if user is not None:
        client.force_login(user)
    return client


@pytest.fixture
def club(db):
    return ClubFactory(name="Sunday Bashers")


@pytest.fixture
def admin(club):
    return _member(club, ClubRole.OWNER)


@pytest.fixture
def invite(club, admin):
    return ClubInvite.issue(club=club, created_by=admin, max_uses=1)


def _accept_url(invite_or_token):
    token = getattr(invite_or_token, "token", invite_or_token)
    return reverse("pingpong:club_invite_accept", args=[token])


# ---------------------------------------------------------------------------
# Generating and revoking
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGeneratingInvites:
    def test_an_admin_can_generate_a_link(self, club, admin):
        response = _client(admin).post(
            reverse("pingpong:club_invite_create"),
            {"valid_for_days": 14, "max_uses": 1, "note": "For Marco"},
            follow=True,
        )

        invite = ClubInvite.objects.get()
        assert invite.club == club
        assert invite.created_by == admin
        assert _accept_url(invite) in response.content.decode()

    def test_the_token_is_unguessable(self, invite):
        """uuid4 hex — 122 bits of CSPRNG output, the same shape as the
        email-verification token already in production."""
        assert len(invite.token) == 32
        assert invite.token != ClubInvite.issue(invite.club, None).token

    def test_a_plain_member_gets_403(self, club, admin):
        member = _member(club, ClubRole.MEMBER)

        response = _client(member).post(
            reverse("pingpong:club_invite_create"),
            {"valid_for_days": 14, "max_uses": 1},
        )

        assert response.status_code == 403
        assert not ClubInvite.objects.exists()

    def test_an_anonymous_visitor_cannot_generate_one(self, club):
        response = _client().post(
            reverse("pingpong:club_invite_create"), {"valid_for_days": 14}
        )
        assert response.status_code in (302, 403)
        assert not ClubInvite.objects.exists()

    def test_an_admin_can_revoke(self, invite, admin):
        _client(admin).post(reverse("pingpong:club_invite_revoke", args=[invite.pk]))

        invite.refresh_from_db()
        assert invite.is_revoked
        assert not invite.is_valid

    def test_an_admin_cannot_revoke_another_clubs_invite(self, admin):
        other = ClubFactory(name="Elsewhere")
        foreign = ClubInvite.issue(club=other, created_by=None)

        response = _client(admin).post(
            reverse("pingpong:club_invite_revoke", args=[foreign.pk])
        )

        foreign.refresh_from_db()
        assert response.status_code == 404
        assert not foreign.is_revoked

    def test_the_link_is_shown_once_and_not_kept_on_the_page(self, club, admin):
        """An invite link is a bearer credential. A settings page that always
        displays every live link is a settings page you cannot show anyone."""
        client = _client(admin)
        first = client.post(
            reverse("pingpong:club_invite_create"),
            {"valid_for_days": 14, "max_uses": 1},
            follow=True,
        )
        invite = ClubInvite.objects.get()
        assert _accept_url(invite) in first.content.decode()

        again = client.get(SETTINGS_URL)
        assert _accept_url(invite) not in again.content.decode()


# ---------------------------------------------------------------------------
# The happy paths
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAcceptingWhileLoggedIn:
    def test_an_authenticated_user_joins_immediately(self, club, invite):
        outsider = UserFactory()
        PlayerFactory(user=outsider)

        response = _client(outsider).get(_accept_url(invite), follow=True)

        assert club.user_is_member(outsider)
        assert response.redirect_chain[-1][0] == reverse("pingpong:dashboard")

    def test_they_land_in_the_new_club_not_their_old_one(self, club, invite):
        """"Land on the club's Today screen" means the *invited* club is
        active, otherwise the user joins and sees no difference."""
        previous = ClubFactory(name="Old Club")
        user = _member(previous)

        client = _client(user)
        client.get(_accept_url(invite))

        assert client.session[ACTIVE_CLUB_SESSION_KEY] == club.pk

    def test_they_join_as_a_plain_member(self, club, invite):
        outsider = UserFactory()
        PlayerFactory(user=outsider)

        _client(outsider).get(_accept_url(invite))

        assert club.role_for(outsider) == ClubRole.MEMBER

    def test_the_use_count_increments(self, invite):
        outsider = UserFactory()
        PlayerFactory(user=outsider)

        _client(outsider).get(_accept_url(invite))

        invite.refresh_from_db()
        assert invite.uses == 1

    def test_a_user_with_no_player_gets_one(self, club, invite):
        """base.html renders user.player.pk unconditionally, so a member with
        no Player 500s every page in the app. An invite must never leave
        someone in that state."""
        outsider = UserFactory(username="newcomer")
        Player.objects.filter(user=outsider).delete()

        response = _client(outsider).get(_accept_url(invite), follow=True)

        outsider.refresh_from_db()
        assert Player.objects.filter(user=outsider).exists()
        assert response.status_code == 200


@pytest.mark.django_db
class TestAlreadyAMember:
    def test_they_are_told_and_redirected(self, club, invite):
        existing = _member(club)

        response = _client(existing).get(_accept_url(invite), follow=True)

        assert "already a member" in response.content.decode().lower()
        assert response.redirect_chain[-1][0] == reverse("pingpong:dashboard")

    def test_no_duplicate_membership_row_is_created(self, club, invite):
        existing = _member(club)
        before = ClubMembership.objects.filter(club=club, user=existing).count()

        _client(existing).get(_accept_url(invite))

        assert ClubMembership.objects.filter(club=club, user=existing).count() == before

    def test_an_existing_members_visit_does_not_burn_a_use(self, club, invite):
        """Otherwise a curious member destroys a single-use link meant for
        someone else."""
        existing = _member(club)

        _client(existing).get(_accept_url(invite))

        invite.refresh_from_db()
        assert invite.uses == 0

    def test_an_existing_members_role_is_not_downgraded(self, club, invite):
        owner = club.memberships.filter(role=ClubRole.OWNER).first()

        _client(owner.user).get(_accept_url(invite))

        owner.refresh_from_db()
        assert owner.role == ClubRole.OWNER


# ---------------------------------------------------------------------------
# Dead links: never a 500, never silent membership
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInvalidInvites:
    def _visit(self, invite_or_token):
        user = UserFactory()
        PlayerFactory(user=user)
        return _client(user).get(_accept_url(invite_or_token)), user

    def test_an_expired_invite_is_refused(self, club, admin):
        invite = ClubInvite.issue(club, admin, validity=timedelta(days=1))
        ClubInvite.objects.filter(pk=invite.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        response, user = self._visit(invite)

        assert response.status_code == 410
        assert "expired" in response.content.decode().lower()
        assert not club.user_is_member(user)

    def test_a_revoked_invite_is_refused(self, club, invite):
        invite.revoke()

        response, user = self._visit(invite)

        assert response.status_code == 410
        assert "revoked" in response.content.decode().lower()
        assert not club.user_is_member(user)

    def test_a_used_up_invite_is_refused(self, club, invite):
        ClubInvite.objects.filter(pk=invite.pk).update(uses=1)

        response, user = self._visit(invite)

        assert response.status_code == 410
        assert "already been used" in response.content.decode().lower()
        assert not club.user_is_member(user)

    def test_an_unknown_token_is_a_404_not_a_500(self):
        response, user = self._visit("0" * 32)
        assert response.status_code == 404

    def test_an_absurd_token_does_not_raise(self):
        response, user = self._visit("x" * 200)
        assert response.status_code == 404

    def test_an_invite_for_a_deleted_club_does_not_500(self, club, admin):
        """`ClubInvite.club` is CASCADE, so deleting the club takes its invites
        with it and the link becomes an unknown token rather than a crash."""
        invite = ClubInvite.issue(club, admin)
        token = invite.token
        club.memberships.all().delete()
        Player.objects.filter(club=club).update(club=None)
        club.delete()

        response, user = self._visit(token)
        assert response.status_code == 404

    def test_an_unlimited_invite_never_becomes_used_up(self, club, admin):
        invite = ClubInvite.issue(club, admin, max_uses=None)

        for _ in range(3):
            user = UserFactory()
            PlayerFactory(user=user)
            _client(user).get(_accept_url(invite))

        invite.refresh_from_db()
        assert invite.uses == 3
        assert invite.is_valid


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestSingleUseIsRaceSafe:
    def test_only_one_of_two_simultaneous_claims_succeeds(self, club, admin):
        """`if invite.is_used_up:` in Python loses this race every time. The
        claim is a conditional UPDATE, which the database evaluates as one
        compare-and-set — the shape Match.result_notified_at already uses."""
        invite = ClubInvite.issue(club, admin, max_uses=1)

        first = invite.claim()
        second = ClubInvite.objects.get(pk=invite.pk).claim()

        assert first is True
        assert second is False

        invite.refresh_from_db()
        assert invite.uses == 1

    def test_stale_in_memory_copies_cannot_overshoot_the_cap(self, club, admin):
        """The realistic version of the race, without threads.

        Six requests each load the invite, all see `uses=0`, and all decide
        they may proceed. Under a Python-level check every one of them would
        succeed; under the conditional UPDATE the database admits exactly two.

        Deliberately not a thread test: SQLite serialises writers, so threads
        here would be testing the test harness rather than the claim.
        """
        invite = ClubInvite.issue(club, admin, max_uses=2)
        stale_copies = [ClubInvite.objects.get(pk=invite.pk) for _ in range(6)]

        assert all(copy.uses == 0 for copy in stale_copies)
        results = [copy.claim() for copy in stale_copies]

        invite.refresh_from_db()
        assert sum(results) == 2
        assert invite.uses == 2

    def test_a_revoked_invite_cannot_be_claimed(self, club, admin):
        invite = ClubInvite.issue(club, admin)
        invite.revoke()
        assert invite.claim() is False

    def test_an_expired_invite_cannot_be_claimed(self, club, admin):
        invite = ClubInvite.issue(club, admin)
        ClubInvite.objects.filter(pk=invite.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        assert ClubInvite.objects.get(pk=invite.pk).claim() is False


# ---------------------------------------------------------------------------
# The integration flow: anonymous -> signup -> member
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAnonymousSignupThroughAnInvite:
    """The required-flows convention in CLAUDE.md. This is the whole point of
    the story: a second group onboarding itself with no admin and no shell."""

    def test_the_full_round_trip(self, club, invite, mailoutbox):
        client = _client()

        # 1. A stranger opens the link and is sent to signup.
        landing = client.get(_accept_url(invite), follow=True)
        assert landing.redirect_chain[-1][0] == reverse("pingpong:signup")
        assert club.name in landing.content.decode()

        # 2. They register through the EXISTING registration view.
        client.post(
            reverse("pingpong:signup"),
            {
                "username": "newcomer",
                "email": "newcomer@example.com",
                "full_name": "New Comer",
                "playing_style": "normal",
                "password1": "a-very-long-passphrase-42",
                "password2": "a-very-long-passphrase-42",
            },
        )

        user = User.objects.get(username="newcomer")

        # 3. They come out a member, with a Player, in the right club.
        assert club.user_is_member(user)
        assert club.role_for(user) == ClubRole.MEMBER
        assert Player.objects.filter(user=user).exists()

        # 4. Registration's own behaviour is untouched -- not forked.
        assert len(mailoutbox) == 1
        assert "verify" in mailoutbox[0].subject.lower()

        # 5. And the invite was consumed exactly once.
        invite.refresh_from_db()
        assert invite.uses == 1

    def test_the_invite_survives_a_failed_validation_pass(self, club, invite):
        """A query parameter would be lost the moment the form posts back with
        an error. The session survives it, which is why the token is parked
        there rather than on the URL."""
        client = _client()
        client.get(_accept_url(invite))

        # Mismatched passwords -> form redisplays.
        client.post(
            reverse("pingpong:signup"),
            {
                "username": "newcomer",
                "email": "newcomer@example.com",
                "full_name": "New Comer",
                "playing_style": "normal",
                "password1": "a-very-long-passphrase-42",
                "password2": "different",
            },
        )
        assert not User.objects.filter(username="newcomer").exists()

        # Second, correct attempt.
        client.post(
            reverse("pingpong:signup"),
            {
                "username": "newcomer",
                "email": "newcomer@example.com",
                "full_name": "New Comer",
                "playing_style": "normal",
                "password1": "a-very-long-passphrase-42",
                "password2": "a-very-long-passphrase-42",
            },
        )

        user = User.objects.get(username="newcomer")
        assert club.user_is_member(user)

    def test_signup_without_an_invite_still_works(self, db):
        """The hook must be inert for everyone who did not arrive via a link."""
        client = _client()
        client.post(
            reverse("pingpong:signup"),
            {
                "username": "solo",
                "email": "solo@example.com",
                "full_name": "Solo Player",
                "playing_style": "normal",
                "password1": "a-very-long-passphrase-42",
                "password2": "a-very-long-passphrase-42",
            },
        )

        user = User.objects.get(username="solo")
        assert user.club_memberships.count() == 0

    def test_an_abandoned_signup_leaves_the_invite_unconsumed(self, club, invite):
        client = _client()
        client.get(_accept_url(invite))

        invite.refresh_from_db()
        assert invite.uses == 0
        assert invite.is_valid

    def test_a_link_that_dies_during_signup_does_not_grant_membership(
        self, club, invite
    ):
        client = _client()
        client.get(_accept_url(invite))

        invite.revoke()

        client.post(
            reverse("pingpong:signup"),
            {
                "username": "toolate",
                "email": "toolate@example.com",
                "full_name": "Too Late",
                "playing_style": "normal",
                "password1": "a-very-long-passphrase-42",
                "password2": "a-very-long-passphrase-42",
            },
        )

        user = User.objects.get(username="toolate")
        assert not club.user_is_member(user)


@pytest.mark.django_db
class TestMultiClub:
    def test_a_member_of_another_club_can_join_a_second(self, club, invite):
        """Per the S1.4 decision: multi-club from day one."""
        other = ClubFactory(name="Elsewhere")
        user = _member(other)

        _client(user).get(_accept_url(invite))

        assert club.user_is_member(user)
        assert other.user_is_member(user)

    def test_joining_does_not_move_their_existing_player_club(self, club, invite):
        other = ClubFactory(name="Elsewhere")
        user = _member(other)
        player = user.player

        _client(user).get(_accept_url(invite))

        player.refresh_from_db()
        assert player.club == other


@pytest.mark.django_db
class TestRateLimiting:
    def test_the_accept_endpoint_is_rate_limited(self, club, invite):
        """The accept endpoint is the only oracle for "is this token real", so
        it is the one place brute force has any purchase."""
        client = _client()

        statuses = [
            client.get(_accept_url("0" * 32)).status_code for _ in range(25)
        ]

        # 403, not 429: django-ratelimit's `Ratelimited` subclasses
        # `PermissionDenied`, which Django renders as 403. Worth pinning down
        # rather than assuming -- the first version of this test asserted 429
        # and passed nothing.
        assert 403 in statuses, "expected the rate limit to engage"
        assert statuses[0] == 404, "the first request should still be answered"

    def test_a_valid_invite_is_not_blocked_by_the_first_request(self, club, invite):
        """The limit exists to stop brute force, not to make one legitimate
        recipient's link fail."""
        user = UserFactory()
        PlayerFactory(user=user)

        response = _client(user).get(_accept_url(invite), follow=True)

        assert response.status_code == 200
        assert club.user_is_member(user)


@pytest.mark.django_db
class TestTokensAreNotLogged:
    def test_the_token_never_reaches_a_log_record(self, club, invite, caplog):
        """A token is a bearer credential. The request-context log filter logs
        `request.path`, and the token IS the path here — so this asserts the
        one place it could leak by accident."""
        import logging

        user = UserFactory()
        PlayerFactory(user=user)

        with caplog.at_level(logging.DEBUG):
            _client(user).get(_accept_url(invite))

        for record in caplog.records:
            assert invite.token not in record.getMessage()
