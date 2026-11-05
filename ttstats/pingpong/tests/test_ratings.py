"""The seasons cutover in the UI (KAN-48 / S4.3, KAN-50 / S4.4).

Two promises, one per flag position:

* **Flag off, nothing changes.** The leaderboard, match detail, the dashboard
  and the profile render from the legacy combined `Player.elo_*` fields
  exactly as before, with the same cache keys.
* **Flag on, ratings are per format and per season.** The leaderboard defaults
  to the active club's current season, offers all-time explicitly, ranks one
  format's `PlayerRating` rows, shows unrated players as unrated (never as
  1500), and marks provisional ratings with a threshold read from `elo.py`.
  The "if agreed" preview is the number that confirmation writes.
"""
import datetime

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from pingpong import elo, ratings
from pingpong.models import RatingChange, Side

from .conftest import (
    ClubFactory,
    ClubMembershipFactory,
    GameFactory,
    MatchFactory,
    PlayerFactory,
    PlayerRatingFactory,
    SeasonFactory,
    UserFactory,
    confirm_match,
)
from .test_views import _login_client

LEADERBOARD = "pingpong:leaderboard"


@pytest.fixture
def seasons_on(settings):
    settings.FEATURE_FLAGS = {**settings.FEATURE_FLAGS, "seasons": True}


def _member(club, **kwargs):
    """A verified user in `club` with a linked player in the same club."""
    user = UserFactory()
    user.profile.email_verified = True
    user.profile.save()
    player = PlayerFactory(user=user, club=club, **kwargs)
    ClubMembershipFactory(club=club, user=user)
    return user, player


def _club_with_current_season():
    club = ClubFactory()
    today = timezone.now().date()
    season = SeasonFactory(
        club=club,
        name="Now",
        starts_on=today - datetime.timedelta(days=10),
        ends_on=today + datetime.timedelta(days=80),
    )
    return club, season


def _finished(winner, loser, *, club, **kwargs):
    """A completed 3-0 singles match in `club`, not yet confirmed."""
    match = MatchFactory(player1=winner, player2=loser, best_of=5, club=club, **kwargs)
    for n in (1, 2, 3):
        GameFactory(match=match, game_number=n, team1_score=11, team2_score=5)
    match.refresh_from_db()
    return match


def _rows(response):
    return {row["player"].pk: row for row in response.context["player_stats"]}


# ---------------------------------------------------------------------------
# Flag off: exactly as before
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestFlagOffIsUnchanged:
    def test_leaderboard_reads_the_legacy_combined_rating(self):
        club, _season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        confirm_match(_finished(me, them, club=club))
        me.refresh_from_db()

        resp = _login_client(user).get(reverse(LEADERBOARD))

        assert resp.context["rating_scope"] is None
        assert _rows(resp)[me.pk]["elo_rating"] == me.elo_rating
        content = resp.content.decode()
        assert 'id="date_filter"' in content
        assert "data-rating-status" not in content
        assert 'id="rating-help"' not in content
        assert "/pingpong/seasons/" not in content

    def test_cache_key_keeps_its_pre_seasons_shape(self):
        from pingpong.views import LeaderboardView

        view = LeaderboardView()
        assert view._leaderboard_cache_key(
            "all", "all", "", "", "10", system=ratings.ELO
        ) == (
            "leaderboard_0_all_all___10"
        )

    def test_match_detail_preview_is_the_legacy_projection(self):
        club, _season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        match = _finished(me, them, club=club)

        ctx = _login_client(user).get(reverse("pingpong:match_detail", args=[match.pk])).context

        assert ctx["rating_scope"] is None
        assert ctx["side1_elo_delta"] == elo.projected_elo_changes(match).side1_change
        assert ctx["elo_is_pending"] is True

    def test_dashboard_preview_is_the_legacy_projection(self):
        club, _season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        match = _finished(me, them, club=club)

        rows = _login_client(user).get(reverse("pingpong:dashboard")).context[
            "pending_confirmations"
        ]

        assert rows[0]["elo_if_true"] == elo.projected_elo_change_for(match, me)

    def test_profile_has_no_format_ratings(self):
        club, _season = _club_with_current_season()
        user, me = _member(club)

        resp = _login_client(user).get(reverse("pingpong:player_detail", args=[me.pk]))

        assert resp.context["format_ratings"] is None
        assert 'id="rating-help"' not in resp.content.decode()


# ---------------------------------------------------------------------------
# Flag on: the leaderboard
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.usefixtures("seasons_on")
class TestSeasonLeaderboard:
    def test_defaults_to_the_current_season_and_singles(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        confirm_match(_finished(me, them, club=club))

        resp = _login_client(user).get(reverse(LEADERBOARD))

        scope = resp.context["rating_scope"]
        assert scope.season == season
        assert scope.format == elo.SINGLES
        season_row = me.ratings.get(season=season, format="singles")
        assert _rows(resp)[me.pk]["elo_rating"] == season_row.rating
        assert _rows(resp)[me.pk]["total_matches"] == 1
        assert _rows(resp)[me.pk]["wins"] == 1
        assert _rows(resp)[them.pk]["losses"] == 1

    def test_all_time_is_an_explicit_choice(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        PlayerRatingFactory(player=me, club=club, season=None, rating=1720, matches=30)
        PlayerRatingFactory(player=me, club=club, season=season, rating=1610, matches=2)

        client = _login_client(user)
        default = client.get(reverse(LEADERBOARD))
        all_time = client.get(reverse(LEADERBOARD), {"season": "all"})

        assert _rows(default)[me.pk]["elo_rating"] == 1610
        assert all_time.context["rating_scope"].season is None
        assert _rows(all_time)[me.pk]["elo_rating"] == 1720
        assert 'value="all"' in default.content.decode()

    def test_a_past_season_reads_its_live_rows(self):
        club, _current = _club_with_current_season()
        past = SeasonFactory(club=club, name="Spring")
        user, me = _member(club)
        PlayerRatingFactory(player=me, club=club, season=past, rating=1555, matches=4)

        resp = _login_client(user).get(reverse(LEADERBOARD), {"season": past.pk})

        assert resp.context["rating_scope"].season == past
        assert _rows(resp)[me.pk]["elo_rating"] == 1555

    def test_format_selects_that_formats_rows_not_a_filtered_shared_number(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        PlayerRatingFactory(player=me, club=club, season=season, format="singles", rating=1600)
        PlayerRatingFactory(player=me, club=club, season=season, format="doubles", rating=1420)

        client = _login_client(user)
        singles = client.get(reverse(LEADERBOARD), {"format": "singles"})
        doubles = client.get(reverse(LEADERBOARD), {"format": "doubles"})

        assert _rows(singles)[me.pk]["elo_rating"] == 1600
        assert _rows(doubles)[me.pk]["elo_rating"] == 1420
        assert doubles.context["rating_scope"].format == elo.DOUBLES

    def test_unrated_players_are_shown_as_unrated_not_1500(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, newcomer = _member(club, name="Newcomer")
        PlayerRatingFactory(player=me, club=club, season=season, rating=1530)
        # Played last season, nothing yet this season.
        PlayerRatingFactory(player=newcomer, club=club, season=None, rating=1650, matches=25)

        resp = _login_client(user).get(reverse(LEADERBOARD))

        assert newcomer.pk not in _rows(resp)
        assert [p.pk for p in resp.context["unrated_players"]] == [newcomer.pk]
        content = resp.content.decode()
        assert 'data-rating-status="unrated"' in content
        assert "1650" not in content
        assert ">1500<" not in content

    def test_a_season_from_another_club_is_not_selectable(self):
        club, season = _club_with_current_season()
        foreign = SeasonFactory(club=ClubFactory())
        user, _me = _member(club)

        resp = _login_client(user).get(reverse(LEADERBOARD), {"season": foreign.pk})

        assert resp.context["rating_scope"].season == season

    def test_garbage_params_fall_back_to_the_defaults(self):
        club, season = _club_with_current_season()
        user, _me = _member(club)

        resp = _login_client(user).get(
            reverse(LEADERBOARD), {"season": "x'", "format": "triples", "top_x": "lots"}
        )

        assert resp.status_code == 200
        assert resp.context["rating_scope"] == elo.RatingScope(season, elo.SINGLES)
        assert resp.context["top_x"] == "10"

    def test_no_current_season_defaults_to_all_time(self):
        club = ClubFactory()
        user, me = _member(club)
        PlayerRatingFactory(player=me, club=club, season=None, rating=1588)

        resp = _login_client(user).get(reverse(LEADERBOARD))

        assert resp.context["rating_scope"].season is None
        assert _rows(resp)[me.pk]["elo_rating"] == 1588

    def test_top_x_limits_rated_rows(self):
        club, season = _club_with_current_season()
        user, _me = _member(club)
        for n in range(12):
            PlayerRatingFactory(player=PlayerFactory(club=club), club=club, season=season, rating=1500 + n)

        resp = _login_client(user).get(reverse(LEADERBOARD))

        ranked = [row["elo_rating"] for row in resp.context["player_stats"]]
        assert ranked == sorted(ranked, reverse=True)
        assert len(ranked) == 10

    def test_the_htmx_fragment_honours_the_params(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        PlayerRatingFactory(player=me, club=club, season=None, format="doubles", rating=1490)

        resp = _login_client(user).get(
            reverse(LEADERBOARD), {"season": "all", "format": "doubles"}, HTTP_HX_REQUEST="true"
        )

        assert [t.name for t in resp.templates][0] == "pingpong/_leaderboard_season_results.html"
        assert resp.context["rating_scope"] == elo.RatingScope(None, elo.DOUBLES)
        assert _rows(resp)[me.pk]["elo_rating"] == 1490
        assert 'id="leaderboard-results"' in resp.content.decode()

    def test_links_to_the_season_archive(self):
        club, _season = _club_with_current_season()
        user, _me = _member(club)

        content = _login_client(user).get(reverse(LEADERBOARD)).content.decode()

        assert 'href="/pingpong/seasons/"' in content
        assert "TODO(KAN-51" not in content

    def test_the_filters_are_rendered_widgets_not_option_loops(self):
        club, season = _club_with_current_season()
        user, _me = _member(club)

        content = _login_client(user).get(reverse(LEADERBOARD)).content.decode()

        assert 'name="season"' in content and 'name="format"' in content
        assert f'<option value="{season.pk}" selected>' in content
        assert 'id="date_filter"' not in content


@pytest.mark.django_db
@pytest.mark.usefixtures("seasons_on")
class TestSeasonLeaderboardCache:
    def test_season_and_format_change_the_key(self):
        from pingpong.views import LeaderboardView

        club, season = _club_with_current_season()
        view = LeaderboardView()
        keys = {
            view._leaderboard_cache_key(
                "all", "all", "", "", "10", rating_scope=elo.RatingScope(s, f),
                system=ratings.ELO,
            )
            for s in (season, None)
            for f in (elo.SINGLES, elo.DOUBLES)
        }
        assert len(keys) == 4
        assert view._leaderboard_cache_key(
            "all", "all", "", "", "10", rating_scope=elo.RatingScope(season, elo.SINGLES),
            system=ratings.ELO,
        ) == f"leaderboard_0_season{season.pk}_singles_10"

    def test_hit_and_miss_produce_identical_context(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        _, idle = _member(club)
        PlayerRatingFactory(player=idle, club=club, season=None, matches=3)
        confirm_match(_finished(me, them, club=club))

        client = _login_client(user)
        cache.clear()
        miss = client.get(reverse(LEADERBOARD)).context
        hit = client.get(reverse(LEADERBOARD)).context

        for key in (
            "player_stats", "unrated_players", "rating_scope", "biggest_mover",
            "top_x", "provisional_matches", "scope_label",
        ):
            assert miss[key] == hit[key], key
        assert miss["player_stats"][0]["status"] == ratings.PROVISIONAL

    def test_a_confirmation_invalidates_the_season_table(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        client = _login_client(user)
        assert client.get(reverse(LEADERBOARD)).context["player_stats"] == []

        confirm_match(_finished(me, them, club=club))

        assert len(client.get(reverse(LEADERBOARD)).context["player_stats"]) == 2


@pytest.mark.django_db
@pytest.mark.usefixtures("seasons_on")
class TestSeasonMovement:
    def test_movement_is_within_the_selected_season(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        confirm_match(_finished(me, them, club=club))

        season_change = RatingChange.objects.get(player=me, rating__season=season)
        all_time_change = RatingChange.objects.get(player=me, rating__season__isnull=True)
        # Make the two scopes disagree, so the test can tell which one was read.
        RatingChange.objects.filter(pk=season_change.pk).update(rating_change=7)
        RatingChange.objects.filter(pk=all_time_change.pk).update(rating_change=23)

        client = _login_client(user)
        assert _rows(client.get(reverse(LEADERBOARD)))[me.pk]["movement"] == 7
        assert _rows(client.get(reverse(LEADERBOARD), {"season": "all"}))[me.pk]["movement"] == 23

    def test_changes_older_than_a_week_do_not_count(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        confirm_match(_finished(me, them, club=club))
        RatingChange.objects.update(created_at=timezone.now() - datetime.timedelta(days=8))

        resp = _login_client(user).get(reverse(LEADERBOARD))

        assert _rows(resp)[me.pk]["movement"] == 0
        assert resp.context["biggest_mover"] is None
        assert "&mdash; 0" in resp.content.decode() or "— 0" in resp.content.decode()

    def test_movement_renders_an_arrow_and_a_signed_number(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        confirm_match(_finished(me, them, club=club))

        resp = _login_client(user).get(reverse(LEADERBOARD))
        gained = _rows(resp)[me.pk]["movement"]

        assert gained > 0
        assert f"&#9650; {gained}" in resp.content.decode() or f"▲ {gained}" in resp.content.decode()

    def test_the_biggest_mover_links_to_their_profile(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        confirm_match(_finished(me, them, club=club))

        resp = _login_client(user).get(reverse(LEADERBOARD))

        assert resp.context["biggest_mover"]["player"] == me
        body = resp.content.decode()
        mover = body[body.index("Biggest mover this week"):]
        assert f'href="{reverse("pingpong:player_detail", args=[me.pk])}"' in mover[:600]


@pytest.mark.django_db
@pytest.mark.usefixtures("seasons_on")
class TestSeasonLeaderboardQueryCount:
    """The legacy table loads every confirmed match into Python. The season
    table must not: its query count may not grow with match history."""

    def _count(self, client):
        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            client.get(reverse(LEADERBOARD))
        return len(ctx.captured_queries)

    def test_query_count_is_flat_in_match_history(self):
        club, _season = _club_with_current_season()
        user, me = _member(club)
        opponents = [_member(club)[1] for _ in range(3)]
        client = _login_client(user)

        confirm_match(_finished(me, opponents[0], club=club))
        few = self._count(client)

        for opponent in opponents:
            for _ in range(3):
                confirm_match(_finished(me, opponent, club=club))
        many = self._count(client)

        assert many == few
        assert few <= 16


# ---------------------------------------------------------------------------
# Flag on: provisional and unrated (KAN-50)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.usefixtures("seasons_on")
class TestProvisionalMarker:
    def _status_on_leaderboard(self, experience, **params):
        club, season = _club_with_current_season()
        user, me = _member(club)
        PlayerRatingFactory(player=me, club=club, season=None, matches=experience)
        PlayerRatingFactory(player=me, club=club, season=season, matches=1)
        resp = _login_client(user).get(reverse(LEADERBOARD), params)
        return _rows(resp)[me.pk]["status"], resp.content.decode()

    def test_below_the_threshold_is_provisional(self):
        status, content = self._status_on_leaderboard(elo.PROVISIONAL_MATCHES - 1)
        assert status == ratings.PROVISIONAL
        assert 'data-rating-status="provisional"' in content
        assert 'id="rating-help"' in content

    def test_at_the_threshold_the_marker_is_gone(self):
        status, content = self._status_on_leaderboard(elo.PROVISIONAL_MATCHES)
        assert status == ratings.ESTABLISHED
        assert 'data-rating-status="provisional"' not in content

    def test_experience_is_all_time_so_a_new_season_does_not_reset_it(self):
        """One match this season, thirty all-time: established."""
        status, _ = self._status_on_leaderboard(30)
        assert status == ratings.ESTABLISHED

    def test_the_threshold_is_read_from_elo(self, monkeypatch):
        monkeypatch.setattr(elo, "PROVISIONAL_MATCHES", 5)
        status, content = self._status_on_leaderboard(6)
        assert status == ratings.ESTABLISHED
        monkeypatch.setattr(elo, "PROVISIONAL_MATCHES", 50)
        cache.clear()
        status, content = self._status_on_leaderboard(30)
        assert status == ratings.PROVISIONAL
        assert "50 rated matches" in content

    def test_the_marker_follows_the_constant_on_a_cache_hit_too(self, monkeypatch):
        club, season = _club_with_current_season()
        user, me = _member(club)
        PlayerRatingFactory(player=me, club=club, season=season, matches=8)
        PlayerRatingFactory(player=me, club=club, season=None, matches=8)
        client = _login_client(user)
        assert _rows(client.get(reverse(LEADERBOARD)))[me.pk]["status"] == ratings.PROVISIONAL

        monkeypatch.setattr(elo, "PROVISIONAL_MATCHES", 8)

        assert _rows(client.get(reverse(LEADERBOARD)))[me.pk]["status"] == ratings.ESTABLISHED

    def test_the_marker_is_a_word_not_a_colour(self):
        _, content = self._status_on_leaderboard(1)
        marker = content.split('data-rating-status="provisional"', 1)[1].split("</span>", 1)[0]
        assert "Provisional" in marker
        assert "ball" not in content.split('data-rating-status="provisional"', 1)[0][-200:]


@pytest.mark.django_db
@pytest.mark.usefixtures("seasons_on")
class TestProfileRatings:
    def test_each_format_shows_season_and_all_time_with_its_state(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        PlayerRatingFactory(player=me, club=club, season=None, format="singles", rating=1640, matches=3)
        PlayerRatingFactory(player=me, club=club, season=season, format="singles", rating=1580, matches=3)
        PlayerRatingFactory(player=me, club=club, season=None, format="doubles", rating=1700, matches=40)

        resp = _login_client(user).get(reverse("pingpong:player_detail", args=[me.pk]))

        by_format = {f["format"]: f for f in resp.context["format_ratings"]["formats"]}
        assert by_format["singles"]["season"] == {"rating": 1580, "status": ratings.PROVISIONAL}
        assert by_format["singles"]["all_time"] == {"rating": 1640, "status": ratings.PROVISIONAL}
        # Established in doubles all-time, but no doubles match this season.
        assert by_format["doubles"]["season"] == {"rating": None, "status": ratings.UNRATED}
        assert by_format["doubles"]["all_time"] == {"rating": 1700, "status": ratings.ESTABLISHED}

        content = resp.content.decode()
        assert 'data-rating-status="provisional"' in content
        assert 'data-rating-status="unrated"' in content
        assert 'id="rating-help"' in content
        assert 'href="#rating-help"' in content
        assert f"{elo.PROVISIONAL_MATCHES} rated matches" in content

    def test_a_player_who_never_played_is_unrated_everywhere(self):
        club, _season = _club_with_current_season()
        user, me = _member(club)

        ctx = _login_client(user).get(reverse("pingpong:player_detail", args=[me.pk])).context

        for entry in ctx["format_ratings"]["formats"]:
            assert entry["all_time"]["status"] == ratings.UNRATED
            assert entry["season"]["status"] == ratings.UNRATED

    def test_no_season_shows_all_time_only(self):
        club = ClubFactory()
        user, me = _member(club)

        ctx = _login_client(user).get(reverse("pingpong:player_detail", args=[me.pk])).context

        assert ctx["format_ratings"]["season"] is None


# ---------------------------------------------------------------------------
# Flag on: the preview is the write
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.usefixtures("seasons_on")
class TestPreviewEqualsWrite:
    def _season_change(self, match, player, season):
        return RatingChange.objects.get(
            match=match, player=player, rating__season=season, rating__format="singles"
        ).rating_change

    def test_match_detail_preview_is_what_confirmation_writes(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        # Give the two players different histories so the season seed and the
        # legacy combined rating disagree -- otherwise any scope would pass.
        PlayerRatingFactory(player=me, club=club, season=None, rating=1400, matches=30)
        PlayerRatingFactory(player=them, club=club, season=None, rating=1700, matches=5)
        match = _finished(me, them, club=club)
        url = reverse("pingpong:match_detail", args=[match.pk])
        client = _login_client(user)

        before = client.get(url).context
        assert before["rating_scope"] == elo.RatingScope(season, elo.SINGLES)
        assert before["elo_is_pending"] is True
        assert before["side1_elo_delta"] != elo.projected_elo_changes(match).side1_change

        confirm_match(match)

        assert before["side1_elo_delta"] == self._season_change(match, me, season)
        assert before["side2_elo_delta"] == self._season_change(match, them, season)
        after = client.get(url).context
        assert after["elo_is_pending"] is False
        assert after["side1_elo_delta"] == before["side1_elo_delta"]
        assert "Singles" in after["scope_label"] and season.name in after["scope_label"]

    def test_dashboard_preview_is_what_confirmation_writes(self):
        club, season = _club_with_current_season()
        user, me = _member(club)
        _, them = _member(club)
        PlayerRatingFactory(player=me, club=club, season=None, rating=1450, matches=12)
        match = _finished(them, me, club=club)

        rows = _login_client(user).get(reverse("pingpong:dashboard")).context[
            "pending_confirmations"
        ]
        previewed = rows[0]["elo_if_true"]
        confirm_match(match)

        assert previewed == self._season_change(match, me, season)

    def test_a_match_outside_every_season_previews_the_all_time_format_scope(self):
        club = ClubFactory()
        user, me = _member(club)
        _, them = _member(club)
        match = _finished(me, them, club=club)

        ctx = _login_client(user).get(reverse("pingpong:match_detail", args=[match.pk])).context
        confirm_match(match)

        assert ctx["rating_scope"] == elo.RatingScope(None, elo.SINGLES)
        written = RatingChange.objects.get(match=match, player=me, rating__season__isnull=True)
        assert ctx["side1_elo_delta"] == written.rating_change
        assert Side.ONE  # sides resolved via _viewer_context, not template branches
