"""Q2.9 -- the point-log fidelity harness (KAN-34).

**The invariant, in plain English:** for any sequence of points and undos the
scorekeeper could tap, the rows persisted in ``PointEvent`` are a faithful
record of what ``live_scoring.py`` decided. Replaying them reproduces the same
game scores and the same match winner; the server recorded against every point
is the side ``live_scoring.current_server()`` said was serving when that point
was played; and the sequence numbers are contiguous, with no gaps and no
duplicates.

This matters more than a normal test. A wrong point log does not raise, does
not look wrong, and produces plausible statistics that are silently incorrect
forever -- there is no way to notice by inspection. So ``live_scoring`` (pure,
and already covered by ``test_match_state.py`` and ``test_scoreboard.py``) is
treated as the oracle, and this harness asserts the *persistence* layer agrees
with it over generated sequences rather than hand-picked cases. The interesting
bugs live at the undo-at-game-boundary and the 20-point serve rotation, which
are tedious to enumerate by hand and easy to miss.

``TestTheHarnessCatchesABrokenWritePath`` runs the same invariant against a
deliberately broken write path (the server read *after* the point instead of
before) and asserts it fails -- proof that the harness tests something real.

Runnable standalone::

    cd ttstats && python -m pytest pingpong/tests/test_point_fidelity.py -q
"""
import json
import random

import pytest
from django.urls import reverse

from pingpong import live_scoring as ls
from pingpong import views
from pingpong.models import Game, Match, PointEvent, Side

from .conftest import MatchFactory, PlayerFactory

# Seeds are fixed so a failure is reproducible and CI runtime is bounded. Each
# seed is an independent generated match; widen the range to search harder.
SEEDS = list(range(24))
MAX_OPERATIONS = 90


def _make_live_match(best_of=5):
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
    return match, p1


def _post(client, url, body=None):
    return client.post(
        url, data=json.dumps(body or {}), content_type="application/json"
    )


# ---------------------------------------------------------------------------
# Replay: rebuild the match from the persisted log alone
# ---------------------------------------------------------------------------


def replay(events, best_of):
    """Rebuild game finals and the match winner from persisted events only.

    Deliberately does not consult ``live_state`` or the ``Game`` rows -- the
    whole point is to ask whether the log on its own is enough.
    """
    finals = {}
    in_progress = None
    for event in events:
        score = (event.team1_score, event.team2_score)
        if ls.is_game_won(*score) is not None:
            finals[event.game_number] = score
            in_progress = None
        else:
            in_progress = (event.game_number, score)

    games_won = {"team1": 0, "team2": 0}
    for score in finals.values():
        winner = ls.is_game_won(*score)
        games_won[winner] += 1

    target = ls.games_to_win(best_of)
    match_winner = None
    for side in ("team1", "team2"):
        if games_won[side] >= target:
            match_winner = side

    return {
        "finals": finals,
        "in_progress": in_progress,
        "games_won": games_won,
        "match_winner": match_winner,
    }


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


def assert_sequence_is_contiguous(events):
    sequences = [e.sequence for e in events]
    assert sequences == sorted(sequences), "sequence numbers out of order"
    assert len(set(sequences)) == len(sequences), "duplicate sequence numbers"
    assert sequences == list(range(1, len(sequences) + 1)), (
        f"sequence has gaps: {sequences}"
    )


def assert_servers_agree(events, expected_servers):
    """Every persisted server is the side live_scoring said was serving."""
    stored = [Side.to_live(e.serving_side) for e in events]
    assert stored == expected_servers, (
        f"server attribution diverged:\n stored   {stored}\n expected {expected_servers}"
    )


def assert_log_replays_to(events, *, oracle_state, best_of, match):
    """Replayed scores equal live_scoring's, and the persisted Game rows."""
    result = replay(events, best_of)

    game_rows = {
        g.game_number: (g.team1_score, g.team2_score)
        for g in Game.all_objects.filter(match=match)
    }
    assert result["finals"] == game_rows, (
        f"replayed finals {result['finals']} != Game rows {game_rows}"
    )

    assert result["games_won"]["team1"] == oracle_state["team1_games"]
    assert result["games_won"]["team2"] == oracle_state["team2_games"]

    expected_in_progress = (
        oracle_state["team1_points"],
        oracle_state["team2_points"],
    )
    if expected_in_progress == (0, 0):
        assert result["in_progress"] is None or result["in_progress"][1] == (0, 0)
    else:
        assert result["in_progress"] is not None, "in-progress game lost in replay"
        assert result["in_progress"][1] == expected_in_progress

    assert result["match_winner"] == ls.match_winner(oracle_state)


# ---------------------------------------------------------------------------
# Generated sequences driven through the real endpoints
# ---------------------------------------------------------------------------


def drive_generated_match(client, match, seed, *, undo_probability=0.18, initial_server=None):
    """Tap a generated sequence of points and undos through the real views.

    Returns ``(oracle_state, expected_servers)``: the state ``live_scoring``
    reports having applied exactly the operations the server accepted, and the
    serving side it named for each point still standing in the log.

    The starting server alternates with the seed unless one is given, so the
    seeded suite covers both. Always starting with side 1 is exactly the run
    that cannot notice a write path ignoring ``initial_server`` (KAN-36).
    """
    rng = random.Random(seed)
    if initial_server is None:
        initial_server = "team1" if seed % 2 == 0 else "team2"
    oracle = ls.set_initial_server(ls.initial_state(match.best_of), initial_server)
    Match.all_objects.filter(pk=match.pk).update(live_state=oracle)

    expected_servers = []
    point_url = reverse("pingpong:live_point", args=[match.pk])
    undo_url = reverse("pingpong:live_undo", args=[match.pk])

    for _ in range(MAX_OPERATIONS):
        if ls.is_match_complete(oracle):
            break

        # Undo only where the client could actually undo: live_state's event
        # log is capped, so once it is empty there is nothing to take back.
        can_undo = bool(oracle.get("events"))
        if can_undo and rng.random() < undo_probability:
            resp = _post(client, undo_url)
            assert resp.status_code == 200, resp.content
            oracle, _ = ls.undo_last_point(oracle)
            if expected_servers:
                expected_servers.pop()
            continue

        side = "team1" if rng.random() < 0.5 else "team2"
        expected_servers.append(ls.current_server(oracle))
        resp = _post(client, point_url, {"side": side})
        assert resp.status_code == 200, resp.content
        oracle, _ = ls.apply_point(oracle, side)

    return oracle, expected_servers


@pytest.mark.django_db
class TestPointLogFidelity:
    """The persisted log is always a faithful record of what live_scoring decided."""

    @pytest.mark.parametrize("seed", SEEDS)
    def test_generated_sequence_of_points_and_undos(self, auth_client, seed):
        best_of = 3 if seed % 3 == 0 else 5
        match, p1 = _make_live_match(best_of=best_of)
        client = auth_client(p1.user)

        oracle, expected_servers = drive_generated_match(client, match, seed)

        events = list(PointEvent.objects.filter(match=match))
        assert_sequence_is_contiguous(events)
        assert_servers_agree(events, expected_servers)
        assert_log_replays_to(
            events, oracle_state=oracle, best_of=best_of, match=match
        )

    @pytest.mark.parametrize("initial_server", ["team1", "team2"])
    def test_long_deuce_game_crosses_the_serve_rotation_boundary(
        self, auth_client, initial_server
    ):
        """Two serves each until 20 points total, then one per point (KAN-36).

        A 20-18 game, for both starting servers: the persisted server must
        match ``current_server()`` at every single point, either side of the
        regime change at 10-10.
        """
        match, p1 = _make_live_match(best_of=5)
        client = auth_client(p1.user)
        point_url = reverse("pingpong:live_point", args=[match.pk])

        oracle = ls.set_initial_server(ls.initial_state(5), initial_server)
        Match.all_objects.filter(pk=match.pk).update(live_state=oracle)

        expected_servers = []
        script = (["team1", "team2"] * 18) + ["team1", "team1"]  # 20-18
        for side in script:
            expected_servers.append(ls.current_server(oracle))
            assert _post(client, point_url, {"side": side}).status_code == 200
            oracle, _ = ls.apply_point(oracle, side)

        events = list(PointEvent.objects.filter(match=match))
        assert len(events) == len(script)
        assert_sequence_is_contiguous(events)
        assert_servers_agree(events, expected_servers)

    def test_undo_at_every_game_boundary(self, auth_client):
        """The boundary case the generator hits rarely, pinned explicitly."""
        match, p1 = _make_live_match(best_of=5)
        client = auth_client(p1.user)
        point_url = reverse("pingpong:live_point", args=[match.pk])
        undo_url = reverse("pingpong:live_undo", args=[match.pk])

        oracle = ls.set_initial_server(ls.initial_state(5), "team1")
        Match.all_objects.filter(pk=match.pk).update(live_state=oracle)

        expected_servers = []

        def tap(side):
            expected_servers.append(ls.current_server(oracle))
            assert _post(client, point_url, {"side": side}).status_code == 200
            return ls.apply_point(oracle, side)[0]

        for _ in range(2):
            # Win a game, undo the winning point, then win it again.
            for _ in range(11):
                oracle = tap("team1")
            assert _post(client, undo_url).status_code == 200
            oracle, _ = ls.undo_last_point(oracle)
            expected_servers.pop()
            oracle = tap("team1")

        events = list(PointEvent.objects.filter(match=match))
        assert_sequence_is_contiguous(events)
        assert_servers_agree(events, expected_servers)
        assert_log_replays_to(events, oracle_state=oracle, best_of=5, match=match)


@pytest.mark.django_db
class TestTheHarnessCatchesABrokenWritePath:
    """Proof the invariant tests something real.

    Recorded on KAN-34: with ``_record_point_event`` changed to read
    ``current_server()`` from the state *after* the point -- the single easiest
    bug to ship on this write path, and one that leaves the data looking
    entirely plausible -- ``assert_servers_agree`` fails.
    """

    def test_server_read_after_the_point_is_detected(self, auth_client, monkeypatch):
        original = views._record_point_event

        def broken(match, *, state_before, side, completed, new_state):
            # The bug: name the server from the state after the point.
            return original(
                match,
                state_before=new_state,
                side=side,
                completed=completed,
                new_state=new_state,
            )

        monkeypatch.setattr(views, "_record_point_event", broken)

        match, p1 = _make_live_match(best_of=5)
        client = auth_client(p1.user)
        oracle, expected_servers = drive_generated_match(
            client, match, seed=0, undo_probability=0.0
        )

        events = list(PointEvent.objects.filter(match=match))
        # Sequence integrity is untouched by this bug -- only attribution.
        assert_sequence_is_contiguous(events)
        with pytest.raises(AssertionError, match="server attribution diverged"):
            assert_servers_agree(events, expected_servers)

    def test_an_ignored_initial_server_is_detected(self, auth_client, monkeypatch):
        """Attributing every match as if side 1 served first (KAN-36).

        Invisible whenever side 1 really did start, which is why the harness
        has to drive matches that side 2 starts.
        """
        original = views._record_point_event

        def broken(match, *, state_before, side, completed, new_state):
            # The bug: drop the match's actual starting server.
            return original(
                match,
                state_before={**state_before, "initial_server": "team1"},
                side=side,
                completed=completed,
                new_state=new_state,
            )

        monkeypatch.setattr(views, "_record_point_event", broken)

        match, p1 = _make_live_match(best_of=5)
        client = auth_client(p1.user)
        oracle, expected_servers = drive_generated_match(
            client, match, seed=0, undo_probability=0.0, initial_server="team2"
        )

        events = list(PointEvent.objects.filter(match=match))
        assert_sequence_is_contiguous(events)
        with pytest.raises(AssertionError, match="server attribution diverged"):
            assert_servers_agree(events, expected_servers)

    def test_a_dropped_row_is_detected(self, auth_client):
        """A silently lost write leaves a gap, and the harness says so."""
        match, p1 = _make_live_match(best_of=5)
        client = auth_client(p1.user)
        drive_generated_match(client, match, seed=1, undo_probability=0.0)

        PointEvent.objects.filter(match=match, sequence=3).delete()

        events = list(PointEvent.objects.filter(match=match))
        with pytest.raises(AssertionError, match="sequence has gaps"):
            assert_sequence_is_contiguous(events)
