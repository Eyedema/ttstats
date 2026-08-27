"""New rows must land in the viewer's club (KAN-6, gap found during rollout).

The epic scoped every *read* and never wired the *writes*. With `clubs` on, a
row created through the UI got `club=NULL`, and the scoped managers filter on
`club=<active club>` -- which NULL never matches. So the thing you just created
was invisible the instant you created it.

That is precisely the "data appearing to vanish" failure the whole epic exists
to prevent, arriving through the one door nobody watched. The isolation suite
missed it because every fixture there assigns `club` explicitly; nothing ever
exercised a *creation path*.

These tests are written against the real views, not the models, because the
question is "does the thing I just made still exist for me" -- and only the
view layer answers that.
"""
import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from pingpong.models import Championship, Club, Location, Match, Player, ScheduledMatch
from ttstats.middleware import REQUEST_LOCAL_ATTRS, _thread_locals

from .conftest import ClubFactory, ClubMembershipFactory, PlayerFactory, UserFactory


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


@pytest.fixture
def club(db):
    return ClubFactory(name="Sunday Bashers")


@pytest.fixture
def member(club):
    user = UserFactory()
    PlayerFactory(user=user, name="Me", club=club)
    ClubMembershipFactory(club=club, user=user)
    return user


@pytest.fixture
def client_for(member):
    c = Client()
    c.force_login(member)
    return c


@pytest.mark.django_db
class TestThingsYouCreateStayVisible:
    """The user-facing statement of the bug."""

    def test_a_player_you_add_is_still_there_afterwards(self, club, member, client_for):
        client_for.post(
            reverse("pingpong:player_add"),
            {"name": "Newcomer", "nickname": "", "playing_style": "normal", "notes": ""},
        )

        created = Player.all_objects.get(name="Newcomer") if hasattr(
            Player, "all_objects"
        ) else Player.objects.get(name="Newcomer")
        assert created.club == club, "a new player landed outside the viewer's club"

        # And the list they land on actually shows them.
        body = client_for.get(reverse("pingpong:player_list")).content.decode()
        assert "Newcomer" in body

    def test_a_match_you_record_is_still_there_afterwards(
        self, club, member, client_for
    ):
        opponent = PlayerFactory(name="Rival", club=club)

        client_for.post(
            reverse("pingpong:match_add"),
            {
                "is_double": "False",
                "date_played": timezone.now().strftime("%Y-%m-%dT%H:%M"),
                "match_type": "casual",
                "best_of": 5,
                "notes": "",
                "player1": member.player.pk,
                "player2": opponent.pk,
            },
        )

        created = Match.all_objects.order_by("-pk").first()
        assert created is not None
        assert created.club == club, "a new match landed outside the viewer's club"

        # The one that matters: it is visible through the scoped manager.
        _thread_locals.user = member
        _thread_locals.club = club
        assert created in Match.objects.all(), "the match vanished on creation"

    def test_a_championship_you_create_is_still_there_afterwards(
        self, club, member, client_for
    ):
        response = client_for.post(
            reverse("pingpong:championship_create"),
            {
                "name": "Autumn Cup",
                "description": "",
                "championship_type": "singles",
                "is_public": "on",
                "max_participants": 8,
                "start_date": (
                    timezone.now() + timezone.timedelta(days=14)
                ).date().isoformat(),
                # Required for public championships, and must precede the start.
                "registration_deadline": (
                    timezone.now() + timezone.timedelta(days=7)
                ).date().isoformat(),
            },
        )

        created = Championship.all_objects.filter(name="Autumn Cup").first()
        assert created is not None, (
            "the form did not validate: "
            f"{getattr(response.context.get('form'), 'errors', None)}"
        )
        assert created.club == club

    def test_a_scheduled_match_you_book_is_still_there_afterwards(
        self, club, member, client_for
    ):
        opponent = PlayerFactory(name="Rival", club=club)
        tomorrow = (timezone.now() + timezone.timedelta(days=1)).date()

        client_for.post(
            reverse("pingpong:match_schedule"),
            {
                "scheduled_date": tomorrow.isoformat(),
                "scheduled_time": "18:00",
                "notes": "",
                "player1": member.player.pk,
                "player2": opponent.pk,
            },
        )

        created = ScheduledMatch.all_objects.order_by("-pk").first()
        assert created is not None
        assert created.club == club


@pytest.mark.django_db
class TestStampingRules:
    """Where the club comes from, and when it must NOT be applied."""

    def test_the_club_is_stamped_on_create(self, club, member):
        _thread_locals.user = member
        _thread_locals.club = club

        player = Player.objects.create(name="Stamped")

        assert player.club == club

    def test_an_explicit_club_always_wins(self, club, member):
        """Stamping fills a blank. It never overrules a caller who said what
        they meant -- the backfill and any future data fix depend on that."""
        other = ClubFactory(name="Elsewhere")
        _thread_locals.user = member
        _thread_locals.club = club

        player = Player.objects.create(name="Explicit", club=other)

        assert player.club == other

    def test_no_viewer_means_no_stamping(self, club):
        """A management command, a migration, the backfill. Same contract the
        managers keep: no user in context means do not interfere."""
        player = Player.objects.create(name="Headless")
        assert player.club is None

    def test_an_existing_row_is_not_restamped(self, club, member):
        """Only creation stamps. Re-saving a row must not silently move it
        between clubs -- that would make a cross-club fix impossible and turn
        any admin edit into a migration."""
        other = ClubFactory(name="Elsewhere")
        player = PlayerFactory(name="Settled", club=other)

        _thread_locals.user = member
        _thread_locals.club = club
        player.name = "Settled, renamed"
        player.save()

        player.refresh_from_db()
        assert player.club == other

    def test_a_null_club_on_an_existing_row_is_backfilled_on_save(
        self, club, member
    ):
        """The repair case: a row created before the cutover, saved by someone
        with a club. Filling the blank is strictly better than leaving it
        invisible, and `backfill_clubs` would do the same thing later."""
        player = PlayerFactory(name="Legacy", club=None)

        _thread_locals.user = member
        _thread_locals.club = club
        player.save()

        player.refresh_from_db()
        assert player.club == club

    def test_nothing_is_stamped_with_the_flag_off(self, club, member, settings):
        """Flag off must stay byte-identical to pre-tenancy."""
        settings.FEATURE_FLAGS = {"clubs": False}
        _thread_locals.user = member
        _thread_locals.club = club

        player = Player.objects.create(name="Unstamped")

        assert player.club is None

    @pytest.mark.parametrize(
        "model,kwargs",
        [
            (Player, {"name": "P"}),
            (Location, {"name": "Court"}),
            (Match, {"best_of": 5}),
            (
                Championship,
                {"name": "C", "start_date": "2026-01-01", "max_participants": 8},
            ),
        ],
    )
    def test_every_club_scoped_model_stamps(self, club, member, model, kwargs):
        _thread_locals.user = member
        _thread_locals.club = club

        manager = getattr(model, "all_objects", model.objects)
        obj = manager.create(**kwargs)

        assert obj.club == club, f"{model.__name__} did not stamp its club"
