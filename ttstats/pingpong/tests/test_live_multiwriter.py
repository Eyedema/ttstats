"""S13.2 / KAN-150 -- the op rules that make two writers correct.

Two phones scoring one match (KAN-149 gave each its own op stream). Here:

* **Side restriction**: a scorer without ``can_score_both`` scores only for
  their own side; anything else is 409 ``not your side`` and consumes the seq.
* **Attribution**: every point records who tapped it (``PointEvent.recorded_by``
  and ``live_state["events"][i]["by"]``), single-scorer matches included.
* **Conditional undo**: an undo may carry ``expects`` (the ``PointEvent``
  sequence it believes is trailing); a mismatch is 409 ``stale undo``. In a
  shared match ``expects`` is required. Either scorer may undo the trailing
  point, whoever recorded it.
* **Start / side switch** with two writers: the first ``start`` wins, the
  second is ``ALREADY_STARTED``; ``side_switch`` from both is idempotent.

The two-stream fidelity harness lives in ``test_point_fidelity.py``.
"""
import json

import pytest
from django.urls import reverse

from pingpong import live_scoring as ls
from pingpong.models import LiveScorer, Match, PointEvent, Side

from .conftest import MatchFactory, PlayerFactory, live_match


def _post(client, name, match, body=None):
    return client.post(
        reverse(f"pingpong:{name}", args=[match.pk]),
        data=json.dumps(body or {}),
        content_type="application/json",
    )


def _point(client, match, seq, side):
    return _post(client, "live_point", match, {"seq": seq, "side": side})


def _undo(client, match, seq, expects=None):
    body = {"seq": seq}
    if expects is not None:
        body["expects"] = expects
    return _post(client, "live_undo", match, body)


def _sync(client, match, ops):
    return _post(client, "live_sync", match, {"ops": ops})


def _scorer(match, role):
    return LiveScorer.objects.get(match=match, role=role)


def _shared(auth_client, **kwargs):
    """A started shared match: A (host, side 1) and B (guest, side 2)."""
    match = live_match(started=True, shared=True, **kwargs)
    a = _scorer(match, LiveScorer.Role.HOST).player
    b = _scorer(match, LiveScorer.Role.GUEST).player
    return match, (a, auth_client(a.user)), (b, auth_client(b.user))


def _rows(match):
    return list(
        PointEvent.objects.filter(match=match).values_list(
            "sequence", "scoring_side", "recorded_by"
        )
    )


def _rev(match):
    return Match.all_objects.get(pk=match.pk).live_rev


# ---------------------------------------------------------------------------
# live_scoring: ``by`` is an opaque pass-through (pure, no DB)
# ---------------------------------------------------------------------------


class TestEventsCarryBy:

    def _started(self):
        return ls.set_initial_server(ls.initial_state(5), "team1")

    def test_by_is_recorded_on_the_event(self):
        state, _ = ls.apply_point(self._started(), "team2", now="t1", by=42)
        assert state["events"][-1] == {"side": "team2", "t": "t1", "game": 1, "by": 42}

    def test_without_by_the_event_shape_is_unchanged(self):
        state, _ = ls.apply_point(self._started(), "team1", now="t1")
        assert state["events"][-1] == {"side": "team1", "t": "t1", "game": 1}

    def test_by_is_opaque(self):
        state, _ = ls.apply_point(self._started(), "team1", now="t1", by="phone-a")
        assert state["events"][-1]["by"] == "phone-a"

    def test_a_game_ending_event_keeps_by(self):
        state = self._started()
        for _ in range(ls.WIN_POINTS):
            state, completed = ls.apply_point(state, "team1", now="t", by=7)
        assert completed is not None
        assert state["events"][-1]["game_end"] is True
        assert state["events"][-1]["by"] == 7
        undone, game = ls.undo_last_point(state)
        assert game == {"game_number": 1, "winner": "team1"}
        assert undone["team1_points"] == ls.WIN_POINTS - 1


# ---------------------------------------------------------------------------
# 1. Side restriction
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSideRestriction:

    def test_guest_scoring_for_the_other_side_is_refused_and_consumes_the_seq(
        self, auth_client
    ):
        match, _, (_, b) = _shared(auth_client)
        rev = _rev(match)

        resp = _point(b, match, 1, "team1")

        assert resp.status_code == 409
        assert resp.json() == {"error": "not your side", "applied_seq": 1}
        assert _rows(match) == []
        assert _scorer(match, LiveScorer.Role.GUEST).applied_seq == 1
        assert _rev(match) == rev
        # The stream carries on at seq 2.
        assert _point(b, match, 2, "team2").status_code == 200
        assert [r[1] for r in _rows(match)] == [Side.TWO]

    def test_host_of_a_shared_match_is_restricted_too(self, auth_client):
        match, (_, a), _ = _shared(auth_client)
        resp = _point(a, match, 1, "team2")
        assert resp.status_code == 409
        assert resp.json()["error"] == "not your side"
        assert _rows(match) == []

    def test_each_scorer_scores_their_own_side(self, auth_client):
        match, (pa, a), (pb, b) = _shared(auth_client)
        assert _point(a, match, 1, "team1").status_code == 200
        assert _point(b, match, 1, "team2").status_code == 200
        assert _rows(match) == [(1, Side.ONE, pa.pk), (2, Side.TWO, pb.pk)]

    def test_a_solo_host_scores_both_sides(self, auth_client):
        match = live_match(started=True)
        client = auth_client(match.scorekeeper.user)
        assert _point(client, match, 1, "team1").status_code == 200
        assert _point(client, match, 2, "team2").status_code == 200
        assert len(_rows(match)) == 2

    def test_in_a_batch_the_wrong_side_stops_the_batch_and_is_named(self, auth_client):
        match, _, (_, b) = _shared(auth_client)
        resp = _sync(b, match, [
            {"seq": 1, "type": "point", "side": "team2"},
            {"seq": 2, "type": "point", "side": "team1"},
            {"seq": 3, "type": "point", "side": "team2"},
        ])
        assert resp.status_code == 200
        data = resp.json()
        assert data["rejected"] == {"seq": 2, "error": "not your side"}
        assert data["applied_seq"] == 2
        assert len(_rows(match)) == 1


# ---------------------------------------------------------------------------
# 2. Attribution
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAttribution:

    def test_a_solo_match_records_the_host_on_every_point(self, auth_client):
        match = live_match(started=True)
        host = match.scorekeeper
        client = auth_client(host.user)
        _point(client, match, 1, "team1")
        _point(client, match, 2, "team2")
        assert [r[2] for r in _rows(match)] == [host.pk, host.pk]
        state = Match.all_objects.get(pk=match.pk).live_state
        assert [e["by"] for e in state["events"]] == [host.pk, host.pk]

    def test_a_shared_match_records_each_writer(self, auth_client):
        match, (pa, a), (pb, b) = _shared(auth_client)
        _point(b, match, 1, "team2")
        _point(a, match, 1, "team1")
        state = Match.all_objects.get(pk=match.pk).live_state
        assert [e["by"] for e in state["events"]] == [pb.pk, pa.pk]
        assert [r[2] for r in _rows(match)] == [pb.pk, pa.pk]

    def test_a_row_less_legacy_match_records_its_scorekeeper(self, auth_client):
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        match = MatchFactory(
            player1=p1, player2=p2, is_live=True, scorekeeper=p1,
            live_state=ls.set_initial_server(ls.initial_state(5), "team1"),
        )
        assert _point(auth_client(p1.user), match, 1, "team2").status_code == 200
        assert [r[2] for r in _rows(match)] == [p1.pk]


# ---------------------------------------------------------------------------
# 3. last_point in the payload, and the conditional undo
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLastPoint:

    def test_null_before_any_point(self, auth_client):
        match = live_match(started=True)
        resp = auth_client(match.scorekeeper.user).get(
            reverse("pingpong:live_state", args=[match.pk])
        )
        assert resp.json()["last_point"] is None

    def test_names_the_trailing_row(self, auth_client):
        match, (pa, a), (pb, b) = _shared(auth_client)
        _point(a, match, 1, "team1")
        data = _point(b, match, 1, "team2").json()
        assert data["last_point"] == {"sequence": 2, "side": "team2", "by": pb.pk}

    def test_follows_an_undo(self, auth_client):
        match, (pa, a), (pb, b) = _shared(auth_client)
        _point(a, match, 1, "team1")
        _point(b, match, 1, "team2")
        data = _undo(a, match, 2, expects=2).json()
        assert data["last_point"] == {"sequence": 1, "side": "team1", "by": pa.pk}


@pytest.mark.django_db
class TestConditionalUndo:

    def test_stale_expects_is_refused_then_the_right_one_removes_the_other_point(
        self, auth_client
    ):
        """The acceptance case: A expects #7 while B's #8 already arrived."""
        match, (pa, a), (pb, b) = _shared(auth_client)
        for seq in range(1, 8):
            side = "team1" if seq % 2 else "team2"
            client = a if side == "team1" else b
            stream_seq = (seq + 1) // 2 if side == "team1" else seq // 2
            assert _point(client, match, stream_seq, side).status_code == 200
        # A's 4 points (seqs 1-4) and B's 3 (seqs 1-3) are rows #1..#7.
        assert _point(b, match, 4, "team2").status_code == 200  # row #8
        rows = _rows(match)
        assert len(rows) == 8
        rev = _rev(match)

        resp = _undo(a, match, 5, expects=7)

        assert resp.status_code == 409
        assert resp.json() == {"error": "stale undo", "applied_seq": 5}
        assert _rows(match) == rows
        assert _rev(match) == rev
        assert _scorer(match, LiveScorer.Role.HOST).applied_seq == 5

        resp = _undo(a, match, 6, expects=8)

        assert resp.status_code == 200
        assert _rows(match) == rows[:7]
        assert rows[7] == (8, Side.TWO, pb.pk)
        assert resp.json()["last_point"]["sequence"] == 7

    def test_shared_undo_without_expects_is_stale(self, auth_client):
        match, (_, a), _ = _shared(auth_client)
        _point(a, match, 1, "team1")
        resp = _undo(a, match, 2)
        assert resp.status_code == 409
        assert resp.json()["error"] == "stale undo"
        assert len(_rows(match)) == 1

    def test_either_scorer_may_undo_the_trailing_point(self, auth_client):
        match, (_, a), (_, b) = _shared(auth_client)
        _point(a, match, 1, "team1")
        assert _undo(b, match, 1, expects=1).status_code == 200
        assert _rows(match) == []

    def test_expects_with_nothing_to_undo_is_stale(self, auth_client):
        match, (_, a), _ = _shared(auth_client)
        resp = _undo(a, match, 1, expects=1)
        assert resp.status_code == 409
        assert resp.json()["error"] == "stale undo"

    def test_solo_undo_without_expects_is_unchanged(self, auth_client):
        match = live_match(started=True)
        client = auth_client(match.scorekeeper.user)
        _point(client, match, 1, "team1")
        assert _undo(client, match, 2).status_code == 200
        assert _rows(match) == []

    def test_solo_undo_honours_expects_when_sent(self, auth_client):
        match = live_match(started=True)
        client = auth_client(match.scorekeeper.user)
        _point(client, match, 1, "team1")
        _point(client, match, 2, "team1")
        assert _undo(client, match, 3, expects=1).status_code == 409
        assert _undo(client, match, 4, expects=2).status_code == 200
        assert len(_rows(match)) == 1

    @pytest.mark.parametrize("expects", ["7", True, 0, -1, 1.5])
    def test_a_malformed_expects_is_400(self, auth_client, expects):
        match = live_match(started=True)
        client = auth_client(match.scorekeeper.user)
        resp = _post(client, "live_undo", match, {"seq": 1, "expects": expects})
        assert resp.status_code == 400
        resp = _sync(client, match, [{"seq": 1, "type": "undo", "expects": expects}])
        assert resp.status_code == 400

    def test_expects_through_live_sync(self, auth_client):
        match, (_, a), (_, b) = _shared(auth_client)
        _sync(a, match, [{"seq": 1, "type": "point", "side": "team1"}])
        _sync(b, match, [{"seq": 1, "type": "point", "side": "team2"}])
        data = _sync(a, match, [
            {"seq": 2, "type": "undo", "expects": 1},
            {"seq": 3, "type": "point", "side": "team1"},
        ]).json()
        assert data["rejected"] == {"seq": 2, "error": "stale undo"}
        assert data["applied_seq"] == 2
        data = _sync(a, match, [
            {"seq": 3, "type": "undo", "expects": 2},
        ]).json()
        assert data["rejected"] is None
        assert [r[1] for r in _rows(match)] == [Side.ONE]


# ---------------------------------------------------------------------------
# 4. Start and side switch with two writers
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestStartAndSideSwitchWithTwoWriters:

    def test_first_start_wins_and_the_second_is_already_started(self, auth_client):
        match = live_match(shared=True)
        a = auth_client(_scorer(match, LiveScorer.Role.HOST).player.user)
        b = auth_client(_scorer(match, LiveScorer.Role.GUEST).player.user)

        assert _post(a, "live_start", match, {"seq": 1, "initial_server": "team1"}).status_code == 200
        rev = _rev(match)
        resp = _post(b, "live_start", match, {"seq": 1, "initial_server": "team2"})

        assert resp.status_code == 409
        assert resp.json() == {"error": "Match already started", "applied_seq": 1}
        assert _scorer(match, LiveScorer.Role.GUEST).applied_seq == 1
        assert Match.all_objects.get(pk=match.pk).live_state["initial_server"] == "team1"
        assert _rev(match) == rev
        # B rebases onto the server's start and carries on at seq 2.
        assert _point(b, match, 2, "team2").status_code == 200

    def test_both_side_switches_apply_idempotently(self, auth_client):
        match = live_match(best_of=1, started=True, shared=True)
        state = Match.all_objects.get(pk=match.pk).live_state
        state = {**state, "team1_points": ls.SIDE_SWITCH_AT, "team2_points": 1}
        Match.all_objects.filter(pk=match.pk).update(live_state=state)
        assert ls.should_prompt_side_switch(state)
        a = auth_client(_scorer(match, LiveScorer.Role.HOST).player.user)
        b = auth_client(_scorer(match, LiveScorer.Role.GUEST).player.user)

        ra = _post(a, "live_side_switch", match, {"seq": 1})
        rb = _post(b, "live_side_switch", match, {"seq": 1})

        assert ra.status_code == rb.status_code == 200
        assert rb.json()["state"]["side_switched"] is True
        assert rb.json()["should_prompt_side_switch"] is False
        after = Match.all_objects.get(pk=match.pk).live_state
        assert {k: v for k, v in after.items() if k != "side_switched"} == {
            k: v for k, v in state.items() if k != "side_switched"
        }
