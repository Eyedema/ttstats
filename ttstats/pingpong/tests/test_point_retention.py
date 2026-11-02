"""T2.8 -- retention policy and growth reporting (KAN-35).

The policy is "keep everything", so there is no pruning job to test. What
these tests do pin is the claim the policy rests on: **the rollups are
sufficient**, so pruning could never break a page that reads them. If that
stops being true, pruning stops being an option -- silently -- and this is
where it gets noticed.
"""
import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.urls import reverse

from pingpong import point_rollups
from pingpong.models import MatchPointSummary, PointEvent

from .conftest import confirm_match
from .test_point_rollups import play_a_full_match


@pytest.mark.django_db
class TestRollupsRemainSufficientWithoutRawRows:
    """The property that makes pruning possible later, asserted now."""

    def test_a_players_career_figures_survive_losing_the_raw_log(self, auth_client):
        match, p1, _ = play_a_full_match(auth_client)
        before = point_rollups.player_point_stats(p1)
        assert before.serve.total > 0

        PointEvent.objects.filter(match=match).delete()
        cache.clear()

        after = point_rollups.player_point_stats(p1)
        assert after.serve == before.serve
        assert after.receive == before.receive
        assert after.points == before.points
        assert after.longest_run == before.longest_run

    def test_player_detail_still_renders_the_section(self, auth_client):
        match, p1, _ = play_a_full_match(auth_client)
        PointEvent.objects.filter(match=match).delete()
        cache.clear()

        resp = auth_client(p1.user).get(
            reverse("pingpong:player_detail", args=[p1.pk])
        )

        assert resp.status_code == 200
        assert resp.context["has_point_stats"] is True

    def test_head_to_head_still_renders_the_comparison(self, auth_client):
        match, p1, p2 = play_a_full_match(auth_client)
        confirm_match(match)
        PointEvent.objects.filter(match=match).delete()
        cache.clear()

        resp = auth_client(p1.user).get(
            reverse("pingpong:head_to_head"), {"player1": p1.pk, "player2": p2.pk}
        )

        assert resp.status_code == 200
        assert resp.context["has_point_comparison"] is True

    def test_match_detail_loses_its_chart_and_says_nothing_instead(self, auth_client):
        """The one thing pruning WOULD cost, and it degrades honestly."""
        match, p1, _ = play_a_full_match(auth_client)
        PointEvent.objects.filter(match=match).delete()
        cache.clear()

        resp = auth_client(p1.user).get(
            reverse("pingpong:match_detail", args=[match.pk])
        )

        assert resp.status_code == 200
        assert resp.context["has_point_data"] is False
        assert "point-momentum-chart" not in resp.content.decode()
        # And the rollups are untouched -- the career figures still stand.
        assert MatchPointSummary.objects.filter(match=match).count() == 2


@pytest.mark.django_db
class TestPointStorageReport:
    def test_it_reports_counts_and_the_threshold(self, auth_client, capsys):
        match, p1, _ = play_a_full_match(auth_client)

        call_command("point_storage")

        out = capsys.readouterr().out
        assert f"PointEvent rows:        {PointEvent.objects.count()}" in out
        assert "MatchPointSummary rows: 2" in out
        assert "Matches with a log:     1" in out
        assert "Points per match (avg): 33.0" in out
        assert "Revisit at:" in out
        assert "Within the retention threshold." in out

    def test_it_surfaces_a_crossed_threshold_rather_than_passing_silently(
        self, auth_client, capsys, monkeypatch
    ):
        play_a_full_match(auth_client)
        from pingpong.management.commands import point_storage

        monkeypatch.setattr(point_storage, "REVISIT_ROWS", 1)

        call_command("point_storage")

        out = capsys.readouterr().out
        assert "THRESHOLD CROSSED" in out
        assert "revisit the retention policy" in out

    def test_it_breaks_down_by_club(self, auth_client, capsys):
        match, p1, _ = play_a_full_match(auth_client)

        call_command("point_storage", by_club=True)

        out = capsys.readouterr().out
        assert "By club:" in out

    def test_an_empty_table_reports_cleanly(self, capsys):
        call_command("point_storage")

        out = capsys.readouterr().out
        assert "PointEvent rows:        0" in out
        assert "no rows yet" in out
        assert "Within the retention threshold." in out
