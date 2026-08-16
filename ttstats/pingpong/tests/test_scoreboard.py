"""Tests for the Live Scoreboard feature (KAN-4 epic)."""
import json

import pytest
from django.urls import reverse

from pingpong import live_scoring as ls
from pingpong.models import Game, Match, Side
from .conftest import (
    GameFactory,
    MatchFactory,
    PlayerFactory,
    confirm_match,
)


def _make_live_match(best_of=5):
    """Create a live match with a verified scorekeeper-player set as team1."""
    p1 = PlayerFactory(with_user=True)
    p2 = PlayerFactory(with_user=True)
    for p in (p1, p2):
        p.user.profile.email_verified = True
        p.user.profile.save()
    match = MatchFactory(
        player1=p1,
        player2=p2,
        best_of=best_of,
        is_live=True,
        scorekeeper=p1,
        live_state=ls.initial_state(best_of),
    )
    return match, p1, p2


# ---------------------------------------------------------------------------
# KAN-6: is_live + live_state + scorekeeper, exclude from stats
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLiveMatchExclusion:
    """Live matches must not bleed into leaderboards, Elo, stats."""

    def test_live_match_excluded_from_default_manager(self):
        finished = MatchFactory()
        live = MatchFactory(is_live=True)

        pks = set(Match.objects.values_list("pk", flat=True))

        assert finished.pk in pks
        assert live.pk not in pks

    def test_live_match_visible_via_all_objects(self):
        live = MatchFactory(is_live=True)
        assert Match.all_objects.filter(pk=live.pk).exists()

    def test_live_objects_returns_only_live_matches(self):
        live = MatchFactory(is_live=True)
        finished = MatchFactory()

        pks = set(Match.live_objects.values_list("pk", flat=True))

        assert live.pk in pks
        assert finished.pk not in pks

    def test_live_match_games_excluded_from_default_manager(self):
        live = MatchFactory(is_live=True)
        GameFactory(match=live, game_number=1, team1_score=11, team2_score=5)

        finished = MatchFactory()
        GameFactory(match=finished, game_number=1, team1_score=11, team2_score=5)

        match_pks = set(Game.objects.values_list("match_id", flat=True))

        assert finished.pk in match_pks
        assert live.pk not in match_pks

    def test_live_match_excluded_from_leaderboard_query(self):
        """The leaderboard view computes wins by querying Match.objects.

        A live match's "in-progress" winner (even if accidentally set) must
        never count toward a player's record.
        """
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)

        live = MatchFactory(player1=p1, player2=p2, is_live=True)
        live.winner_side = Side.ONE
        live.save()

        assert (
            Match.objects.filter(winner_side__isnull=False).count() == 0
        ), "live match must not count as a confirmed win"

    def test_live_match_excluded_from_head_to_head(self):
        """Head-to-head queries Match.objects between two players."""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)

        live = MatchFactory(player1=p1, player2=p2, is_live=True)

        h2h_qs = Match.objects.filter(participants__player=p1).filter(
            participants__player=p2
        )

        assert live.pk not in set(h2h_qs.values_list("pk", flat=True))

    def test_live_state_persists_arbitrary_json(self):
        match = MatchFactory(
            is_live=True,
            live_state={
                "best_of": 5,
                "team1_points": 7,
                "team2_points": 4,
                "current_server": "team1",
            },
        )
        match.refresh_from_db()

        assert match.live_state["team1_points"] == 7
        assert match.live_state["current_server"] == "team1"

    def test_scorekeeper_assignment(self):
        p1 = PlayerFactory(with_user=True)
        match = MatchFactory(player1=p1, is_live=True, scorekeeper=p1)

        match.refresh_from_db()
        assert match.scorekeeper == p1
        # Reverse query via live_objects (default manager hides is_live=True)
        assert Match.live_objects.filter(scorekeeper=p1).count() == 1

    def test_completing_live_match_makes_it_visible_and_updates_elo(self):
        """When is_live flips to False and a winning Game lands, the match
        flows through the existing confirmation+Elo pipeline."""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        for p in (p1, p2):
            p.user.profile.email_verified = True
            p.user.profile.save()

        match = MatchFactory(player1=p1, player2=p2, is_live=True, best_of=3)

        # Add 2 games during live mode. GameManager filters out is_live=True
        # matches' games, so match.games.count() inside Match.save() sees 0
        # and the winner stays None until is_live flips back to False.
        GameFactory(match=match, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match, game_number=2, team1_score=11, team2_score=7)

        match.refresh_from_db()
        assert match.winner_side is None
        assert Match.objects.filter(pk=match.pk).count() == 0  # hidden while live

        # Mimic the match-end handoff: flip is_live first, then save again so
        # Match.save()'s winner-detection sees the games.
        match.is_live = False
        match.live_state = None
        match.save()
        match.refresh_from_db()

        assert match.winner_side == 1
        assert Match.objects.filter(pk=match.pk).count() == 1

        confirm_match(match)
        match.refresh_from_db()
        assert match.is_confirmed is True


# ---------------------------------------------------------------------------
# KAN-8: Scoring rules (pure functions)
# ---------------------------------------------------------------------------


def _play(state, sides):
    """Apply a list of side strings, collecting completed games."""
    completed = []
    for s in sides:
        state, g = ls.apply_point(state, s)
        if g is not None:
            completed.append(g)
    return state, completed


def _started(best_of=5, initial_server="team1"):
    return ls.set_initial_server(ls.initial_state(best_of), initial_server)


class TestScoringRules:
    def test_games_to_win(self):
        assert ls.games_to_win(3) == 2
        assert ls.games_to_win(5) == 3
        assert ls.games_to_win(7) == 4

    def test_straight_11_0_wins_game(self):
        state, completed = _play(_started(), ["team1"] * 11)
        assert len(completed) == 1
        assert completed[0] == {
            "game_number": 1,
            "team1_score": 11,
            "team2_score": 0,
            "winner": "team1",
        }
        assert state["team1_games"] == 1
        assert state["team1_points"] == 0
        assert state["current_game_number"] == 2

    def test_game_must_be_won_by_two(self):
        # 11-10 is not a win
        sides = (["team1"] * 10) + (["team2"] * 10) + ["team1"]
        state, completed = _play(_started(), sides)
        assert completed == []
        assert state["team1_points"] == 11
        assert state["team2_points"] == 10

    def test_deuce_12_10(self):
        # 10-10 then team1 wins next two
        sides = (["team1"] * 10) + (["team2"] * 10) + ["team1", "team1"]
        state, completed = _play(_started(), sides)
        assert len(completed) == 1
        assert completed[0]["team1_score"] == 12
        assert completed[0]["team2_score"] == 10
        assert completed[0]["winner"] == "team1"

    def test_deuce_14_12(self):
        # 10-10, then alternate twice, then team2 wins two
        sides = (["team1"] * 10) + (["team2"] * 10)
        # 10-10 reached. Now alternate: T1 T2 T1 T2 → 12-12. Then T2 T2 → 12-14.
        sides += ["team1", "team2", "team1", "team2", "team2", "team2"]
        state, completed = _play(_started(), sides)
        assert len(completed) == 1
        assert completed[0]["team1_score"] == 12
        assert completed[0]["team2_score"] == 14
        assert completed[0]["winner"] == "team2"

    def test_match_complete_at_best_of_3_wins(self):
        state = _started(best_of=3)
        # team1 wins game 1 11-0
        state, _ = _play(state, ["team1"] * 11)
        assert not ls.is_match_complete(state)
        # team1 wins game 2 11-0 → match complete
        state, _ = _play(state, ["team1"] * 11)
        assert ls.is_match_complete(state)
        assert ls.match_winner(state) == "team1"
        # team1_games == 2 = games_to_win(3)
        assert state["team1_games"] == 2

    def test_cannot_score_after_match_complete(self):
        state = _started(best_of=3)
        state, _ = _play(state, ["team1"] * 11)
        state, _ = _play(state, ["team1"] * 11)
        with pytest.raises(ValueError):
            ls.apply_point(state, "team1")

    def test_cannot_score_before_initial_server_set(self):
        with pytest.raises(ValueError):
            ls.apply_point(ls.initial_state(5), "team1")

    def test_invalid_side_rejected(self):
        with pytest.raises(ValueError):
            ls.apply_point(_started(), "team3")  # type: ignore[arg-type]


class TestServerRotation:
    def test_initial_server_serves_first_two(self):
        state = _started(initial_server="team1")
        assert ls.current_server(state) == "team1"
        state, _ = _play(state, ["team1"])  # 1-0
        assert ls.current_server(state) == "team1"
        state, _ = _play(state, ["team2"])  # 1-1
        # 2 points played, rotation → team2 serves
        assert ls.current_server(state) == "team2"
        state, _ = _play(state, ["team1"])  # 2-1
        assert ls.current_server(state) == "team2"
        state, _ = _play(state, ["team2"])  # 2-2
        assert ls.current_server(state) == "team1"

    def test_server_alternates_between_games(self):
        # Game 1 initial server team1 → game 2 initial server team2
        state = _started(initial_server="team1")
        state, _ = _play(state, ["team1"] * 11)
        assert state["current_game_number"] == 2
        assert ls.current_server(state) == "team2"

    def test_server_rotates_every_point_at_deuce(self):
        state = _started(initial_server="team1")
        # 10-10 reached after 20 points → rotations=10, even → team1 serves
        state, _ = _play(state, (["team1"] * 10) + (["team2"] * 10))
        assert state["team1_points"] == 10 and state["team2_points"] == 10
        assert ls.current_server(state) == "team1"
        state, _ = _play(state, ["team1"])  # 11-10
        assert ls.current_server(state) == "team2"
        state, _ = _play(state, ["team2"])  # 11-11
        assert ls.current_server(state) == "team1"


class TestSideSwitchPrompt:
    def test_no_prompt_before_deciding_game(self):
        state = _started(best_of=5)
        state, _ = _play(state, ["team1"] * 5)
        assert not ls.should_prompt_side_switch(state)

    def test_prompt_once_in_deciding_game_at_5(self):
        # best_of=3, deciding game is #3 (we need 1-1 then game 3)
        state = _started(best_of=3)
        state, _ = _play(state, ["team1"] * 11)  # game 1
        state, _ = _play(state, ["team2"] * 11)  # game 2 → 1-1, game 3 starts
        assert state["current_game_number"] == 3
        assert not ls.should_prompt_side_switch(state)
        # Score up to 4-4 — still no prompt
        for _ in range(4):
            state, _ = _play(state, ["team1"])
            state, _ = _play(state, ["team2"])
        assert state["team1_points"] == 4 and state["team2_points"] == 4
        assert not ls.should_prompt_side_switch(state)
        # Hit 5 → prompt fires
        state, _ = _play(state, ["team1"])
        assert ls.should_prompt_side_switch(state)
        # After confirm, prompt stops
        state = ls.confirm_side_switch(state)
        assert not ls.should_prompt_side_switch(state)

    def test_prompt_does_not_fire_on_non_deciding_game(self):
        state = _started(best_of=5)
        # Score 5-0 in game 1 — not deciding
        state, _ = _play(state, ["team1"] * 5)
        assert not ls.should_prompt_side_switch(state)


class TestRandomFullMatch:
    def test_random_best_of_5_terminates_correctly(self):
        """Sanity: a randomised best-of-5 hits the match-end exactly when one
        side reaches 3 game wins."""
        import random
        rng = random.Random(42)
        state = _started(best_of=5)
        safety = 0
        while not ls.is_match_complete(state):
            side = rng.choice(["team1", "team2"])
            try:
                state, _ = ls.apply_point(state, side)
            except ValueError:
                break
            safety += 1
            assert safety < 1000, "match should complete in well under 1000 points"
        assert ls.is_match_complete(state)
        assert state["team1_games"] + state["team2_games"] <= 5
        assert max(state["team1_games"], state["team2_games"]) == 3


# ---------------------------------------------------------------------------
# KAN-8: HTTP endpoints (point / start / state)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLivePointEndpoint:
    def _post(self, client, url, body=None):
        return client.post(
            url,
            data=json.dumps(body or {}),
            content_type="application/json",
        )

    def test_non_scorekeeper_gets_403(self, auth_client):
        match, p1, p2 = _make_live_match()
        # Start the match first
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)

        other_client = auth_client(p2.user)
        resp = self._post(other_client, reverse("pingpong:live_point", args=[match.pk]),
                          {"side": "team1"})
        assert resp.status_code == 403

    def test_invalid_side_rejected(self, auth_client):
        match, p1, _ = _make_live_match()
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)

        client = auth_client(p1.user)
        resp = self._post(client, reverse("pingpong:live_point", args=[match.pk]),
                          {"side": "team3"})
        assert resp.status_code == 400

    def test_point_increments_state(self, auth_client):
        match, p1, _ = _make_live_match()
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)

        client = auth_client(p1.user)
        resp = self._post(client, reverse("pingpong:live_point", args=[match.pk]),
                          {"side": "team1"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"]["team1_points"] == 1
        assert data["state"]["team2_points"] == 0
        assert data["current_server"] == "team1"  # second of two serves
        assert data["is_match_complete"] is False
        assert data["redirect_url"] is None

    def test_game_end_creates_game_row_and_advances_state(self, auth_client):
        match, p1, _ = _make_live_match(best_of=5)
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)

        client = auth_client(p1.user)
        url = reverse("pingpong:live_point", args=[match.pk])
        for _ in range(11):
            resp = self._post(client, url, {"side": "team1"})
            assert resp.status_code == 200

        data = resp.json()
        assert data["state"]["team1_games"] == 1
        assert data["state"]["team1_points"] == 0
        assert data["state"]["current_game_number"] == 2
        assert data["is_match_complete"] is False
        # A Game row should now exist
        assert Game.all_objects.filter(match=match, game_number=1).count() == 1
        game = Game.all_objects.get(match=match, game_number=1)
        assert game.team1_score == 11
        assert game.team2_score == 0

    def test_match_end_flips_is_live_and_redirects(self, auth_client):
        match, p1, p2 = _make_live_match(best_of=3)
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)

        client = auth_client(p1.user)
        url = reverse("pingpong:live_point", args=[match.pk])
        # team1 wins games 1 and 2 → match end
        for _ in range(22):
            resp = self._post(client, url, {"side": "team1"})
            assert resp.status_code == 200

        data = resp.json()
        assert data["is_match_complete"] is True
        # KAN-12: redirect to match_confirm handoff
        assert data["redirect_url"] == reverse(
            "pingpong:match_confirm", args=[match.pk]
        )

        match.refresh_from_db()
        assert match.is_live is False
        assert match.live_state is None
        # Winner was set via Game.save() → Match.save() pipeline
        assert match.winner_side == Side.ONE
        # 2 Game rows persisted
        assert Game.all_objects.filter(match=match).count() == 2

    def test_cannot_score_before_start(self, auth_client):
        match, p1, _ = _make_live_match()
        # live_state.started is False — no initial server picked yet
        client = auth_client(p1.user)
        resp = self._post(client, reverse("pingpong:live_point", args=[match.pk]),
                          {"side": "team1"})
        assert resp.status_code == 409

    def test_match_not_live_returns_409(self, auth_client):
        match, p1, _ = _make_live_match()
        Match.all_objects.filter(pk=match.pk).update(is_live=False)
        client = auth_client(p1.user)
        resp = self._post(client, reverse("pingpong:live_point", args=[match.pk]),
                          {"side": "team1"})
        assert resp.status_code == 409


@pytest.mark.django_db
class TestLiveStartEndpoint:
    def test_start_sets_initial_server(self, auth_client):
        match, p1, _ = _make_live_match()
        client = auth_client(p1.user)
        resp = client.post(
            reverse("pingpong:live_start", args=[match.pk]),
            data=json.dumps({"initial_server": "team2"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"]["initial_server"] == "team2"
        assert data["state"]["started"] is True

    def test_start_rejects_invalid_server(self, auth_client):
        match, p1, _ = _make_live_match()
        client = auth_client(p1.user)
        resp = client.post(
            reverse("pingpong:live_start", args=[match.pk]),
            data=json.dumps({"initial_server": "neither"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_start_is_idempotent_with_same_server(self, auth_client):
        match, p1, _ = _make_live_match()
        client = auth_client(p1.user)
        url = reverse("pingpong:live_start", args=[match.pk])
        body = json.dumps({"initial_server": "team1"})
        r1 = client.post(url, data=body, content_type="application/json")
        r2 = client.post(url, data=body, content_type="application/json")
        assert r1.status_code == 200 and r2.status_code == 200

    def test_start_rejects_change_after_started(self, auth_client):
        match, p1, _ = _make_live_match()
        client = auth_client(p1.user)
        url = reverse("pingpong:live_start", args=[match.pk])
        r1 = client.post(url, data=json.dumps({"initial_server": "team1"}),
                         content_type="application/json")
        r2 = client.post(url, data=json.dumps({"initial_server": "team2"}),
                         content_type="application/json")
        assert r1.status_code == 200
        assert r2.status_code == 409


@pytest.mark.django_db
class TestLiveStateEndpoint:
    """Resync endpoint, called on visibilitychange by scoreboard.html.

    It was fully wired -- view, URL, context var, data-state-url attribute
    and these tests -- but had no caller until B.5e.
    """

    def test_page_exposes_the_url_to_the_client(self, auth_client):
        match, p1, _ = _make_live_match()
        resp = auth_client(p1.user).get(
            reverse("pingpong:live_scoreboard", args=[match.pk])
        )
        body = resp.content.decode()
        assert reverse("pingpong:live_state", args=[match.pk]) in body
        # The handler that consumes it must exist, or the attribute is inert
        # again -- which is exactly the state this endpoint was in.
        assert "refreshState" in body

    def test_returns_canonical_state(self, auth_client):
        match, p1, _ = _make_live_match()
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)
        client = auth_client(p1.user)
        resp = client.get(reverse("pingpong:live_state", args=[match.pk]))
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"]["initial_server"] == "team1"
        assert data["current_server"] == "team1"

    def test_completed_match_returns_redirect_url(self, auth_client):
        match, p1, _ = _make_live_match()
        Match.all_objects.filter(pk=match.pk).update(
            is_live=False, live_state=None
        )
        client = auth_client(p1.user)
        resp = client.get(reverse("pingpong:live_state", args=[match.pk]))
        assert resp.status_code == 200
        assert resp.json()["redirect_url"] is not None

    def test_non_scorekeeper_rejected(self, auth_client):
        match, _, p2 = _make_live_match()
        client = auth_client(p2.user)
        resp = client.get(reverse("pingpong:live_state", args=[match.pk]))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# KAN-7: "Score live" button on match-create form
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestScoreLiveLaunch:
    def _base_form_data(self, p1, p2, best_of=5):
        return {
            "player1": p1.pk,
            "player2": p2.pk,
            "player3": "",
            "player4": "",
            "is_double": "False",
            "date_played": "2026-05-17T12:00",
            "location": "",
            "match_type": "casual",
            "best_of": best_of,
            "notes": "",
        }

    def test_score_live_creates_live_match_and_redirects_to_scoreboard(self, auth_client):
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        client = auth_client(p1.user)

        data = self._base_form_data(p1, p2, best_of=5)
        data["start_live"] = "1"
        resp = client.post(reverse("pingpong:match_add"), data=data)

        assert resp.status_code == 302
        match = Match.all_objects.latest("pk")
        assert match.is_live is True
        assert match.scorekeeper == p1
        assert match.best_of == 5
        assert match.live_state == ls.initial_state(5)
        assert resp.url == reverse("pingpong:live_scoreboard", args=[match.pk])

    def test_regular_submit_creates_normal_match(self, auth_client):
        """No start_live flag → existing behavior preserved (backwards-compat)."""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        client = auth_client(p1.user)

        data = self._base_form_data(p1, p2)
        resp = client.post(reverse("pingpong:match_add"), data=data)

        assert resp.status_code == 302
        match = Match.all_objects.latest("pk")
        assert match.is_live is False
        assert match.scorekeeper is None
        assert match.live_state is None
        assert resp.url == reverse("pingpong:match_detail", args=[match.pk])

    def test_score_live_button_visible_on_form(self, auth_client):
        p1 = PlayerFactory(with_user=True)
        client = auth_client(p1.user)
        resp = client.get(reverse("pingpong:match_add"))
        assert resp.status_code == 200
        assert b'name="start_live"' in resp.content
        assert b"Score live" in resp.content

    def test_score_live_button_hidden_on_edit(self, auth_client):
        """No "Score live" on the match edit form — feature is creation-only."""
        from pingpong.tests.conftest import MatchFactory
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        match = MatchFactory(player1=p1, player2=p2)
        client = auth_client(p1.user)
        resp = client.get(reverse("pingpong:match_edit", args=[match.pk]))
        if resp.status_code == 200:
            assert b'name="start_live"' not in resp.content

    def test_score_live_falls_back_to_normal_for_doubles(self, auth_client):
        """Doubles is KAN-13 (future). Submitting start_live for a doubles
        match still creates the match (non-live) so the user isn't blocked."""
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        p3 = PlayerFactory(with_user=True)
        p4 = PlayerFactory(with_user=True)
        client = auth_client(p1.user)

        data = self._base_form_data(p1, p2)
        data["player3"] = p3.pk
        data["player4"] = p4.pk
        data["is_double"] = "True"
        data["start_live"] = "1"
        resp = client.post(reverse("pingpong:match_add"), data=data)

        assert resp.status_code == 302
        match = Match.all_objects.latest("pk")
        assert match.is_double is True
        assert match.is_live is False
        assert resp.url == reverse("pingpong:match_detail", args=[match.pk])


# ---------------------------------------------------------------------------
# KAN-27: Scoreboard page render
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestScoreboardPage:
    def test_scorekeeper_sees_scoreboard(self, auth_client):
        match, p1, _ = _make_live_match()
        client = auth_client(p1.user)
        resp = client.get(reverse("pingpong:live_scoreboard", args=[match.pk]))
        assert resp.status_code == 200
        assert b'id="scoreboard"' in resp.content
        assert b'id="scoreboard-bootstrap"' in resp.content
        assert b"Who's serving" in resp.content
        # tap zones present
        assert b'addPoint(' in resp.content
        # wake lock acquisition in client JS
        assert b"wakeLock" in resp.content

    def test_non_scorekeeper_gets_403(self, auth_client):
        match, _, p2 = _make_live_match()
        client = auth_client(p2.user)
        resp = client.get(reverse("pingpong:live_scoreboard", args=[match.pk]))
        assert resp.status_code == 403

    def test_completed_match_redirects_to_detail(self, auth_client):
        match, p1, _ = _make_live_match()
        Match.all_objects.filter(pk=match.pk).update(is_live=False)
        client = auth_client(p1.user)
        resp = client.get(reverse("pingpong:live_scoreboard", args=[match.pk]))
        assert resp.status_code == 302
        assert resp.url == reverse("pingpong:match_detail", args=[match.pk])

    def test_bootstrap_payload_includes_state_and_urls(self, auth_client):
        match, p1, _ = _make_live_match()
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)
        client = auth_client(p1.user)
        resp = client.get(reverse("pingpong:live_scoreboard", args=[match.pk]))
        assert resp.status_code == 200
        # endpoint urls passed through data-* attributes
        assert reverse("pingpong:live_point", args=[match.pk]).encode() in resp.content
        assert reverse("pingpong:live_state", args=[match.pk]).encode() in resp.content
        assert reverse("pingpong:live_undo", args=[match.pk]).encode() in resp.content


# ---------------------------------------------------------------------------
# KAN-9: Side-switch prompt at midpoint of deciding game
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSideSwitchEndpoint:
    def _drive_to_deciding_game_at_5(self, client, match):
        """Play out a best-of-3 to 1-1 then score to 5-0 in game 3."""
        url = reverse("pingpong:live_point", args=[match.pk])
        body_t1 = json.dumps({"side": "team1"})
        body_t2 = json.dumps({"side": "team2"})
        for _ in range(11):
            client.post(url, data=body_t1, content_type="application/json")
        for _ in range(11):
            client.post(url, data=body_t2, content_type="application/json")
        for _ in range(5):
            r = client.post(url, data=body_t1, content_type="application/json")
        return r

    def test_prompt_fires_in_deciding_game_at_5(self, auth_client):
        match, p1, _ = _make_live_match(best_of=3)
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)
        client = auth_client(p1.user)

        last = self._drive_to_deciding_game_at_5(client, match)
        data = last.json()
        assert data["state"]["current_game_number"] == 3
        assert data["state"]["team1_points"] == 5
        assert data["should_prompt_side_switch"] is True

    def test_side_switch_endpoint_marks_confirmed(self, auth_client):
        match, p1, _ = _make_live_match(best_of=3)
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)
        client = auth_client(p1.user)
        self._drive_to_deciding_game_at_5(client, match)

        resp = client.post(reverse("pingpong:live_side_switch", args=[match.pk]),
                           data="{}", content_type="application/json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"]["side_switched"] is True
        assert data["should_prompt_side_switch"] is False

    def test_side_switch_does_not_fire_again_same_match(self, auth_client):
        match, p1, _ = _make_live_match(best_of=3)
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)
        client = auth_client(p1.user)
        self._drive_to_deciding_game_at_5(client, match)

        # confirm
        client.post(reverse("pingpong:live_side_switch", args=[match.pk]),
                    data="{}", content_type="application/json")
        # score more points — prompt stays off
        url = reverse("pingpong:live_point", args=[match.pk])
        r = client.post(url, data=json.dumps({"side": "team1"}),
                        content_type="application/json")
        assert r.json()["should_prompt_side_switch"] is False
        assert r.json()["state"]["team1_points"] == 6

    def test_no_prompt_in_non_deciding_game(self, auth_client):
        match, p1, _ = _make_live_match(best_of=5)
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)
        client = auth_client(p1.user)
        # 5-0 in game 1 (not the deciding game of best-of-5)
        url = reverse("pingpong:live_point", args=[match.pk])
        body = json.dumps({"side": "team1"})
        for _ in range(5):
            r = client.post(url, data=body, content_type="application/json")
        assert r.json()["should_prompt_side_switch"] is False

    def test_side_switch_persists_through_reload(self, auth_client):
        match, p1, _ = _make_live_match(best_of=3)
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)
        client = auth_client(p1.user)
        self._drive_to_deciding_game_at_5(client, match)
        client.post(reverse("pingpong:live_side_switch", args=[match.pk]),
                    data="{}", content_type="application/json")
        # GET state — flag persists
        resp = client.get(reverse("pingpong:live_state", args=[match.pk]))
        assert resp.json()["state"]["side_switched"] is True


# ---------------------------------------------------------------------------
# KAN-10: Undo last point
# ---------------------------------------------------------------------------


class TestUndoPure:
    def test_undo_regular_point(self):
        state = _started(best_of=5)
        state, _ = ls.apply_point(state, "team1")
        state, _ = ls.apply_point(state, "team1")
        assert state["team1_points"] == 2

        undone, ended = ls.undo_last_point(state)
        assert ended is None
        assert undone["team1_points"] == 1
        assert undone["team2_points"] == 0
        assert len(undone["events"]) == 1

    def test_undo_at_0_0_is_noop(self):
        state = _started()
        undone, ended = ls.undo_last_point(state)
        assert ended is None
        assert undone == state

    def test_undo_point_that_ended_a_game(self):
        state = _started(best_of=5)
        # team1 wins game 1: 11-0
        state, _ = _play(state, ["team1"] * 11)
        assert state["team1_games"] == 1
        assert state["current_game_number"] == 2
        assert state["team1_points"] == 0

        undone, ended = ls.undo_last_point(state)
        assert ended is not None
        assert ended["game_number"] == 1
        assert ended["winner"] == "team1"
        # State rolled back into the in-progress game
        assert undone["team1_games"] == 0
        assert undone["team1_points"] == 10
        assert undone["team2_points"] == 0
        assert undone["current_game_number"] == 1

    def test_multiple_undos_unwind_game_boundary(self):
        state = _started(best_of=5)
        state, _ = _play(state, ["team1"] * 11)
        # Score 2 more (start of game 2)
        state, _ = ls.apply_point(state, "team2")
        state, _ = ls.apply_point(state, "team2")
        assert state["team2_points"] == 2
        assert state["current_game_number"] == 2

        # Undo 4 times — back to 9-0 in game 1
        for _ in range(4):
            state, _ = ls.undo_last_point(state)
        assert state["current_game_number"] == 1
        assert state["team1_points"] == 9
        assert state["team2_points"] == 0
        assert state["team1_games"] == 0


@pytest.mark.django_db
class TestUndoEndpoint:
    def test_undo_endpoint_reverts_last_point(self, auth_client):
        match, p1, _ = _make_live_match()
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)
        client = auth_client(p1.user)

        client.post(reverse("pingpong:live_point", args=[match.pk]),
                    data=json.dumps({"side": "team1"}),
                    content_type="application/json")
        client.post(reverse("pingpong:live_point", args=[match.pk]),
                    data=json.dumps({"side": "team1"}),
                    content_type="application/json")

        resp = client.post(reverse("pingpong:live_undo", args=[match.pk]),
                           data="{}", content_type="application/json")
        assert resp.status_code == 200
        assert resp.json()["state"]["team1_points"] == 1

    def test_undo_at_game_boundary_deletes_game_row(self, auth_client):
        match, p1, _ = _make_live_match()
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)
        client = auth_client(p1.user)

        point_url = reverse("pingpong:live_point", args=[match.pk])
        for _ in range(11):
            client.post(point_url, data=json.dumps({"side": "team1"}),
                        content_type="application/json")
        assert Game.all_objects.filter(match=match, game_number=1).exists()

        resp = client.post(reverse("pingpong:live_undo", args=[match.pk]),
                           data="{}", content_type="application/json")
        assert resp.status_code == 200
        # Game row deleted, in-progress restored
        assert not Game.all_objects.filter(match=match, game_number=1).exists()
        data = resp.json()
        assert data["state"]["team1_games"] == 0
        assert data["state"]["team1_points"] == 10
        assert data["state"]["current_game_number"] == 1

    def test_undo_at_match_start_is_noop(self, auth_client):
        match, p1, _ = _make_live_match()
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)
        client = auth_client(p1.user)

        resp = client.post(reverse("pingpong:live_undo", args=[match.pk]),
                           data="{}", content_type="application/json")
        assert resp.status_code == 200
        assert resp.json()["state"]["team1_points"] == 0
        assert resp.json()["state"]["team2_points"] == 0

    def test_undo_after_match_complete_returns_409(self, auth_client):
        match, p1, _ = _make_live_match(best_of=3)
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)
        client = auth_client(p1.user)
        # Drive to match end
        for _ in range(22):
            client.post(reverse("pingpong:live_point", args=[match.pk]),
                        data=json.dumps({"side": "team1"}),
                        content_type="application/json")

        resp = client.post(reverse("pingpong:live_undo", args=[match.pk]),
                           data="{}", content_type="application/json")
        assert resp.status_code == 409

    def test_undo_non_scorekeeper_rejected(self, auth_client):
        match, _, p2 = _make_live_match()
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)
        client = auth_client(p2.user)
        resp = client.post(reverse("pingpong:live_undo", args=[match.pk]),
                           data="{}", content_type="application/json")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# KAN-11: Dashboard resume banner
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDashboardResumeBanner:
    def test_banner_shows_when_user_has_live_match(self, auth_client):
        match, p1, _ = _make_live_match()
        state = ls.set_initial_server(match.live_state, "team1")
        # Play one point so live_state has a current score
        state, _ = ls.apply_point(state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)

        client = auth_client(p1.user)
        resp = client.get(reverse("pingpong:dashboard"))
        assert resp.status_code == 200
        assert b"live-resume-banner" in resp.content
        assert b"Resume" in resp.content
        # opponent name appears
        opp_name = match.side2_players.first().name.encode()
        assert opp_name in resp.content

    def test_no_banner_when_no_live_matches(self, auth_client):
        p1 = PlayerFactory(with_user=True)
        client = auth_client(p1.user)
        resp = client.get(reverse("pingpong:dashboard"))
        assert resp.status_code == 200
        assert b"live-resume-banner" not in resp.content

    def test_other_users_dont_see_my_live_match(self, auth_client):
        match, p1, p2 = _make_live_match()
        # p2 is NOT the scorekeeper
        client = auth_client(p2.user)
        resp = client.get(reverse("pingpong:dashboard"))
        assert resp.status_code == 200
        assert b"live-resume-banner" not in resp.content

    def test_resume_link_targets_scoreboard(self, auth_client):
        match, p1, _ = _make_live_match()
        client = auth_client(p1.user)
        resp = client.get(reverse("pingpong:dashboard"))
        scoreboard_url = reverse(
            "pingpong:live_scoreboard", args=[match.pk]
        ).encode()
        assert scoreboard_url in resp.content


# ---------------------------------------------------------------------------
# KAN-12: Hand-off to match_confirm with DB-shape parity
# ---------------------------------------------------------------------------


def _score_via_scoreboard(client, match, scripted_winners):
    """Drive a live match to completion by POSTing each individual point.

    scripted_winners: list of "team1"/"team2" — one per game — sets up
    11-0 wins for that side in each game.
    """
    point_url = reverse("pingpong:live_point", args=[match.pk])
    for winner_side in scripted_winners:
        for _ in range(11):
            r = client.post(point_url, data=json.dumps({"side": winner_side}),
                            content_type="application/json")
            assert r.status_code == 200
    return r


@pytest.mark.django_db
class TestMatchConfirmHandoff:
    def test_redirect_to_match_confirm_at_match_end(self, auth_client):
        match, p1, _ = _make_live_match(best_of=3)
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)
        client = auth_client(p1.user)
        last = _score_via_scoreboard(client, match, ["team1", "team1"])
        assert last.json()["redirect_url"] == reverse(
            "pingpong:match_confirm", args=[match.pk]
        )

    def test_signals_fire_and_email_sent_on_completion(self, auth_client, mailoutbox):
        match, p1, p2 = _make_live_match(best_of=3)
        # Both players verified so the email path is taken (not auto-confirm)
        state = ls.set_initial_server(match.live_state, "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=state)
        client = auth_client(p1.user)

        _score_via_scoreboard(client, match, ["team1", "team1"])

        match.refresh_from_db()
        assert match.winner_side == Side.ONE
        # One confirmation email goes to the other verified player (p2)
        recipients = [addr for m in mailoutbox for addr in m.to]
        assert p2.user.email in recipients

    def test_db_shape_parity_with_manual_path(self, auth_client):
        """A live-scored match should land in the same DB state as a match
        created via MatchCreateView + GameCreateView (modulo timestamps,
        live_state, and the scorekeeper field which is intrinsic to live)."""
        # Build the live match with UNVERIFIED players so the comparison to
        # the manual MatchFactory path (also unverified) is apples-to-apples.
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        match_live = MatchFactory(
            player1=p1, player2=p2, best_of=5,
            is_live=True, scorekeeper=p1,
            live_state=ls.initial_state(5),
        )
        state = ls.set_initial_server(match_live.live_state, "team1")
        Match.all_objects.filter(pk=match_live.pk).update(live_state=state)
        client = auth_client(p1.user)
        point_url = reverse("pingpong:live_point", args=[match_live.pk])

        # game 1: 11-5
        for _ in range(5):
            client.post(point_url, data=json.dumps({"side": "team2"}),
                        content_type="application/json")
        for _ in range(11):
            client.post(point_url, data=json.dumps({"side": "team1"}),
                        content_type="application/json")
        # game 2: 11-7
        for _ in range(7):
            client.post(point_url, data=json.dumps({"side": "team2"}),
                        content_type="application/json")
        for _ in range(11):
            client.post(point_url, data=json.dumps({"side": "team1"}),
                        content_type="application/json")
        # game 3: 11-9 (match-ending)
        for _ in range(9):
            client.post(point_url, data=json.dumps({"side": "team2"}),
                        content_type="application/json")
        for _ in range(11):
            r = client.post(point_url, data=json.dumps({"side": "team1"}),
                            content_type="application/json")
        assert r.json()["is_match_complete"]

        match_live.refresh_from_db()

        # Manually-created match with identical structure
        p3 = PlayerFactory(with_user=True)
        p4 = PlayerFactory(with_user=True)
        match_manual = MatchFactory(player1=p3, player2=p4, best_of=5)
        GameFactory(match=match_manual, game_number=1, team1_score=11, team2_score=5)
        GameFactory(match=match_manual, game_number=2, team1_score=11, team2_score=7)
        GameFactory(match=match_manual, game_number=3, team1_score=11, team2_score=9)
        match_manual.refresh_from_db()

        # Parity on match-level fields (excluding ts, live_state, FKs, scorekeeper)
        assert match_live.is_live is False
        assert match_live.live_state is None
        assert match_live.is_confirmed == match_manual.is_confirmed
        assert match_live.team1_score_cache == match_manual.team1_score_cache  # 3
        assert match_live.team2_score_cache == match_manual.team2_score_cache  # 0
        assert match_live.winner_side == Side.ONE
        assert match_manual.winner_side == Side.ONE
        assert match_live.best_of == match_manual.best_of

        # Parity on game-level fields
        live_games = list(
            Game.all_objects.filter(match=match_live)
            .order_by("game_number")
            .values("game_number", "team1_score", "team2_score")
        )
        manual_games = list(
            Game.all_objects.filter(match=match_manual)
            .order_by("game_number")
            .values("game_number", "team1_score", "team2_score")
        )
        assert live_games == manual_games

    def test_full_flow_from_score_live_button_to_confirm(self, auth_client, mailoutbox):
        """Integration test: form → live match → score → match_confirm
        redirect → confirmations created → email sent.
        """
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        for p in (p1, p2):
            p.user.profile.email_verified = True
            p.user.profile.save()
        client = auth_client(p1.user)

        # 1. Submit match form with "Score live"
        resp = client.post(reverse("pingpong:match_add"), data={
            "player1": p1.pk,
            "player2": p2.pk,
            "player3": "",
            "player4": "",
            "is_double": "False",
            "date_played": "2026-05-17T12:00",
            "location": "",
            "match_type": "casual",
            "best_of": 3,
            "notes": "",
            "start_live": "1",
        })
        assert resp.status_code == 302
        match = Match.all_objects.latest("pk")
        assert resp.url == reverse("pingpong:live_scoreboard", args=[match.pk])

        # 2. Page renders
        assert client.get(resp.url).status_code == 200

        # 3. Start the match
        r = client.post(reverse("pingpong:live_start", args=[match.pk]),
                        data=json.dumps({"initial_server": "team1"}),
                        content_type="application/json")
        assert r.status_code == 200

        # 4. Score to match-end (best_of=3 → team1 wins 2 games)
        _score_via_scoreboard(client, match, ["team1", "team1"])

        # 5. Follow the redirect_url
        confirm_resp = client.get(
            reverse("pingpong:match_confirm", args=[match.pk]),
            follow=True
        )
        assert confirm_resp.status_code == 200

        # 6. Scorekeeper is now confirmed, the other got an email
        match.refresh_from_db()
        from pingpong.models import MatchConfirmation
        assert MatchConfirmation.objects.filter(match=match, player=p1).exists()
        recipients = [addr for em in mailoutbox for addr in em.to]
        assert p2.user.email in recipients


class TestFinalScoreRules:
    """is_valid_final_score / common_final_scores -- no DB, no Django."""

    @pytest.mark.parametrize(
        "t1,t2",
        [
            (11, 0),   # shutout
            (11, 9),   # closest win without deuce
            (0, 11),
            (9, 11),
            (12, 10),  # one exchange past deuce
            (10, 12),
            (15, 13),  # long deuce
        ],
    )
    def test_accepts_scores_a_real_game_could_end_on(self, t1, t2):
        assert ls.is_valid_final_score(t1, t2) is True

    @pytest.mark.parametrize(
        "t1,t2,why",
        [
            (11, 11, "a tie"),
            (5, 3, "nobody reached 11"),
            (10, 8, "nobody reached 11"),
            (11, 10, "11-10 is not a win, play continues"),
            (13, 5, "play would have stopped at 11-5"),
            (20, 2, "same, further out"),
            (-1, 11, "negative"),
        ],
    )
    def test_rejects_scores_that_cannot_happen(self, t1, t2, why):
        assert ls.is_valid_final_score(t1, t2) is False, why

    def test_presets_are_the_four_shortcut_scorelines(self):
        assert ls.common_final_scores() == [(11, 0), (11, 9), (0, 11), (9, 11)]

    def test_every_preset_is_itself_a_valid_final_score(self):
        """The presets are derived from the rules, so they must satisfy them."""
        for t1, t2 in ls.common_final_scores():
            assert ls.is_valid_final_score(t1, t2)

    def test_presets_track_the_rule_constants(self):
        """Derived, not typed out: the shutout and the closest win both
        follow from WIN_POINTS and MIN_LEAD."""
        presets = ls.common_final_scores()
        assert presets[0] == (ls.WIN_POINTS, 0)
        assert presets[1] == (ls.WIN_POINTS, ls.WIN_POINTS - ls.MIN_LEAD)


@pytest.mark.django_db
class TestGameFormUsesTheSharedRule:
    """GameForm held a partial third copy of the scoring rules."""

    def _form(self, t1, t2):
        from pingpong.forms import GameForm

        return GameForm({
            "game_number": 1, "team1_score": t1, "team2_score": t2,
        })

    def test_accepts_a_legal_finish(self):
        assert self._form(11, 5).is_valid()

    def test_rejects_a_tie(self):
        form = self._form(11, 11)
        assert not form.is_valid()
        assert "tie" in str(form.errors)

    def test_rejects_a_score_where_nobody_reached_eleven(self):
        """Previously accepted: the old check only looked at ties and 10-10."""
        form = self._form(5, 3)
        assert not form.is_valid()
        assert "11 points" in str(form.errors)

    def test_rejects_an_impossible_blowout(self):
        """13-5 cannot occur -- play stops the moment the lead is enough.
        Previously accepted."""
        form = self._form(13, 5)
        assert not form.is_valid()

    def test_rejects_a_one_point_lead_past_deuce(self):
        form = self._form(11, 10)
        assert not form.is_valid()
        assert "win by 2" in str(form.errors)

    def test_accepts_a_deuce_finish(self):
        assert self._form(12, 10).is_valid()


# ---------------------------------------------------------------------------
# Scoreboard responsiveness (apple-design §1 "Response", §13 haptics)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestScoreboardResponsiveness:
    """Markup/handler contracts that would fail silently if broken.

    None of this is testable through the Python layer -- the behaviour lives
    in the Alpine component -- but each assertion pins a coupling that has a
    real failure mode, so a refactor that drops one gets caught here rather
    than on a phone at the table.
    """

    def _page(self, auth_client):
        match, p1, _ = _make_live_match()
        resp = auth_client(p1.user).get(
            reverse("pingpong:live_scoreboard", args=[match.pk])
        )
        assert resp.status_code == 200
        return resp.content.decode()

    def test_score_reads_through_the_optimistic_overlay(self, auth_client):
        """Binding straight to state.teamN_points puts the round-trip back on
        the input path: the numeral would only move once the POST returned."""
        body = self._page(auth_client)
        assert "points('team1')" in body
        assert "points('team2')" in body
        assert 'x-text="state.team1_points"' not in body

    def test_tap_zones_are_never_disabled(self, auth_client):
        """`:disabled="busy"` on a tap zone drops points during a slow POST."""
        body = self._page(auth_client)
        zones = body.count("tap-zone")
        assert zones == 2, f"expected two tap zones, found {zones}"
        # The control cluster may still disable on `busy`; the zones may not.
        for fragment in body.split("<button")[1:]:
            if "tap-zone" in fragment.split(">")[0]:
                assert ":disabled" not in fragment.split(">")[0]

    def test_press_and_haptic_feedback_are_wired(self, auth_client):
        body = self._page(auth_client)
        assert "bumpScore" in body       # visual confirmation of the tap
        assert "navigator.vibrate" in body

    def test_errors_do_not_use_a_blocking_dialog(self, auth_client):
        """alert() freezes the page mid-match and reads as a browser failure."""
        body = self._page(auth_client)
        assert "alert(" not in body
        assert "showError" in body

    def test_requests_are_serialised(self, auth_client):
        """Points must reach the server in tap order once zones accept
        taps while a request is in flight."""
        body = self._page(auth_client)
        assert "enqueue" in body
