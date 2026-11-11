"""S7.1 / KAN-85 -- idempotent live ops: client sequence numbers + server dedupe.

Every scorekeeper op (start, point, undo, side_switch) may carry a ``seq``: a
per-match, gapless, client-assigned integer starting at 1. The server records
the highest one applied in ``Match.live_applied_seq`` and

* ``seq == applied + 1`` applies the op and bumps ``applied``;
* ``seq <= applied`` is a duplicate delivery: nothing is applied, the answer
  is the canonical payload;
* ``seq > applied + 1`` is a gap: 409 ``{"error": "gap", "applied_seq": N}``;
* no ``seq`` at all is the legacy contract, unchanged.

The dedupe runs *before* the ``is_live`` check, so a retried match point after
the match completed still learns that the match is over.
"""
import json

import pytest
from django.urls import reverse

from pingpong import live_scoring as ls
from pingpong.models import Game, Match, PointEvent

from .conftest import MatchFactory, PlayerFactory


def _make_live_match(best_of=5, *, started=True, initial_server="team1"):
    p1 = PlayerFactory(with_user=True)
    p2 = PlayerFactory(with_user=True)
    for p in (p1, p2):
        p.user.profile.email_verified = True
        p.user.profile.save()
    state = ls.initial_state(best_of)
    if started:
        state = ls.set_initial_server(state, initial_server)
    match = MatchFactory(
        player1=p1,
        player2=p2,
        best_of=best_of,
        is_live=True,
        scorekeeper=p1,
        live_state=state,
    )
    return match, p1


def _post(client, name, match, body=None):
    return client.post(
        reverse(f"pingpong:{name}", args=[match.pk]),
        data=json.dumps(body or {}),
        content_type="application/json",
    )


def _point(client, match, seq=None, side="team1"):
    body = {"side": side}
    if seq is not None:
        body["seq"] = seq
    return _post(client, "live_point", match, body)


def _snapshot(match):
    match = Match.all_objects.get(pk=match.pk)
    return {
        "live_state": match.live_state,
        "is_live": match.is_live,
        "applied": match.live_applied_seq,
        "points": list(
            PointEvent.objects.filter(match=match).values_list(
                "sequence", "scoring_side", "serving_side", "team1_score", "team2_score"
            )
        ),
        "games": list(
            Game.all_objects.filter(match=match).values_list(
                "game_number", "team1_score", "team2_score"
            )
        ),
    }


@pytest.mark.django_db
class TestAppliedSeqIsPersisted:
    def test_new_match_has_no_applied_seq(self):
        match, _ = _make_live_match()
        assert match.live_applied_seq is None

    def test_applying_an_op_records_its_seq(self, auth_client):
        match, p1 = _make_live_match()
        resp = _point(auth_client(p1.user), match, seq=1)
        assert resp.status_code == 200
        assert resp.json()["applied_seq"] == 1
        match.refresh_from_db()
        assert match.live_applied_seq == 1

    def test_payload_reports_zero_before_any_seq(self, auth_client):
        match, p1 = _make_live_match()
        data = auth_client(p1.user).get(
            reverse("pingpong:live_state", args=[match.pk])
        ).json()
        assert data["applied_seq"] == 0

    def test_scoreboard_bootstrap_includes_applied_seq(self, auth_client):
        match, p1 = _make_live_match()
        Match.all_objects.filter(pk=match.pk).update(live_applied_seq=7)
        resp = auth_client(p1.user).get(
            reverse("pingpong:live_scoreboard", args=[match.pk])
        )
        assert resp.context["bootstrap"]["applied_seq"] == 7
        assert b'"applied_seq": 7' in resp.content


@pytest.mark.django_db
class TestDuplicateDelivery:
    def test_resent_point_changes_nothing(self, auth_client):
        match, p1 = _make_live_match()
        client = auth_client(p1.user)
        for seq in range(1, 6):
            assert _point(client, match, seq=seq).status_code == 200
        before = _snapshot(match)
        canonical = client.get(reverse("pingpong:live_state", args=[match.pk])).json()
        canonical.pop("csrf_token")  # per-request, not match state
        canonical.pop("server_now")  # the server's clock (KAN-154 presence)

        resp = _point(client, match, seq=5)

        assert resp.status_code == 200
        assert _snapshot(match) == before
        answer = resp.json()
        answer.pop("server_now")
        assert answer == canonical

    def test_older_duplicate_changes_nothing(self, auth_client):
        match, p1 = _make_live_match()
        client = auth_client(p1.user)
        for seq in range(1, 6):
            _point(client, match, seq=seq)
        before = _snapshot(match)
        assert _point(client, match, seq=2, side="team2").status_code == 200
        assert _snapshot(match) == before

    def test_resent_undo_does_not_undo_twice(self, auth_client):
        match, p1 = _make_live_match()
        client = auth_client(p1.user)
        _point(client, match, seq=1)
        _point(client, match, seq=2)
        assert _post(client, "live_undo", match, {"seq": 3}).status_code == 200
        before = _snapshot(match)
        assert len(before["points"]) == 1

        assert _post(client, "live_undo", match, {"seq": 3}).status_code == 200
        assert _snapshot(match) == before

    def test_resent_side_switch_is_a_duplicate(self, auth_client):
        match, p1 = _make_live_match()
        client = auth_client(p1.user)
        assert _post(client, "live_side_switch", match, {"seq": 1}).status_code == 200
        before = _snapshot(match)
        assert _post(client, "live_side_switch", match, {"seq": 1}).status_code == 200
        assert _snapshot(match) == before

    def test_resent_start_is_a_duplicate_not_a_conflict(self, auth_client):
        match, p1 = _make_live_match(started=False)
        client = auth_client(p1.user)
        r1 = _post(client, "live_start", match, {"initial_server": "team2", "seq": 1})
        assert r1.status_code == 200
        # Even with a different server: the op with seq 1 was already applied,
        # so this is a duplicate delivery and is answered, not judged.
        r2 = _post(client, "live_start", match, {"initial_server": "team1", "seq": 1})
        assert r2.status_code == 200
        assert r2.json()["state"]["initial_server"] == "team2"


@pytest.mark.django_db
class TestDuplicateOfTheCompletingPoint:
    def _finish_best_of_3(self, client, match):
        resp = None
        for seq in range(1, 23):
            resp = _point(client, match, seq=seq)
            assert resp.status_code == 200
        return resp

    def test_resent_match_point_reports_completion_not_409(self, auth_client):
        match, p1 = _make_live_match(best_of=3)
        client = auth_client(p1.user)
        last = self._finish_best_of_3(client, match)
        assert last.json()["is_match_complete"] is True
        before = _snapshot(match)

        resp = _point(client, match, seq=22)

        assert resp.status_code == 200
        data = resp.json()
        assert data["is_match_complete"] is True
        assert data["redirect_url"] == reverse("pingpong:match_confirm", args=[match.pk])
        assert data["applied_seq"] == 22
        assert _snapshot(match) == before

    def test_applied_seq_outlives_live_state(self, auth_client):
        match, p1 = _make_live_match(best_of=3)
        self._finish_best_of_3(auth_client(p1.user), match)
        match.refresh_from_db()
        assert match.live_state is None
        assert match.live_applied_seq == 22

    def test_a_new_op_after_completion_is_still_not_live(self, auth_client):
        match, p1 = _make_live_match(best_of=3)
        client = auth_client(p1.user)
        self._finish_best_of_3(client, match)
        resp = _point(client, match, seq=23)
        assert resp.status_code == 409
        assert resp.json()["error"] == "Match is not live"


@pytest.mark.django_db
class TestGap:
    @pytest.mark.parametrize(
        "name,body",
        [
            ("live_point", {"side": "team1"}),
            ("live_undo", {}),
            ("live_side_switch", {}),
        ],
    )
    def test_gap_is_409_and_applies_nothing(self, auth_client, name, body):
        match, p1 = _make_live_match()
        client = auth_client(p1.user)
        for seq in range(1, 4):
            _point(client, match, seq=seq)
        before = _snapshot(match)

        resp = _post(client, name, match, {**body, "seq": 5})

        assert resp.status_code == 409
        assert resp.json() == {"error": "gap", "applied_seq": 3}
        assert _snapshot(match) == before

    def test_start_gap(self, auth_client):
        match, p1 = _make_live_match(started=False)
        resp = _post(
            auth_client(p1.user), "live_start", match,
            {"initial_server": "team1", "seq": 2},
        )
        assert resp.status_code == 409
        assert resp.json() == {"error": "gap", "applied_seq": 0}
        match.refresh_from_db()
        assert match.live_state["started"] is False

    def test_gap_is_checked_before_is_live(self, auth_client):
        match, p1 = _make_live_match()
        Match.all_objects.filter(pk=match.pk).update(is_live=False, live_applied_seq=4)
        resp = _point(auth_client(p1.user), match, seq=9)
        assert resp.status_code == 409
        assert resp.json() == {"error": "gap", "applied_seq": 4}


@pytest.mark.django_db
class TestUndoIsAnOp:
    def test_undo_consumes_a_seq(self, auth_client):
        match, p1 = _make_live_match()
        client = auth_client(p1.user)
        _point(client, match, seq=1)
        resp = _post(client, "live_undo", match, {"seq": 2})
        assert resp.status_code == 200
        assert resp.json()["applied_seq"] == 2
        # The next point is seq 3, not a re-use of 1 or 2.
        assert _point(client, match, seq=3).status_code == 200
        assert _point(client, match, seq=2).status_code == 200  # duplicate
        assert PointEvent.objects.filter(match=match).count() == 1


@pytest.mark.django_db
class TestRejectedOpConsumesItsSeq:
    """A rule rejection is still the op with that seq: the client drops it
    and carries on with the next number, so the seq is consumed."""

    def test_point_before_start_consumes_seq(self, auth_client):
        match, p1 = _make_live_match(started=False)
        client = auth_client(p1.user)
        resp = _point(client, match, seq=1)
        assert resp.status_code == 409
        assert resp.json()["applied_seq"] == 1
        assert _post(
            client, "live_start", match, {"initial_server": "team1", "seq": 2}
        ).status_code == 200

    def test_start_conflict_consumes_seq(self, auth_client):
        match, p1 = _make_live_match(started=False)
        client = auth_client(p1.user)
        _post(client, "live_start", match, {"initial_server": "team1", "seq": 1})
        resp = _post(client, "live_start", match, {"initial_server": "team2", "seq": 2})
        assert resp.status_code == 409
        assert resp.json() == {"error": "Match already started", "applied_seq": 2}


@pytest.mark.django_db
class TestInvalidSeq:
    @pytest.mark.parametrize("bad", [True, False, "3", 0, -1, 1.5, [1], {"n": 1}])
    @pytest.mark.parametrize(
        "name,body",
        [
            ("live_point", {"side": "team1"}),
            ("live_undo", {}),
            ("live_side_switch", {}),
            ("live_start", {"initial_server": "team1"}),
        ],
    )
    def test_rejected_with_400(self, auth_client, name, body, bad):
        match, p1 = _make_live_match(started=(name != "live_start"))
        before = _snapshot(match)
        resp = _post(auth_client(p1.user), name, match, {**body, "seq": bad})
        assert resp.status_code == 400
        assert "seq" in resp.json()["error"]
        assert _snapshot(match) == before


@pytest.mark.django_db
class TestLegacyNoSeq:
    """No ``seq`` is today's contract, unchanged, so a page cached before the
    deploy keeps working. It neither reads nor writes ``live_applied_seq``."""

    def test_point_without_seq_applies_and_leaves_applied_seq_alone(self, auth_client):
        match, p1 = _make_live_match()
        Match.all_objects.filter(pk=match.pk).update(live_applied_seq=3)
        client = auth_client(p1.user)
        assert _point(client, match).status_code == 200
        assert _point(client, match).status_code == 200
        match.refresh_from_db()
        assert match.live_applied_seq == 3
        assert PointEvent.objects.filter(match=match).count() == 2

    def test_legacy_request_is_logged(self, auth_client, caplog):
        match, p1 = _make_live_match()
        with caplog.at_level("INFO", logger="pingpong.views"):
            _point(auth_client(p1.user), match)
        assert any("without seq" in r.getMessage() for r in caplog.records)

    def test_legacy_start_keeps_same_server_idempotency(self, auth_client):
        match, p1 = _make_live_match(started=False)
        client = auth_client(p1.user)
        body = {"initial_server": "team1"}
        assert _post(client, "live_start", match, body).status_code == 200
        assert _post(client, "live_start", match, body).status_code == 200
        assert _post(
            client, "live_start", match, {"initial_server": "team2"}
        ).status_code == 409

    def test_legacy_undo_and_side_switch_accept_an_empty_body(self, auth_client):
        match, p1 = _make_live_match()
        client = auth_client(p1.user)
        _point(client, match)
        url = reverse("pingpong:live_undo", args=[match.pk])
        assert client.post(url).status_code == 200
        url = reverse("pingpong:live_side_switch", args=[match.pk])
        assert client.post(url).status_code == 200

    def test_legacy_error_bodies_are_unchanged(self, auth_client):
        match, p1 = _make_live_match(started=False)
        resp = _point(auth_client(p1.user), match)
        assert resp.status_code == 409
        assert resp.json() == {
            "error": "Match has not started — pick an initial server"
        }
