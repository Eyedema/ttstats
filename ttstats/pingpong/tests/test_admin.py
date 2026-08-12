"""Smoke tests for the admin.

There was no test file for admin.py. Bad search_fields / list_filter lookups
raise only when the page is actually rendered, so they stay invisible until
someone opens the changelist in production. These render it.
"""
import pytest
from django.urls import reverse

from .conftest import (
    GameFactory,
    MatchFactory,
    PlayerFactory,
    ScheduledMatchFactory,
    UserFactory,
)


def _admin_client():
    from django.test import Client

    user = UserFactory(is_staff=True, is_superuser=True)
    PlayerFactory(user=user)
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
class TestMatchAdmin:
    def test_changelist_renders(self):
        m = MatchFactory()
        GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)

        resp = _admin_client().get(reverse("admin:pingpong_match_changelist"))
        assert resp.status_code == 200

    def test_search_finds_a_match_by_participant_name(self):
        target = PlayerFactory(with_user=True, name="Zenobia Searchable")
        other = PlayerFactory(with_user=True, name="Ordinary Bob")
        wanted = MatchFactory(player1=target, player2=other)
        unwanted = MatchFactory()

        resp = _admin_client().get(
            reverse("admin:pingpong_match_changelist"), {"q": "Zenobia"}
        )
        assert resp.status_code == 200
        pks = [obj.pk for obj in resp.context["cl"].result_list]
        assert wanted.pk in pks
        assert unwanted.pk not in pks

    def test_search_finds_a_side_two_participant(self):
        target = PlayerFactory(with_user=True, name="Quintus Findable")
        wanted = MatchFactory(player1=PlayerFactory(with_user=True), player2=target)

        resp = _admin_client().get(
            reverse("admin:pingpong_match_changelist"), {"q": "Quintus"}
        )
        assert [obj.pk for obj in resp.context["cl"].result_list] == [wanted.pk]

    def test_has_winner_filter(self):
        finished = MatchFactory()
        for n in (1, 2, 3):
            GameFactory(match=finished, game_number=n, team1_score=11, team2_score=5)
        unfinished = MatchFactory()

        client = _admin_client()
        url = reverse("admin:pingpong_match_changelist")

        complete = client.get(url, {"has_winner": "yes"})
        assert [o.pk for o in complete.context["cl"].result_list] == [finished.pk]

        in_progress = client.get(url, {"has_winner": "no"})
        assert unfinished.pk in [o.pk for o in in_progress.context["cl"].result_list]

    def test_change_form_renders(self):
        m = MatchFactory()
        resp = _admin_client().get(
            reverse("admin:pingpong_match_change", args=[m.pk])
        )
        assert resp.status_code == 200


@pytest.mark.django_db
class TestOtherAdminChangelists:
    @pytest.mark.parametrize(
        "url_name",
        [
            "admin:pingpong_player_changelist",
            "admin:pingpong_team_changelist",
            "admin:pingpong_game_changelist",
            "admin:pingpong_scheduledmatch_changelist",
            "admin:pingpong_location_changelist",
            "admin:pingpong_championship_changelist",
            "admin:pingpong_achievement_changelist",
        ],
    )
    def test_changelist_renders(self, url_name):
        m = MatchFactory()
        GameFactory(match=m, game_number=1, team1_score=11, team2_score=5)
        ScheduledMatchFactory()

        resp = _admin_client().get(reverse(url_name))
        assert resp.status_code == 200

    def test_scheduled_match_search_by_participant(self):
        target = PlayerFactory(with_user=True, name="Perpetua Scheduled")
        sm = ScheduledMatchFactory(
            player1=target, player2=PlayerFactory(with_user=True)
        )

        resp = _admin_client().get(
            reverse("admin:pingpong_scheduledmatch_changelist"), {"q": "Perpetua"}
        )
        assert [o.pk for o in resp.context["cl"].result_list] == [sm.pk]
