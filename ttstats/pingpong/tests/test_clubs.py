"""Club and ClubMembership (KAN-12 / S1.1).

This story is additive only: nothing reads the new columns yet. These tests pin
down the *shape* — the unique membership constraint, PROTECT on delete, and the
fact that a null club is still valid everywhere — so that S1.2's backfill and
S1.3's scoping have something to stand on.
"""
import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from pingpong.models import (
    Championship,
    Club,
    ClubMembership,
    Location,
    Match,
    Player,
    ScheduledMatch,
)

from .conftest import (
    ChampionshipFactory,
    ClubFactory,
    LocationFactory,
    MatchFactory,
    PlayerFactory,
    ScheduledMatchFactory,
    UserFactory,
)


@pytest.mark.django_db
class TestClub:
    def test_a_club_can_be_created_with_a_name(self):
        club = ClubFactory(name="Sunday Bashers")
        assert club.pk is not None
        assert str(club) == "Sunday Bashers"

    def test_slug_is_unique(self):
        ClubFactory(slug="bashers")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                ClubFactory(slug="bashers")

    def test_slug_is_derived_from_the_name_when_not_given(self):
        club = Club.objects.create(name="Sunday Bashers")
        assert club.slug == "sunday-bashers"

    def test_derived_slugs_do_not_collide(self):
        first = Club.objects.create(name="Sunday Bashers")
        second = Club.objects.create(name="Sunday Bashers")
        assert first.slug != second.slug

    def test_a_club_with_no_name_is_rejected(self):
        club = Club(name="")
        with pytest.raises(Exception):
            club.full_clean()


@pytest.mark.django_db
class TestClubMembership:
    def test_a_user_can_join_a_club(self):
        club = ClubFactory()
        user = UserFactory()
        membership = ClubMembership.objects.create(club=club, user=user)

        assert membership.pk is not None
        assert list(club.memberships.all()) == [membership]
        assert list(user.club_memberships.all()) == [membership]

    def test_the_same_user_cannot_join_the_same_club_twice(self):
        """A database-level constraint, not a form check: the invite flow in
        S1.6 races two acceptances of the same link."""
        club = ClubFactory()
        user = UserFactory()
        ClubMembership.objects.create(club=club, user=user)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                ClubMembership.objects.create(club=club, user=user)

    def test_a_user_can_belong_to_two_clubs(self):
        user = UserFactory()
        club_a, club_b = ClubFactory(), ClubFactory()
        ClubMembership.objects.create(club=club_a, user=user)
        ClubMembership.objects.create(club=club_b, user=user)

        assert user.club_memberships.count() == 2

    def test_deleting_a_user_removes_their_memberships(self):
        club = ClubFactory()
        user = UserFactory()
        ClubMembership.objects.create(club=club, user=user)

        user.delete()
        assert ClubMembership.objects.count() == 0

    def test_clubs_for_user_returns_the_users_clubs_only(self):
        user = UserFactory()
        mine, theirs = ClubFactory(), ClubFactory()
        ClubMembership.objects.create(club=mine, user=user)
        ClubMembership.objects.create(club=theirs, user=UserFactory())

        assert list(Club.objects.for_user(user)) == [mine]

    def test_clubs_for_user_is_empty_for_anonymous(self):
        from django.contrib.auth.models import AnonymousUser

        ClubFactory()
        assert list(Club.objects.for_user(AnonymousUser())) == []

    def test_clubs_for_user_is_empty_for_none(self):
        ClubFactory()
        assert list(Club.objects.for_user(None)) == []


@pytest.mark.django_db
class TestClubDeleteIsProtected:
    """`MatchParticipant.player` is CASCADE and it 500-ed production when a
    player was deleted. A club holds strictly more data than a player; deleting
    one must be refused, never quietly cascade."""

    def test_a_club_with_members_cannot_be_deleted(self):
        club = ClubFactory()
        ClubMembership.objects.create(club=club, user=UserFactory())

        with pytest.raises(ProtectedError):
            club.delete()

    def test_a_club_with_players_cannot_be_deleted(self):
        club = ClubFactory()
        PlayerFactory(club=club)

        with pytest.raises(ProtectedError):
            club.delete()

    def test_a_club_with_matches_cannot_be_deleted(self):
        club = ClubFactory()
        match = MatchFactory()
        Match.all_objects.filter(pk=match.pk).update(club=club)

        with pytest.raises(ProtectedError):
            club.delete()

    def test_an_empty_club_can_be_deleted(self):
        club = ClubFactory()
        club.delete()
        assert Club.objects.count() == 0


@pytest.mark.django_db
class TestClubForeignKeys:
    """Every entity that needs scoping carries a nullable, indexed club FK."""

    @pytest.mark.parametrize(
        "model,factory",
        [
            (Player, PlayerFactory),
            (Location, LocationFactory),
        ],
    )
    def test_club_defaults_to_null(self, model, factory):
        obj = factory()
        assert obj.club_id is None

    def test_match_club_defaults_to_null(self):
        assert MatchFactory().club_id is None

    def test_scheduled_match_club_defaults_to_null(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        assert ScheduledMatchFactory(player1=p1, player2=p2).club_id is None

    def test_championship_club_defaults_to_null(self):
        assert ChampionshipFactory().club_id is None

    def test_a_player_can_be_assigned_to_a_club(self):
        club = ClubFactory()
        player = PlayerFactory(club=club)
        player.refresh_from_db()
        assert player.club == club
        assert list(club.players.all()) == [player]

    def test_a_match_can_be_assigned_to_a_club(self):
        club = ClubFactory()
        match = MatchFactory()
        Match.all_objects.filter(pk=match.pk).update(club=club)
        match.refresh_from_db()
        assert match.club == club

    def test_a_match_may_span_two_clubs_at_the_database_level(self):
        """The UI will forbid this, but it must be *representable*, or a future
        data fix has nowhere to put the intermediate state."""
        club_a, club_b = ClubFactory(), ClubFactory()
        p1 = PlayerFactory(club=club_a)
        p2 = PlayerFactory(club=club_b)
        match = MatchFactory(player1=p1, player2=p2)
        Match.all_objects.filter(pk=match.pk).update(club=club_a)
        match.refresh_from_db()

        assert match.club == club_a
        assert {p.club for p in match.all_players} == {club_a, club_b}

    @pytest.mark.parametrize(
        "model", [Player, Match, ScheduledMatch, Championship, Location]
    )
    def test_club_column_is_indexed(self, model):
        """Every scoped query filters on it, so an unindexed column would be a
        full table scan per request."""
        field = model._meta.get_field("club")
        assert field.db_index is True
        assert field.null is True

    def test_game_has_no_club_column(self):
        """Deliberate: a game is always reachable via `match__club`, and a
        second denormalized copy is a second thing that can disagree."""
        from pingpong.models import Game

        with pytest.raises(Exception):
            Game._meta.get_field("club")


@pytest.mark.django_db
class TestClubIsInertUntilBackfill:
    """S1.1 must be deployable with zero user-visible change."""

    def test_a_player_with_a_null_club_still_appears_in_the_player_list(self):
        player = PlayerFactory()
        assert player in Player.objects.all()

    def test_a_match_with_a_null_club_is_still_visible_to_its_players(self):
        from ttstats.middleware import _thread_locals

        player = PlayerFactory(with_user=True)
        other = PlayerFactory()
        match = MatchFactory(player1=player, player2=other)

        _thread_locals.user = player.user
        try:
            assert match in Match.objects.all()
        finally:
            del _thread_locals.user
