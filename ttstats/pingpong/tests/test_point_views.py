"""S2.5 / S2.6 / S2.7 -- the screens that read the point log.

pytest can assert that the context is right and the payload is well formed.
**It cannot say whether a chart appears on screen**; that is what
`tests/e2e/momentum.spec.js` is for. See the frontend-verification skill.
"""
import json

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from pingpong.views import MatchDetailView
from pingpong.models import Match, MatchPointSummary, PointEvent

from .conftest import MatchFactory, PlayerFactory
from .test_point_rollups import play_a_full_match
from .test_scoreboard import _point, _started_live_match


@pytest.mark.django_db
class TestMatchDetailMomentum:
    def test_a_live_scored_match_gets_the_momentum_context(self, auth_client):
        match, p1, p2 = play_a_full_match(auth_client)
        client = auth_client(p1.user)

        resp = client.get(reverse("pingpong:match_detail", args=[match.pk]))

        assert resp.status_code == 200
        assert resp.context["has_point_data"] is True
        assert resp.context["point_total"] == 33
        assert len(resp.context["point_timeline"]) == 3

    def test_a_match_not_scored_live_has_the_section_absent_entirely(self, auth_client):
        """Absent, not an empty chart and not a 'no data' box taking up space."""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        match = MatchFactory(player1=p1, player2=p2)
        client = auth_client(p1.user)

        resp = client.get(reverse("pingpong:match_detail", args=[match.pk]))

        assert resp.status_code == 200
        assert resp.context["has_point_data"] is False
        assert "point_chart_data" not in resp.context
        body = resp.content.decode()
        assert 'id="point-chart-data"' not in body
        assert "Momentum" not in body
        # And Chart.js is not shipped to a page with no chart on it.
        assert "chart.umd.js" not in body

    def test_the_json_script_payload_is_well_formed(self, auth_client):
        match, p1, _ = play_a_full_match(auth_client)
        client = auth_client(p1.user)

        resp = client.get(reverse("pingpong:match_detail", args=[match.pk]))
        body = resp.content.decode()

        start = body.index('id="point-chart-data"')
        start = body.index(">", start) + 1
        end = body.index("</script>", start)
        payload = json.loads(body[start:end])

        assert len(payload["games"]) == 3
        assert len(payload["colours"]) == 2
        assert payload["colours"][0] != payload["colours"][1]
        first = payload["games"][0]
        # Progression starts at the origin, so a chart has something to draw
        # from, and ends on the game's final score.
        assert first["side1"][0] == 0 and first["side2"][0] == 0
        assert (first["side1"][-1], first["side2"][-1]) == (11, 0)
        assert len(first["labels"]) == len(first["side1"]) == len(first["side2"])

    def test_a_comeback_is_stated_in_words_not_only_drawn(self, auth_client):
        match, client, p1, p2 = _started_live_match(auth_client, best_of=3)
        # Game 1: side one wins from 5-9 down.
        script = (["team1", "team2"] * 5) + ["team2"] * 4 + ["team1"] * 6
        script += ["team1"] * 11  # game 2, and the match
        for side in script:
            _point(client, match, side)
        match.refresh_from_db()

        resp = auth_client(p1.user).get(
            reverse("pingpong:match_detail", args=[match.pk])
        )

        first_game = resp.context["point_timeline"][0]
        assert first_game["deficit_overcome"] == 4
        assert "came back from 4 points down" in first_game["comeback_note"]
        assert first_game["comeback_note"] in resp.content.decode()

    def test_an_eleven_nil_game_has_no_lead_change_and_no_comeback(self, auth_client):
        match, p1, _ = play_a_full_match(auth_client)
        client = auth_client(p1.user)

        resp = client.get(reverse("pingpong:match_detail", args=[match.pk]))

        first = resp.context["point_timeline"][0]
        assert first["lead_changes"] == 0
        assert first["comeback_note"] is None
        assert first["longest_run"] == 11

    def test_an_abandoned_final_game_is_marked_unfinished(self, auth_client):
        """A match abandoned mid-game, then taken out of live mode by hand.

        A match still `is_live` is hidden from match detail by
        `LiveMatchManager`, so this state is only reachable once somebody has
        stopped the scoreboard -- which is exactly the recovery case the
        section has to survive. The chart reads the raw log, so the partial
        game renders and is labelled rather than silently dropped.
        """
        match, client, p1, _ = _started_live_match(auth_client, best_of=5)
        for _ in range(11):
            _point(client, match, "team1")
        for _ in range(3):
            _point(client, match, "team2")

        Match.all_objects.filter(pk=match.pk).update(is_live=False)

        resp = auth_client(p1.user).get(
            reverse("pingpong:match_detail", args=[match.pk])
        )

        assert resp.status_code == 200
        timeline = resp.context["point_timeline"]
        assert timeline[0]["complete"] is True
        assert timeline[1]["complete"] is False
        assert "unfinished" in resp.content.decode()

    def test_the_section_costs_exactly_one_query(self, auth_client):
        """One query, and the same one whether or not there is a log to read.

        The naive version of this section is a per-game query for the side
        labels -- which is what it did first, at eight extra queries on a
        three-game match. `_point_context` resolves both labels once from the
        memoized `sides()`.
        """
        match, p1, _ = play_a_full_match(auth_client)
        client = auth_client(p1.user)
        url = reverse("pingpong:match_detail", args=[match.pk])

        match.sides()  # the page has already paid for this by the time we run
        with CaptureQueriesContext(connection) as ctx:
            MatchDetailView._point_context(match)
        assert len(ctx.captured_queries) == 1

        def page_cost():
            client.get(url)  # warm anything the page caches
            with CaptureQueriesContext(connection) as inner:
                client.get(url)
            return len(inner.captured_queries)

        with_log = page_cost()
        PointEvent.objects.filter(match=match).delete()
        without_log = page_cost()

        # Having a point log costs nothing beyond that single read: no
        # per-game query, no N+1 on the side labels.
        assert with_log == without_log


@pytest.mark.django_db
class TestPlayerDetailPointStats:
    def test_a_player_with_live_scored_matches_gets_the_section(self, auth_client):
        match, p1, _ = play_a_full_match(auth_client)
        client = auth_client(p1.user)

        resp = client.get(reverse("pingpong:player_detail", args=[p1.pk]))

        assert resp.status_code == 200
        assert resp.context["has_point_stats"] is True
        body = resp.content.decode()
        assert "Serving &amp; receiving" in body

    def test_a_player_with_nothing_live_scored_has_the_section_absent(self, auth_client):
        player = PlayerFactory(with_user=True)
        client = auth_client(player.user)

        resp = client.get(reverse("pingpong:player_detail", args=[player.pk]))

        assert resp.status_code == 200
        assert resp.context["has_point_stats"] is False
        assert "Serving &amp; receiving" not in resp.content.decode()

    def test_every_rate_shows_the_counts_it_came_from(self, auth_client):
        """A number the reader can check, rather than one they must trust."""
        match, p1, _ = play_a_full_match(auth_client)
        resp = auth_client(p1.user).get(
            reverse("pingpong:player_detail", args=[p1.pk])
        )
        body = resp.content.decode()
        served = resp.context["point_stats"].serve

        assert served.total > 0
        assert f"{served.won}" in body
        assert f"{served.total}" in body
        assert "points served" in body
        assert "points received" in body

    def test_a_small_sample_is_marked_rather_than_withheld(self, auth_client):
        """Epic Open Question 2, decided: show it, caveat it, never hide it."""
        match, p1, _ = play_a_full_match(auth_client)
        resp = auth_client(p1.user).get(
            reverse("pingpong:player_detail", args=[p1.pk])
        )

        # One 3-game match is well under the threshold.
        assert resp.context["point_stats"].serve.total < 50
        assert resp.context["point_serve_confident"] is False
        body = resp.content.decode()
        assert "Small sample" in body
        # ...and the rate itself is still on the page.
        assert f"{round(resp.context['point_stats'].serve.percent)}" in body

    def test_a_large_sample_carries_no_caveat(self, auth_client):

        match, p1, _ = play_a_full_match(auth_client)
        MatchPointSummary.objects.filter(player=p1).update(
            serve_won=60, serve_total=100, receive_won=40, receive_total=100
        )
        cache.clear()

        resp = auth_client(p1.user).get(
            reverse("pingpong:player_detail", args=[p1.pk])
        )

        assert resp.context["point_serve_confident"] is True
        assert "Small sample" not in resp.content.decode()

    def test_the_section_costs_at_most_one_extra_query(self, auth_client):
        match, p1, _ = play_a_full_match(auth_client)
        client = auth_client(p1.user)
        url = reverse("pingpong:player_detail", args=[p1.pk])

        cache.clear()
        client.get(url)  # warm every cache the page uses, including this one
        with CaptureQueriesContext(connection) as ctx:
            client.get(url)
        warm = len(ctx.captured_queries)

        # Warm, the fold is served from cache and costs nothing at all.
        from pingpong import cache_utils
        assert cache.get(cache_utils.player_point_stats_key(p1)) is not None

        cache.delete(cache_utils.player_point_stats_key(p1))
        with CaptureQueriesContext(connection) as ctx:
            client.get(url)
        cold_fold = len(ctx.captured_queries)

        assert cold_fold - warm == 1

    def test_a_deleted_opponent_does_not_break_the_page(self, auth_client):
        """MatchParticipant.player cascades, so a side can end up empty."""
        match, p1, p2 = play_a_full_match(auth_client)
        p2.delete()
        cache.clear()

        resp = auth_client(p1.user).get(
            reverse("pingpong:player_detail", args=[p1.pk])
        )

        assert resp.status_code == 200

    def test_a_rate_of_exactly_zero_and_of_exactly_one_hundred(self, auth_client):

        match, p1, _ = play_a_full_match(auth_client)
        MatchPointSummary.objects.filter(player=p1).update(
            serve_won=0, serve_total=80, receive_won=80, receive_total=80
        )
        cache.clear()

        resp = auth_client(p1.user).get(
            reverse("pingpong:player_detail", args=[p1.pk])
        )

        assert resp.context["point_stats"].serve.percent == 0.0
        assert resp.context["point_stats"].receive.percent == 100.0
        assert resp.status_code == 200
