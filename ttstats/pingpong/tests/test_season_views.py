"""The season archive views (KAN-51): frozen tables, read-only, club-scoped.

Seasons are always on (the `seasons` flag was retired by KAN-73)."""

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from pingpong.models import PlayerRating, RatingChange
from ttstats.middleware import REQUEST_LOCAL_ATTRS, _thread_locals

from .conftest import (
    ClubFactory,
    ClubMembershipFactory,
    PlayerFactory,
    PlayerRatingFactory,
    SeasonFactory,
    SeasonStandingFactory,
    UserFactory,
)

# override_settings cannot decorate a plain pytest class; fixtures can.
CLUBS_ON = pytest.mark.usefixtures("clubs_on")


@pytest.fixture
def clubs_on(settings):
    settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "clubs": True}


@pytest.fixture(autouse=True)
def _no_thread_local_leak():
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)
    yield
    for attr in REQUEST_LOCAL_ATTRS:
        if hasattr(_thread_locals, attr):
            delattr(_thread_locals, attr)


def _member(club):
    player = PlayerFactory(with_user=True, club=club)
    ClubMembershipFactory(club=club, user=player.user)
    client = Client()
    client.force_login(player.user)
    return client, player


def _standing(season, player, *, rank, rating, fmt="singles", name=None, wins=1, losses=0):
    return SeasonStandingFactory(
        season=season, format=fmt, player=player, player_name=name or player.name,
        rank=rank, rating=rating, wins=wins, losses=losses,
    )


def _archived(club, **kwargs):
    kwargs.setdefault("archived_at", timezone.now())
    return SeasonFactory(club=club, **kwargs)


def _detail(season, **params):
    return reverse("pingpong:season_archive_detail", args=[season.pk])


@pytest.mark.django_db
class TestLoginGate:
    def test_anonymous_is_sent_to_login(self):
        response = Client().get(reverse("pingpong:season_archive"))
        assert response.status_code == 302
        assert "login" in response["Location"]


@pytest.mark.django_db
class TestArchiveList:
    def test_lists_archived_seasons_with_their_champions(self):
        club = ClubFactory()
        client, me = _member(club)
        season = _archived(club, name="Spring Cup")
        _standing(season, me, rank=1, rating=1540, name="Champ Name")
        SeasonFactory(club=club, name="Still Running")

        body = client.get(reverse("pingpong:season_archive")).content.decode()

        assert "Spring Cup" in body
        assert "Champ Name" in body
        assert "Still Running" not in body

    def test_an_empty_archive_says_so(self):
        client, _ = _member(ClubFactory())
        body = client.get(reverse("pingpong:season_archive")).content.decode()
        assert "No season has ended yet" in body


@pytest.mark.django_db
class TestArchiveDetail:
    def test_renders_the_frozen_rows_in_rank_order_with_shared_ranks(self):
        club = ClubFactory()
        client, me = _member(club)
        season = _archived(club)
        a, b = PlayerFactory(club=club, name="Ann"), PlayerFactory(club=club, name="Bob")
        _standing(season, b, rank=3, rating=1480, wins=0, losses=2)
        _standing(season, a, rank=1, rating=1520)
        _standing(season, me, rank=1, rating=1520, name="Me Myself")

        response = client.get(_detail(season))

        rows = response.context["standings"]
        assert [(r.player_name, r.rank) for r in rows] == [("Ann", 1), ("Me Myself", 1), ("Bob", 3)]
        assert "0&ndash;2" in response.content.decode() or "0–2" in response.content.decode()

    def test_does_not_recompute_from_ratings(self):
        """The page reads only SeasonStanding: wiping every rating changes nothing."""
        club = ClubFactory()
        client, me = _member(club)
        season = _archived(club)
        _standing(season, me, rank=1, rating=1612)
        PlayerRatingFactory(
            player=me, club=club, season=season, format="singles",
            glicko_rating=1300, rd=60, volatility=0.06, last_rated_on=season.ends_on,
        )

        def table():
            body = client.get(_detail(season)).content.decode()
            return body[body.index("<ol"):body.index("</ol>")]

        before = table()
        RatingChange.objects.all().delete()
        PlayerRating.all_objects.all().delete()

        assert "1612" in before and "1300" not in before
        assert table() == before

    def test_a_deleted_player_still_reads_by_their_frozen_name(self):
        club = ClubFactory()
        client, _ = _member(club)
        season = _archived(club)
        gone = PlayerFactory(club=club, name="Gone Gina")
        _standing(season, gone, rank=1, rating=1550)
        gone.delete()

        response = client.get(_detail(season))

        assert response.status_code == 200
        assert "Gone Gina" in response.content.decode()

    def test_the_format_switch_shows_the_other_table(self):
        club = ClubFactory()
        client, me = _member(club)
        season = _archived(club)
        partner = PlayerFactory(club=club, name="Doubles Dana")
        _standing(season, me, rank=1, rating=1530)
        _standing(season, partner, rank=1, rating=1560, fmt="doubles")

        singles = client.get(_detail(season))
        doubles = client.get(_detail(season) + "?format=doubles")

        assert [r.format for r in singles.context["standings"]] == ["singles"]
        assert [r.player_name for r in doubles.context["standings"]] == ["Doubles Dana"]
        assert 'aria-current="page"' in doubles.content.decode()

    def test_an_unknown_format_falls_back_to_the_first_available(self):
        club = ClubFactory()
        client, me = _member(club)
        season = _archived(club)
        _standing(season, me, rank=1, rating=1560, fmt="doubles")

        response = client.get(_detail(season) + "?format=nonsense")

        assert response.context["current_format"] == "doubles"

    def test_a_season_with_no_matches_renders_an_empty_table(self):
        club = ClubFactory()
        client, _ = _member(club)
        body = client.get(_detail(_archived(club))).content.decode()
        assert "Nobody played this season" in body

    def test_an_unarchived_season_is_404(self):
        club = ClubFactory()
        client, _ = _member(club)
        assert client.get(_detail(SeasonFactory(club=club))).status_code == 404


@pytest.mark.django_db
@CLUBS_ON
class TestTenancy:
    def test_another_clubs_season_is_404(self):
        mine, theirs = ClubFactory(), ClubFactory()
        client, _ = _member(mine)
        assert client.get(_detail(_archived(theirs))).status_code == 404

    def test_the_list_shows_only_the_active_club(self):
        mine, theirs = ClubFactory(), ClubFactory()
        client, _ = _member(mine)
        _archived(mine, name="Ours")
        _archived(theirs, name="ZzTheirs")

        body = client.get(reverse("pingpong:season_archive")).content.decode()

        assert "Ours" in body and "ZzTheirs" not in body

    def test_a_user_with_no_club_sees_nothing(self):
        club = ClubFactory()
        season = _archived(club)
        lonely = UserFactory()
        PlayerFactory(user=lonely)
        client = Client()
        client.force_login(lonely)

        assert client.get(_detail(season)).status_code == 404
        assert season.name not in client.get(reverse("pingpong:season_archive")).content.decode()


@pytest.mark.django_db
class TestArchiveIsOutsideTheLeaderboardCache:
    def test_viewing_the_archive_writes_no_cache_entries(self):
        from django.core.cache import cache

        club = ClubFactory()
        client, me = _member(club)
        season = _archived(club)
        _standing(season, me, rank=1, rating=1500)
        client.get(_detail(season))  # warm whatever base.html caches
        cache.clear()

        with pytest.MonkeyPatch.context() as mp:
            writes = []
            original = cache.set
            mp.setattr(cache, "set", lambda key, *a, **k: (writes.append(key), original(key, *a, **k)))
            client.get(_detail(season))

        assert not [key for key in writes if "leaderboard" in str(key)]

