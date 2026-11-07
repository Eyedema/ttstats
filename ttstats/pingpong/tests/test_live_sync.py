"""S7.2 / KAN-88 -- the batch sync endpoint, ``POST matches/<pk>/live/sync/``.

After a dead spell the scoreboard may hold dozens of unsent ops; one request
flushes them all, in one transaction under one row lock, through the same
``live_ops.apply_op`` every single-op endpoint uses.
"""
import json
from unittest import mock

import pytest
from django.urls import reverse

from pingpong import live_scoring as ls
from pingpong import point_rollups
from pingpong.models import Game, Match, MatchPointSummary, PointEvent, Side

from .conftest import PlayerFactory
from .test_point_fidelity import (
    _make_live_match,
    deliver,
    generate_ops,
    match_record,
)


def _sync(client, match, ops):
    return client.post(
        reverse("pingpong:live_sync", args=[match.pk]),
        data=json.dumps({"ops": ops}),
        content_type="application/json",
    )


def _numbered(ops, first_seq=1):
    """Script ops (as ``generate_ops`` makes them) with their seqs."""
    return [{**op, "seq": first_seq + i} for i, op in enumerate(ops)]


def _two_games_of_points():
    """40 points: side 1 takes two games 11-9."""
    game = ["team1", "team2"] * 9 + ["team1", "team1"]
    return [{"type": "point", "side": s} for s in game * 2]


@pytest.mark.django_db
class TestFortyPointsInOneBatch:
    def test_two_games_and_forty_rows(self, auth_client):
        match, p1 = _make_live_match(best_of=5)
        client = auth_client(p1.user)
        ops = _numbered([{"type": "start", "initial_server": "team2"}] + _two_games_of_points())

        resp = _sync(client, match, ops)

        assert resp.status_code == 200, resp.content
        data = resp.json()
        assert data["applied_seq"] == 41
        assert data["rejected"] is None
        assert data["state"]["team1_games"] == 2
        assert Game.all_objects.filter(match=match).count() == 2
        events = list(PointEvent.objects.filter(match=match))
        assert [e.sequence for e in events] == list(range(1, 41))

    def test_same_rows_as_the_single_op_path(self, auth_client):
        script = [{"type": "start", "initial_server": "team2"}] + _two_games_of_points()

        single, p1 = _make_live_match(best_of=5)
        deliver(auth_client(p1.user), single, script)

        batched, p2 = _make_live_match(best_of=5)
        assert _sync(auth_client(p2.user), batched, _numbered(script)).status_code == 200

        assert match_record(batched) == match_record(single)

    def test_query_ceiling(self, auth_client, django_assert_max_num_queries):
        match, p1 = _make_live_match(best_of=5)
        client = auth_client(p1.user)
        ops = _numbered(_two_games_of_points(), first_seq=2)
        _sync(client, match, [{"seq": 1, "type": "start", "initial_server": "team1"}])

        # 40 points incl. two game ends: 213 measured. Each point costs ~5
        # (savepoint + release, the sequence MAX, the insert, the state
        # UPDATE) -- linear in the batch, never in the length of the log.
        with django_assert_max_num_queries(230):
            resp = _sync(client, match, ops)
        assert resp.status_code == 200
        assert PointEvent.objects.filter(match=match).count() == 40


@pytest.mark.django_db
class TestPartialSuccess:
    def test_invalid_op_stops_the_batch_and_keeps_the_ones_before(self, auth_client):
        match, p1 = _make_live_match(best_of=5)
        client = auth_client(p1.user)
        ops = _numbered(
            [{"type": "start", "initial_server": "team1"}]
            + [{"type": "point", "side": "team1"}] * 8
            # seq 10: a rule rejection -- the match has already started.
            + [{"type": "start", "initial_server": "team2"}]
            + [{"type": "point", "side": "team2"}] * 3
        )

        resp = _sync(client, match, ops)

        assert resp.status_code == 200
        data = resp.json()
        assert data["rejected"] == {"seq": 10, "error": "Match already started"}
        # The rejected op is consumed: the client drops it and resumes at 11.
        assert data["applied_seq"] == 10
        assert data["state"]["team1_points"] == 8
        assert data["state"]["team2_points"] == 0
        assert PointEvent.objects.filter(match=match).count() == 8

        resumed = _sync(client, match, ops[10:])
        assert resumed.json()["rejected"] is None
        assert resumed.json()["state"]["team2_points"] == 3

    def test_rule_rejection_never_rolls_back_valid_points(self, auth_client):
        match, p1 = _make_live_match(best_of=5)
        client = auth_client(p1.user)
        ops = _numbered([{"type": "point", "side": "team1"}] * 3)  # never started
        resp = _sync(client, match, ops)
        assert resp.json()["rejected"] == {
            "seq": 1, "error": "Match has not started — pick an initial server"
        }
        assert resp.json()["applied_seq"] == 1


@pytest.mark.django_db
class TestCompletionInsideABatch:
    def _match_point_then_two_more(self):
        return _numbered(
            [{"type": "start", "initial_server": "team1"}]
            + [{"type": "point", "side": "team1"}] * 22
            + [{"type": "point", "side": "team2"}] * 2
        )

    def test_completes_once_and_rejects_the_rest(self, auth_client):
        match, p1 = _make_live_match(best_of=3)
        client = auth_client(p1.user)

        with mock.patch.object(
            point_rollups, "rebuild_match_summaries",
            wraps=point_rollups.rebuild_match_summaries,
        ) as rollup:
            resp = _sync(client, match, self._match_point_then_two_more())

        assert resp.status_code == 200
        data = resp.json()
        assert data["is_match_complete"] is True
        assert data["applied_seq"] == 23
        assert data["rejected"] == {"seq": 24, "error": "match complete"}
        assert data["redirect_url"] == reverse("pingpong:match_confirm", args=[match.pk])
        assert rollup.call_count == 1
        assert MatchPointSummary.objects.filter(match=match).count() == 2
        assert PointEvent.objects.filter(match=match).count() == 22
        match.refresh_from_db()
        assert match.winner_side == Side.ONE
        assert Game.all_objects.filter(match=match).count() == 2

    def test_ratings_update_once_on_confirmation(self, auth_client):
        from pingpong.models import RatingChange

        match, p1 = _make_live_match(best_of=3)
        client = auth_client(p1.user)
        _sync(client, match, self._match_point_then_two_more())
        _sync(client, match, self._match_point_then_two_more())

        # The live scorekeeper attests the score; the opponent confirms.
        match = Match.all_objects.get(pk=match.pk)
        client.get(reverse("pingpong:match_confirm", args=[match.pk]))
        opponent = match.side2_players[0]
        auth_client(opponent.user).get(reverse("pingpong:match_confirm", args=[match.pk]))

        match.refresh_from_db()
        assert match.is_confirmed
        per_player = RatingChange.objects.filter(match=match).values_list("player", flat=True)
        assert sorted(set(per_player)) == sorted([p1.pk, opponent.pk])
        before = RatingChange.objects.filter(match=match).count()
        _sync(client, match, self._match_point_then_two_more())
        assert RatingChange.objects.filter(match=match).count() == before

    def test_a_batch_for_a_finished_match_learns_it_finished(self, auth_client):
        match, p1 = _make_live_match(best_of=3)
        client = auth_client(p1.user)
        ops = self._match_point_then_two_more()
        _sync(client, match, ops)
        before = match_record(match)

        resp = _sync(client, match, ops)

        data = resp.json()
        assert resp.status_code == 200
        assert data["is_match_complete"] is True
        assert data["redirect_url"] == reverse("pingpong:match_confirm", args=[match.pk])
        assert data["rejected"] == {"seq": 24, "error": "match complete"}
        assert match_record(match) == before


@pytest.mark.django_db
class TestIdempotentBatches:
    def test_same_batch_twice_changes_nothing(self, auth_client):
        ops, _, _ = generate_ops(seed=5, best_of=5)
        match, p1 = _make_live_match(best_of=5)
        client = auth_client(p1.user)
        first = _sync(client, match, _numbered(ops))
        before = match_record(match)

        second = _sync(client, match, _numbered(ops))

        assert second.status_code == 200
        assert match_record(match) == before
        first_data, second_data = first.json(), second.json()
        assert second_data["applied_seq"] == first_data["applied_seq"] == len(ops)

    def test_interleaved_undos_equal_the_single_op_replay(self, auth_client):
        ops, _, _ = generate_ops(seed=7, best_of=5, undo_probability=0.3)
        assert any(op["type"] == "undo" for op in ops)

        single, p1 = _make_live_match(best_of=5)
        deliver(auth_client(p1.user), single, ops)

        batched, p2 = _make_live_match(best_of=5)
        assert _sync(auth_client(p2.user), batched, _numbered(ops)).status_code == 200

        assert match_record(batched) == match_record(single)

    def test_ops_are_applied_in_seq_order_whatever_the_list_order(self, auth_client):
        script = [{"type": "start", "initial_server": "team1"}] + [
            {"type": "point", "side": s} for s in ("team1", "team2", "team2")
        ]
        match, p1 = _make_live_match()
        resp = _sync(auth_client(p1.user), match, list(reversed(_numbered(script))))
        assert resp.json()["applied_seq"] == 4
        assert [
            Side.to_live(e.scoring_side) for e in PointEvent.objects.filter(match=match)
        ] == ["team1", "team2", "team2"]

    def test_empty_batch_returns_the_canonical_payload(self, auth_client):
        match, p1 = _make_live_match()
        resp = _sync(auth_client(p1.user), match, [])
        assert resp.status_code == 200
        assert resp.json()["applied_seq"] == 0
        assert resp.json()["rejected"] is None


@pytest.mark.django_db
class TestGaps:
    def test_a_batch_starting_past_applied_plus_one_is_a_409_gap(self, auth_client):
        match, p1 = _make_live_match()
        client = auth_client(p1.user)
        _sync(client, match, [{"seq": 1, "type": "start", "initial_server": "team1"}])
        resp = _sync(client, match, [{"seq": 3, "type": "point", "side": "team1"}])
        assert resp.status_code == 409
        assert resp.json() == {"error": "gap", "applied_seq": 1}

    def test_a_hole_inside_a_batch_applies_up_to_it(self, auth_client):
        match, p1 = _make_live_match()
        client = auth_client(p1.user)
        ops = [
            {"seq": 1, "type": "start", "initial_server": "team1"},
            {"seq": 2, "type": "point", "side": "team1"},
            {"seq": 4, "type": "point", "side": "team1"},
        ]
        resp = _sync(client, match, ops)
        assert resp.status_code == 200
        assert resp.json()["applied_seq"] == 2
        assert resp.json()["rejected"] == {"seq": 4, "error": "gap"}
        assert PointEvent.objects.filter(match=match).count() == 1

    def test_duplicates_then_a_gap_is_still_a_409(self, auth_client):
        match, p1 = _make_live_match()
        client = auth_client(p1.user)
        _sync(client, match, [{"seq": 1, "type": "start", "initial_server": "team1"}])
        resp = _sync(client, match, [
            {"seq": 1, "type": "start", "initial_server": "team1"},
            {"seq": 5, "type": "point", "side": "team1"},
        ])
        assert resp.status_code == 409
        assert resp.json() == {"error": "gap", "applied_seq": 1}


@pytest.mark.django_db
class TestValidation:
    @pytest.mark.parametrize(
        "body",
        [
            "not json",
            json.dumps([]),
            json.dumps({}),
            json.dumps({"ops": "nope"}),
            json.dumps({"ops": ["nope"]}),
            json.dumps({"ops": [{"type": "point", "side": "team1"}]}),
            json.dumps({"ops": [{"seq": True, "type": "point", "side": "team1"}]}),
            json.dumps({"ops": [{"seq": "1", "type": "point", "side": "team1"}]}),
            json.dumps({"ops": [{"seq": 0, "type": "point", "side": "team1"}]}),
            json.dumps({"ops": [{"seq": 1, "type": "smash"}]}),
            json.dumps({"ops": [{"seq": 1, "type": "point", "side": "team3"}]}),
            json.dumps({"ops": [{"seq": 1, "type": "point"}]}),
            json.dumps({"ops": [{"seq": 1, "type": "start", "initial_server": "x"}]}),
            json.dumps({"ops": [
                {"seq": 1, "type": "point", "side": "team1"},
                {"seq": 1, "type": "point", "side": "team2"},
            ]}),
        ],
    )
    def test_malformed_batch_is_400_and_applies_nothing(self, auth_client, body):
        match, p1 = _make_live_match()
        Match.all_objects.filter(pk=match.pk).update(
            live_state=ls.set_initial_server(ls.initial_state(5), "team1")
        )
        before = match_record(match)
        resp = auth_client(p1.user).post(
            reverse("pingpong:live_sync", args=[match.pk]),
            data=body,
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "error" in resp.json()
        assert match_record(match) == before

    def test_a_valid_op_before_a_malformed_one_is_not_applied(self, auth_client):
        match, p1 = _make_live_match()
        resp = _sync(auth_client(p1.user), match, [
            {"seq": 1, "type": "start", "initial_server": "team1"},
            {"seq": 2, "type": "point", "side": "nobody"},
        ])
        assert resp.status_code == 400
        match.refresh_from_db()
        assert match.live_state["started"] is False

    def test_more_than_500_ops_is_413(self, auth_client):
        match, p1 = _make_live_match()
        ops = [{"seq": i, "type": "point", "side": "team1"} for i in range(1, 502)]
        resp = _sync(auth_client(p1.user), match, ops)
        assert resp.status_code == 413
        assert PointEvent.objects.filter(match=match).count() == 0

    def test_500_ops_is_accepted(self, auth_client):
        match, p1 = _make_live_match()
        ops = [{"seq": i, "type": "side_switch"} for i in range(1, 501)]
        ops[0] = {"seq": 1, "type": "start", "initial_server": "team1"}
        resp = _sync(auth_client(p1.user), match, ops)
        assert resp.status_code == 200
        assert resp.json()["applied_seq"] == 500


@pytest.mark.django_db
class TestAccess:
    def test_get_is_not_allowed(self, auth_client):
        match, p1 = _make_live_match()
        resp = auth_client(p1.user).get(reverse("pingpong:live_sync", args=[match.pk]))
        assert resp.status_code == 405

    def test_anonymous_is_sent_to_login(self, client):
        match, _ = _make_live_match()
        resp = _sync(client, match, [])
        assert resp.status_code == 302

    def test_non_scorekeeper_is_403(self, auth_client):
        match, _ = _make_live_match()
        other = PlayerFactory(with_user=True)
        resp = _sync(auth_client(other.user), match, [
            {"seq": 1, "type": "start", "initial_server": "team1"},
        ])
        assert resp.status_code == 403
        match.refresh_from_db()
        assert match.live_state["started"] is False

    def test_scoreboard_exposes_the_sync_url(self, auth_client):
        match, p1 = _make_live_match()
        resp = auth_client(p1.user).get(reverse("pingpong:live_scoreboard", args=[match.pk]))
        url = reverse("pingpong:live_sync", args=[match.pk])
        assert f'data-sync-url="{url}"' in resp.content.decode()
