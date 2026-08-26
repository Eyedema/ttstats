"""Club roles and permission enforcement (KAN-17 / S1.7).

The permission matrix is the deliverable, so it is parameterized rather than
prose: {owner, admin, member, non-member, staff} x {settings, role change,
remove}. A matrix written out by hand is a matrix with a hole in it.

Two rules carry the most weight, because both are unrecoverable without a shell:

* **A club can never be left ownerless.** The last owner cannot be demoted or
  removed, including by themselves.
* **You cannot act on someone who outranks you or is your equal.** An admin
  promoting themselves past the owner would make the owner role decorative.
"""
import pytest
from django.test import Client
from django.urls import reverse

from pingpong.models import Club, ClubMembership, ClubRole
from ttstats.middleware import REQUEST_LOCAL_ATTRS, _thread_locals

from .conftest import ClubFactory, ClubMembershipFactory, PlayerFactory, UserFactory

SETTINGS_URL = reverse("pingpong:club_settings")


@pytest.fixture(autouse=True)
def _clean_context():
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)
    yield
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)


@pytest.fixture(autouse=True)
def clubs_on(settings):
    settings.FEATURE_FLAGS = {"clubs": True, "tenancy_canary": False}


def _member(club, role, **kwargs):
    """A user with a Player, in `club`, holding `role`.

    The Player is not optional: `base.html` unconditionally renders
    `{% url 'pingpong:player_detail' user.player.pk %}`, so a logged-in user
    without one raises NoReverseMatch during template rendering.
    """
    user = UserFactory(**kwargs)
    PlayerFactory(user=user, club=club)
    if role is not None:
        ClubMembershipFactory(club=club, user=user, role=role)
    return user


def _client(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def club(db):
    return ClubFactory(name="Sunday Bashers")


@pytest.fixture
def owner(club):
    return _member(club, ClubRole.OWNER)


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

ROLES = {
    "owner": ClubRole.OWNER,
    "admin": ClubRole.ADMIN,
    "member": ClubRole.MEMBER,
    "non_member": None,
}

# Who may administer the club at all.
MAY_ADMINISTER = {"owner": True, "admin": True, "member": False, "non_member": False}

# What refusal looks like. A member of the club is *refused* (403); someone
# with no membership anywhere has no active club at all, so there is no club
# settings page for them to be refused from -- 404 is the honest answer, and it
# matches the app's existing "invisible objects 404, they do not 403" rule.
REFUSAL_STATUS = {"member": 403, "non_member": 404}


@pytest.mark.django_db
class TestPermissionMatrix:
    @pytest.mark.parametrize("role_name", sorted(ROLES))
    def test_club_settings_access(self, role_name, club, owner):
        user = _member(club, ROLES[role_name])
        response = _client(user).get(SETTINGS_URL)

        if MAY_ADMINISTER[role_name]:
            assert response.status_code == 200
        else:
            # Never a redirect: a forged request is refused, not bounced to a
            # page that looks like it worked.
            assert response.status_code == REFUSAL_STATUS[role_name]

    @pytest.mark.parametrize("role_name", sorted(ROLES))
    def test_role_change_access(self, role_name, club, owner):
        user = _member(club, ROLES[role_name])
        target = ClubMembershipFactory(
            club=club, user=UserFactory(), role=ClubRole.MEMBER
        )

        response = _client(user).post(
            reverse("pingpong:club_member_role", args=[target.pk]),
            {"role": ClubRole.ADMIN},
        )

        target.refresh_from_db()
        if MAY_ADMINISTER[role_name]:
            assert response.status_code == 302
            assert target.role == ClubRole.ADMIN
        else:
            assert response.status_code == REFUSAL_STATUS[role_name]
            assert target.role == ClubRole.MEMBER

    @pytest.mark.parametrize("role_name", sorted(ROLES))
    def test_member_removal_access(self, role_name, club, owner):
        user = _member(club, ROLES[role_name])
        target = ClubMembershipFactory(
            club=club, user=UserFactory(), role=ClubRole.MEMBER
        )

        response = _client(user).post(
            reverse("pingpong:club_member_remove", args=[target.pk])
        )

        if MAY_ADMINISTER[role_name]:
            assert response.status_code == 302
            assert not ClubMembership.objects.filter(pk=target.pk).exists()
        else:
            assert response.status_code == REFUSAL_STATUS[role_name]
            assert ClubMembership.objects.filter(pk=target.pk).exists()


@pytest.mark.django_db
class TestStaffGetNoSpecialStanding:
    """Per the S1.4 decision, applied consistently.

    `is_staff` is global; a club is not. A staff account that is not in this
    club has no business administering it, and one that *is* in the club is
    governed by its role like anyone else.
    """

    def test_staff_who_are_not_members_get_no_club(self, club, owner):
        staff = UserFactory(is_staff=True)
        PlayerFactory(user=staff)

        # No membership anywhere, so no active club at all -- 404, not 403.
        assert _client(staff).get(SETTINGS_URL).status_code == 404

    def test_staff_who_are_plain_members_are_refused(self, club, owner):
        staff = _member(club, ClubRole.MEMBER, is_staff=True)
        assert _client(staff).get(SETTINGS_URL).status_code == 403

    def test_a_superuser_who_is_a_plain_member_is_refused(self, club, owner):
        root = _member(club, ClubRole.MEMBER, is_staff=True, is_superuser=True)
        assert _client(root).get(SETTINGS_URL).status_code == 403

    def test_staff_who_are_club_admins_are_allowed(self, club, owner):
        staff = _member(club, ClubRole.ADMIN, is_staff=True)
        assert _client(staff).get(SETTINGS_URL).status_code == 200


@pytest.mark.django_db
class TestAClubIsNeverLeftOwnerless:
    """Both of these are unrecoverable without a shell."""

    def test_the_last_owner_cannot_demote_themselves(self, club, owner):
        membership = club.memberships.get(user=owner)

        response = _client(owner).post(
            reverse("pingpong:club_member_role", args=[membership.pk]),
            {"role": ClubRole.MEMBER},
            follow=True,
        )

        membership.refresh_from_db()
        assert membership.role == ClubRole.OWNER
        assert "only owner" in response.content.decode().lower()

    def test_the_last_owner_cannot_remove_themselves(self, club, owner):
        membership = club.memberships.get(user=owner)

        response = _client(owner).post(
            reverse("pingpong:club_member_remove", args=[membership.pk]),
            follow=True,
        )

        assert ClubMembership.objects.filter(pk=membership.pk).exists()
        assert "must always have an owner" in response.content.decode().lower()

    def test_an_owner_can_step_down_once_there_is_another(self, club, owner):
        second = _member(club, ClubRole.OWNER)
        membership = club.memberships.get(user=owner)

        _client(owner).post(
            reverse("pingpong:club_member_role", args=[membership.pk]),
            {"role": ClubRole.MEMBER},
        )

        membership.refresh_from_db()
        assert membership.role == ClubRole.MEMBER
        assert club.owner_count == 1


@pytest.mark.django_db
class TestRankingRules:
    def test_an_admin_cannot_change_the_owners_role(self, club, owner):
        admin = _member(club, ClubRole.ADMIN)
        owner_membership = club.memberships.get(user=owner)

        response = _client(admin).post(
            reverse("pingpong:club_member_role", args=[owner_membership.pk]),
            {"role": ClubRole.MEMBER},
            follow=True,
        )

        owner_membership.refresh_from_db()
        assert owner_membership.role == ClubRole.OWNER
        assert "cannot change the role" in response.content.decode().lower()

    def test_an_admin_cannot_remove_the_owner(self, club, owner):
        admin = _member(club, ClubRole.ADMIN)
        owner_membership = club.memberships.get(user=owner)

        _client(admin).post(
            reverse("pingpong:club_member_remove", args=[owner_membership.pk])
        )

        assert ClubMembership.objects.filter(pk=owner_membership.pk).exists()

    def test_an_admin_cannot_act_on_another_admin(self, club, owner):
        admin = _member(club, ClubRole.ADMIN)
        peer = _member(club, ClubRole.ADMIN)
        peer_membership = club.memberships.get(user=peer)

        _client(admin).post(
            reverse("pingpong:club_member_remove", args=[peer_membership.pk])
        )

        assert ClubMembership.objects.filter(pk=peer_membership.pk).exists()

    def test_an_admin_cannot_appoint_an_owner(self, club, owner):
        """Only an owner can hand out the role that outranks them."""
        admin = _member(club, ClubRole.ADMIN)
        target = ClubMembershipFactory(club=club, user=UserFactory())

        response = _client(admin).post(
            reverse("pingpong:club_member_role", args=[target.pk]),
            {"role": ClubRole.OWNER},
            follow=True,
        )

        target.refresh_from_db()
        assert target.role == ClubRole.MEMBER
        assert "only an owner" in response.content.decode().lower()

    def test_an_unknown_role_is_rejected(self, club, owner):
        target = ClubMembershipFactory(club=club, user=UserFactory())

        _client(owner).post(
            reverse("pingpong:club_member_role", args=[target.pk]),
            {"role": "superadmin"},
        )

        target.refresh_from_db()
        assert target.role == ClubRole.MEMBER


@pytest.mark.django_db
class TestControlsAreNotRenderedForPeopleWhoCannotUseThem:
    """A 403 on submit is the backstop. The template is the actual UX."""

    def test_an_admin_sees_no_controls_for_the_owner(self, club, owner):
        admin = _member(club, ClubRole.ADMIN)
        owner_membership = club.memberships.get(user=owner)

        body = _client(admin).get(SETTINGS_URL).content.decode()

        assert reverse(
            "pingpong:club_member_remove", args=[owner_membership.pk]
        ) not in body

    def test_an_admin_sees_controls_for_a_member(self, club, owner):
        admin = _member(club, ClubRole.ADMIN)
        target = ClubMembershipFactory(club=club, user=UserFactory())

        body = _client(admin).get(SETTINGS_URL).content.decode()

        assert reverse("pingpong:club_member_remove", args=[target.pk]) in body

    def test_nobody_sees_controls_for_the_last_owner(self, club, owner):
        owner_membership = club.memberships.get(user=owner)

        body = _client(owner).get(SETTINGS_URL).content.decode()

        assert reverse(
            "pingpong:club_member_remove", args=[owner_membership.pk]
        ) not in body


@pytest.mark.django_db
class TestCrossClubIsolationOfRoles:
    def test_a_role_is_per_membership_not_per_user(self, club):
        """Someone can run one club and merely play in another. Conflating the
        two is how a person ends up administering a club they visit."""
        other = ClubFactory(name="Other")
        user = UserFactory()
        PlayerFactory(user=user, club=club)
        ClubMembershipFactory(club=club, user=user, role=ClubRole.OWNER)
        ClubMembershipFactory(club=other, user=user, role=ClubRole.MEMBER)

        assert club.user_can_administer(user) is True
        assert other.user_can_administer(user) is False

    def test_an_owner_of_another_club_administers_only_their_own(self, club, owner):
        """The sharpest version of "non-member". Someone who runs Club B is an
        owner -- just not here. The settings page must show *their* club, and
        the roles in this one must be untouchable."""
        other = ClubFactory(name="Elsewhere")
        outsider = _member(other, ClubRole.OWNER)
        target = ClubMembershipFactory(club=club, user=UserFactory())

        response = _client(outsider).get(SETTINGS_URL)
        assert response.status_code == 200
        body = response.content.decode()
        assert "Elsewhere" in body
        assert club.name not in body

        assert _client(outsider).post(
            reverse("pingpong:club_member_remove", args=[target.pk])
        ).status_code == 404
        assert ClubMembership.objects.filter(pk=target.pk).exists()

    def test_an_admin_cannot_reach_another_clubs_membership_rows(self, club, owner):
        """The view looks the membership up through `self.club.memberships`, so
        a foreign id is a 404 rather than a cross-tenant write."""
        other = ClubFactory(name="Other")
        foreign = ClubMembershipFactory(club=other, user=UserFactory())

        response = _client(owner).post(
            reverse("pingpong:club_member_remove", args=[foreign.pk])
        )

        assert response.status_code == 404
        assert ClubMembership.objects.filter(pk=foreign.pk).exists()


@pytest.mark.django_db
class TestPermissionHelpers:
    """One shared helper, not an inline is_staff comparison copied per view."""

    def test_anonymous_is_never_a_member(self, club):
        from django.contrib.auth.models import AnonymousUser

        assert club.user_is_member(AnonymousUser()) is False
        assert club.user_can_administer(AnonymousUser()) is False

    def test_none_is_never_a_member(self, club):
        assert club.user_is_member(None) is False
        assert club.user_can_administer(None) is False

    def test_role_for_returns_none_for_a_non_member(self, club):
        assert club.role_for(UserFactory()) is None

    @pytest.mark.parametrize(
        "role,can_administer,is_owner",
        [
            (ClubRole.OWNER, True, True),
            (ClubRole.ADMIN, True, False),
            (ClubRole.MEMBER, False, False),
        ],
    )
    def test_helper_answers_per_role(self, club, role, can_administer, is_owner):
        user = _member(club, role)
        assert club.user_can_administer(user) is can_administer
        assert club.user_is_owner(user) is is_owner
        assert club.user_is_member(user) is True

    def test_rank_orders_the_roles(self):
        assert ClubRole.rank(ClubRole.OWNER) > ClubRole.rank(ClubRole.ADMIN)
        assert ClubRole.rank(ClubRole.ADMIN) > ClubRole.rank(ClubRole.MEMBER)
        assert ClubRole.rank(None) == 0

    def test_a_new_membership_defaults_to_member(self, club):
        """The least privilege that is useful. The backfill's `admin` grant is
        a migration artefact, not the policy for people who join later."""
        membership = ClubMembership.objects.create(club=club, user=UserFactory())
        assert membership.role == ClubRole.MEMBER


@pytest.mark.django_db
class TestTheBackfillPreservesExistingCapability:
    """Before roles existed every verified user could create and start a
    championship. Handing them the model default at cutover would silently
    strip that from everyone -- the regression S1.2 exists to prevent."""

    def test_existing_users_become_admins(self):
        from pingpong import club_backfill
        from pingpong.management.commands.backfill_clubs import command_models

        users = [UserFactory() for _ in range(3)]
        for user in users:
            PlayerFactory(user=user)

        club_backfill.run(command_models())

        roles = set(ClubMembership.objects.values_list("role", flat=True))
        assert roles <= {ClubRole.OWNER, ClubRole.ADMIN}
        assert ClubRole.MEMBER not in roles

    def test_exactly_one_owner_is_appointed(self):
        from pingpong import club_backfill
        from pingpong.management.commands.backfill_clubs import command_models

        for _ in range(3):
            PlayerFactory(user=UserFactory())

        club_backfill.run(command_models())

        default = Club.objects.get(slug=club_backfill.DEFAULT_CLUB_SLUG)
        assert default.owner_count == 1

    def test_a_superuser_is_preferred_as_owner(self):
        from pingpong import club_backfill
        from pingpong.management.commands.backfill_clubs import command_models

        first = UserFactory()
        PlayerFactory(user=first)
        root = UserFactory(is_staff=True, is_superuser=True)
        PlayerFactory(user=root)

        club_backfill.run(command_models())

        owner_membership = ClubMembership.objects.get(role=ClubRole.OWNER)
        assert owner_membership.user == root

    def test_owner_selection_is_deterministic_across_runs(self):
        from pingpong import club_backfill
        from pingpong.management.commands.backfill_clubs import command_models

        for _ in range(4):
            PlayerFactory(user=UserFactory())

        club_backfill.run(command_models())
        first_owner = ClubMembership.objects.get(role=ClubRole.OWNER).user_id

        club_backfill.reverse(command_models())
        club_backfill.run(command_models())

        assert ClubMembership.objects.get(role=ClubRole.OWNER).user_id == first_owner
