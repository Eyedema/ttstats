"""S2.4 -- point-statistic rollups and caching (KAN-30).

The rollups are a cache over `PointEvent`, and the tests below are mostly
about that claim holding: a rollup always equals what `point_stats` computes
from the raw log, it is rebuildable, and nothing that reads it ever runs the
naive query.
"""

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.urls import reverse

from pingpong import cache_utils, point_rollups, point_stats
from pingpong.models import Match, MatchPointSummary, PointEvent, Side

from .conftest import MatchFactory, PlayerFactory
from .test_scoreboard import _point, _started_live_match


def play_a_full_match(auth_client, best_of=3, script=None):
    """Score a whole match through the real endpoints and return it."""
    match, client, p1, p2 = _started_live_match(auth_client, best_of=best_of)
    if script is None:
        script = (["team1"] * 11) + (["team2"] * 11) + (["team1"] * 11)
    for side in script:
        _point(client, match, side)
    match.refresh_from_db()
    return match, p1, p2


@pytest.mark.django_db
class TestRollupsAreWrittenOnCompletion:
    def test_a_completed_live_match_has_rollups_matching_the_raw_log(self, auth_client):
        match, p1, p2 = play_a_full_match(auth_client)

        summaries = {s.player_id: s for s in MatchPointSummary.objects.filter(match=match)}
        assert set(summaries) == {p1.pk, p2.pk}

        raw = point_rollups.match_stats(match)
        for player, side in ((p1, Side.ONE), (p2, Side.TWO)):
            stored = point_rollups.side_stats_from_summary(summaries[player.pk])
            expected = raw.side(int(side))
            assert stored.serve == expected.serve
            assert stored.receive == expected.receive
            assert stored.points == expected.points
            assert stored.games == expected.games
            assert stored.deuce == expected.deuce
            assert stored.comebacks == expected.comebacks
            assert stored.longest_run == expected.longest_run

    def test_the_summary_records_the_side_each_player_was_on(self, auth_client):
        match, p1, p2 = play_a_full_match(auth_client)
        summaries = {s.player_id: s.side for s in MatchPointSummary.objects.filter(match=match)}
        assert summaries[p1.pk] == Side.ONE
        assert summaries[p2.pk] == Side.TWO

    def test_a_match_never_scored_live_gets_no_rollups(self, auth_client):
        match = MatchFactory()
        assert MatchPointSummary.objects.filter(match=match).count() == 0

        # And rebuilding removes rows rather than writing zeroes: absent and
        # zero mean different things to every consuming surface.
        assert point_rollups.rebuild_match_summaries(match) == 0
        assert MatchPointSummary.objects.filter(match=match).count() == 0

    def test_a_mid_match_state_has_no_rollups_yet(self, auth_client):
        match, client, _, _ = _started_live_match(auth_client)
        for _ in range(5):
            _point(client, match, "team1")

        assert PointEvent.objects.filter(match=match).count() == 5
        assert MatchPointSummary.objects.filter(match=match).count() == 0

    def test_a_rollup_failure_does_not_lose_the_match(self, auth_client, monkeypatch):
        """A missing rollup is rebuildable; a lost match is not."""
        def boom(match):
            raise RuntimeError("nope")

        monkeypatch.setattr(point_rollups, "rebuild_match_summaries", boom)

        match, p1, p2 = play_a_full_match(auth_client)

        assert match.winner_side == Side.ONE
        assert match.is_live is False
        assert MatchPointSummary.objects.filter(match=match).count() == 0


@pytest.mark.django_db
class TestRebuildAndConsistency:
    def test_rollups_are_rebuildable_from_the_raw_log(self, auth_client):
        match, p1, p2 = play_a_full_match(auth_client)
        before = {
            s.player_id: s.points_won
            for s in MatchPointSummary.objects.filter(match=match)
        }

        MatchPointSummary.objects.filter(match=match).delete()
        call_command("rebuild_point_rollups")

        after = {
            s.player_id: s.points_won
            for s in MatchPointSummary.objects.filter(match=match)
        }
        assert after == before

    def test_rebuild_is_idempotent(self, auth_client):
        match, _, _ = play_a_full_match(auth_client)
        call_command("rebuild_point_rollups")
        call_command("rebuild_point_rollups")
        assert MatchPointSummary.objects.filter(match=match).count() == 2

    def test_rebuild_can_be_restricted_to_one_match(self, auth_client):
        match_a, _, _ = play_a_full_match(auth_client)
        match_b, _, _ = play_a_full_match(auth_client)
        MatchPointSummary.objects.all().delete()

        call_command("rebuild_point_rollups", match=match_a.pk)

        assert MatchPointSummary.objects.filter(match=match_a).count() == 2
        assert MatchPointSummary.objects.filter(match=match_b).count() == 0

    def test_the_check_reports_drift_and_repairs_nothing(self, auth_client):
        match, p1, _ = play_a_full_match(auth_client)
        assert point_rollups.check_match(match) is None

        MatchPointSummary.objects.filter(match=match, player=p1).update(serve_won=999)

        problem = point_rollups.check_match(match)
        assert problem is not None
        assert problem["player"] == p1.pk
        assert "serve_won" in problem["fields"]
        # Reports, never repairs.
        assert (
            MatchPointSummary.objects.get(match=match, player=p1).serve_won == 999
        )

    def test_the_check_command_names_rollups_without_a_log(self, auth_client, capsys):
        match, _, _ = play_a_full_match(auth_client)
        PointEvent.objects.filter(match=match).delete()

        call_command("rebuild_point_rollups", check=True)

        out = capsys.readouterr().out
        assert "rollups but no point log" in out

    def test_the_check_command_is_quiet_when_everything_agrees(self, auth_client, capsys):
        play_a_full_match(auth_client)
        call_command("rebuild_point_rollups", check=True)
        assert "agree with the raw log" in capsys.readouterr().out


@pytest.mark.django_db
class TestUndoInvalidatesRollups:
    def test_an_undo_recomputes_an_existing_rollup(self, auth_client):
        """The log changed, so the summary of it must change too."""
        match, client, p1, _ = _started_live_match(auth_client, best_of=5)
        for _ in range(11):
            _point(client, match, "team1")
        for _ in range(5):
            _point(client, match, "team2")

        # Force a rollup into existence mid-match, as a rebuild would.
        point_rollups.rebuild_match_summaries(match)
        before = MatchPointSummary.objects.get(match=match, player=p1).points_total
        assert before == 16

        client.post(
            reverse("pingpong:live_undo", args=[match.pk]),
            data="{}", content_type="application/json",
        )

        after = MatchPointSummary.objects.get(match=match, player=p1).points_total
        assert after == 15

    def test_an_undo_with_no_rollup_present_writes_none(self, auth_client):
        match, client, _, _ = _started_live_match(auth_client)
        _point(client, match, "team1")

        client.post(
            reverse("pingpong:live_undo", args=[match.pk]),
            data="{}", content_type="application/json",
        )

        assert MatchPointSummary.objects.filter(match=match).count() == 0


@pytest.mark.django_db
class TestPlayerFoldAndCaching:
    def test_a_career_fold_equals_recomputing_from_every_match(self, auth_client):
        match_a, p1, p2 = play_a_full_match(auth_client)
        match_b, p3, p4 = play_a_full_match(auth_client)

        folded = point_rollups.player_point_stats(p1)
        expected = point_stats.combine_side_stats(
            [
                point_rollups.side_stats_from_summary(s)
                for s in MatchPointSummary.objects.filter(player=p1)
            ]
        )
        assert folded.serve == expected.serve
        assert folded.points == expected.points
        assert folded.longest_run == expected.longest_run

    def test_a_player_with_no_live_matches_gets_the_zero_state(self, auth_client):
        player = PlayerFactory()
        stats = point_rollups.player_point_stats(player)

        assert stats.has_sample is False
        assert stats.serve.rate == 0.0
        assert stats.points == point_stats.Rate(0, 0)

    def test_the_fold_costs_one_query_on_a_miss_and_none_on_a_hit(
        self, auth_client, django_assert_num_queries
    ):
        match, p1, _ = play_a_full_match(auth_client)
        cache.clear()

        with django_assert_num_queries(1):
            point_rollups.player_point_stats(p1)

        with django_assert_num_queries(0):
            point_rollups.player_point_stats(p1)

    def test_a_player_with_nothing_also_costs_one_query_then_none(
        self, auth_client, django_assert_num_queries
    ):
        player = PlayerFactory()
        cache.clear()

        with django_assert_num_queries(1):
            point_rollups.player_point_stats(player)
        with django_assert_num_queries(0):
            point_rollups.player_point_stats(player)

    def test_a_new_rollup_invalidates_the_players_cached_fold(self, auth_client):
        match, p1, _ = play_a_full_match(auth_client)
        first = point_rollups.player_point_stats(p1).points.total
        assert first > 0

        # A second match for the same player must show up immediately.
        match2, client, _, _ = _started_live_match(auth_client, best_of=3)
        Match.all_objects.filter(pk=match2.pk).update(club=p1.club)
        from pingpong import services
        services.set_match_sides(match2, [p1], list(match2.side2_players))
        for side in (["team1"] * 11) + (["team1"] * 11):
            _point(client, match2, side)

        second = point_rollups.player_point_stats(p1).points.total
        assert second > first

    def test_invalidate_match_caches_drops_the_point_fold(self, auth_client):
        match, p1, _ = play_a_full_match(auth_client)
        point_rollups.player_point_stats(p1)
        key = cache_utils.player_point_stats_key(p1)
        assert cache.get(key) is not None

        cache_utils.invalidate_match_caches(match)

        assert cache.get(key) is None

    def test_invalidate_player_caches_drops_the_point_fold(self, auth_client):
        match, p1, _ = play_a_full_match(auth_client)
        point_rollups.player_point_stats(p1)
        key = cache_utils.player_point_stats_key(p1)

        cache_utils.invalidate_player_caches(p1)

        assert cache.get(key) is None


@pytest.mark.django_db
class TestHeadToHeadFold:
    def test_stats_are_restricted_to_the_shared_matches(self, auth_client):
        """A player's global rate must never leak into a comparison."""
        shared, p1, p2 = play_a_full_match(auth_client)

        # p1 also plays somebody else, decisively.
        other, client, _, _ = _started_live_match(auth_client, best_of=3)
        from pingpong import services
        Match.all_objects.filter(pk=other.pk).update(club=p1.club)
        services.set_match_sides(other, [p1], list(other.side2_players))
        for _ in range(22):
            _point(client, other, "team1")

        career = point_rollups.player_point_stats(p1)
        h2h = point_rollups.head_to_head_point_stats(p1, p2, [shared.pk])

        assert career.points.total > h2h[p1.pk].points.total
        assert h2h[p1.pk].points.total == (
            MatchPointSummary.objects.get(match=shared, player=p1).points_total
        )

    def test_the_fold_costs_one_query_per_player_then_none(
        self, auth_client, django_assert_num_queries
    ):
        shared, p1, p2 = play_a_full_match(auth_client)
        cache.clear()

        with django_assert_num_queries(2):
            point_rollups.head_to_head_point_stats(p1, p2, [shared.pk])
        with django_assert_num_queries(0):
            point_rollups.head_to_head_point_stats(p1, p2, [shared.pk])

    def test_pairs_key_the_same_entry_in_either_order(self, auth_client):
        shared, p1, p2 = play_a_full_match(auth_client)
        assert cache_utils.h2h_point_stats_key(p1, p2) == cache_utils.h2h_point_stats_key(p2, p1)

    def test_players_with_no_shared_live_matches_get_the_zero_state(self, auth_client):
        p1 = PlayerFactory()
        p2 = PlayerFactory()

        h2h = point_rollups.head_to_head_point_stats(p1, p2, [])

        assert h2h[p1.pk].has_sample is False
        assert h2h[p2.pk].has_sample is False
