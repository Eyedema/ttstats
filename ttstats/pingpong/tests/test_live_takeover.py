"""S13.6 / KAN-154 -- presence, takeover and recovery.

* **Takeover**: ``take_over`` / ``release`` ops set / clear
  ``can_score_both`` on the requester's own row. Sequenced in that phone's
  stream (ordered with its points, deduped, consuming the seq), never
  touching the other scorer's row: if that phone comes back, its queued
  points are still applied.
* **Remove a phone**: the host revokes the guest's row (and scores both);
  the removed phone gets 403 ``{"error": "removed"}`` on every live
  endpoint and is sent from the scoreboard page to the match.
* **Undo names its recorder**: ``expects_by`` must match the trailing
  point's ``recorded_by`` (sequences are reused after an undo).
* **Presence**: ``polling`` while a phone holds a long-poll, thresholds in
  the bootstrap, ``server_now`` in the payload.
* **Today** lists the live match for every non-revoked scorer.
"""
import json

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from pingpong import cache_utils, live_ops, views
from pingpong.models import LiveScorer, Match, PointEvent

from .conftest import live_match


def _post(client, name, match, body=None, args=None):
    return client.post(
        reverse(f"pingpong:{name}", args=args or [match.pk]),
        data=json.dumps(body or {}),
        content_type="application/json",
    )


def _sync(client, match, ops):
    return _post(client, "live_sync", match, {"ops": ops})


def _state(client, match, **params):
    return client.get(reverse("pingpong:live_state", args=[match.pk]), params)


def _remove(client, match, player):
    return _post(client, "live_remove_scorer", match, args=[match.pk, player.pk])


def _row(match, role):
    return LiveScorer.objects.get(match=match, role=role)


def _shared(auth_client, **kwargs):
    """A started shared match: host (side 1) and guest (side 2), each with a
    logged-in client."""
    match = live_match(started=True, shared=True, **kwargs)
    host, guest = _row(match, "host").player, _row(match, "guest").player
    return match, (host, auth_client(host.user)), (guest, auth_client(guest.user))


def _rev(match):
    return Match.all_objects.get(pk=match.pk).live_rev


def _points(match):
    return list(
        PointEvent.objects.filter(match=match)
        .order_by("sequence")
        .values_list("sequence", "scoring_side", "recorded_by")
    )


# ---------------------------------------------------------------------------
# take_over / release
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTakeOver:
    def test_take_over_lets_the_requester_score_the_other_side(self, auth_client):
        match, (host, a), (guest, b) = _shared(auth_client)
        refused = _sync(a, match, [{"seq": 1, "type": "point", "side": "team2"}]).json()
        assert refused["rejected"]["error"] == live_ops.NOT_YOUR_SIDE

        data = _sync(a, match, [
            {"seq": 2, "type": "take_over"},
            {"seq": 3, "type": "point", "side": "team2"},
        ]).json()
        assert data["rejected"] is None
        assert data["applied_seq"] == 3
        assert data["me"]["can_score_both"] is True
        assert _row(match, "host").can_score_both is True
        assert _points(match) == [(1, 2, host.pk)]

    def test_the_other_scorer_is_not_revoked_and_its_points_still_land(self, auth_client):
        match, (host, a), (guest, b) = _shared(auth_client)
        _sync(a, match, [{"seq": 1, "type": "take_over"}])
        guest_row = _row(match, "guest")
        assert guest_row.revoked_at is None
        assert guest_row.can_score_both is False
        # The guest's phone comes back with points queued while it was away.
        data = _sync(b, match, [
            {"seq": 1, "type": "point", "side": "team2"},
            {"seq": 2, "type": "point", "side": "team2"},
        ]).json()
        assert data["rejected"] is None
        assert [p[2] for p in _points(match)] == [guest.pk, guest.pk]
        # Both phones' presence says the host scores both.
        by_role = {s["role"]: s for s in data["scorers"]}
        assert by_role["host"]["can_score_both"] is True
        assert by_role["guest"]["can_score_both"] is False

    def test_take_over_consumes_its_seq_and_bumps_rev(self, auth_client):
        match, (_, a), _ = _shared(auth_client)
        before = _rev(match)
        data = _sync(a, match, [{"seq": 1, "type": "take_over"}]).json()
        assert data["applied_seq"] == 1
        assert _rev(match) == before + 1

    def test_a_resent_take_over_is_a_duplicate(self, auth_client):
        match, (_, a), _ = _shared(auth_client)
        _sync(a, match, [{"seq": 1, "type": "take_over"}])
        rev = _rev(match)
        data = _sync(a, match, [{"seq": 1, "type": "take_over"}]).json()
        assert data["applied_seq"] == 1
        assert _rev(match) == rev

    def test_take_over_when_already_scoring_both_uses_the_seq_without_rev(self, auth_client):
        match, (_, a), _ = _shared(auth_client)
        _sync(a, match, [{"seq": 1, "type": "take_over"}])
        rev = _rev(match)
        data = _sync(a, match, [{"seq": 2, "type": "take_over"}]).json()
        assert data["rejected"] is None
        assert data["applied_seq"] == 2
        assert _rev(match) == rev

    def test_either_scorer_may_take_over(self, auth_client):
        match, _, (_, b) = _shared(auth_client)
        data = _sync(b, match, [
            {"seq": 1, "type": "take_over"},
            {"seq": 2, "type": "point", "side": "team1"},
        ]).json()
        assert data["rejected"] is None
        assert _row(match, "guest").can_score_both is True
        assert _row(match, "host").can_score_both is False

    def test_release_restores_the_side_restriction(self, auth_client):
        match, (_, a), _ = _shared(auth_client)
        data = _sync(a, match, [
            {"seq": 1, "type": "take_over"},
            {"seq": 2, "type": "release"},
            {"seq": 3, "type": "point", "side": "team2"},
        ]).json()
        assert data["rejected"] == {"seq": 3, "error": live_ops.NOT_YOUR_SIDE}
        assert data["me"]["can_score_both"] is False

    def test_the_only_scorer_cannot_release(self, auth_client):
        match = live_match(started=True)
        client = auth_client(match.scorekeeper.user)
        data = _sync(client, match, [{"seq": 1, "type": "release"}]).json()
        assert data["rejected"] == {"seq": 1, "error": live_ops.ONLY_SCORER}
        assert data["applied_seq"] == 1
        assert _row(match, "host").can_score_both is True

    def test_a_finished_match_refuses_without_consuming(self, auth_client):
        match, (_, a), _ = _shared(auth_client)
        Match.all_objects.filter(pk=match.pk).update(is_live=False)
        resp = _sync(a, match, [{"seq": 1, "type": "take_over"}])
        assert resp.json()["rejected"]["error"] == live_ops.NOT_LIVE
        assert _row(match, "host").applied_seq == 0

    def test_a_row_less_match_has_no_scorer_to_take_over(self, auth_client):
        match = live_match(started=True)
        LiveScorer.objects.filter(match=match).delete()
        client = auth_client(match.scorekeeper.user)
        data = _sync(client, match, [{"seq": 1, "type": "take_over"}]).json()
        assert data["rejected"] == {"seq": 1, "error": live_ops.NOT_A_SCORER}

    def test_take_over_carries_no_fields(self, auth_client):
        """Validated like any op: a type the endpoint knows, a seq."""
        match, (_, a), _ = _shared(auth_client)
        assert "take_over" in live_ops.OP_TYPES and "release" in live_ops.OP_TYPES
        resp = _sync(a, match, [{"seq": 1, "type": "take_over", "side": "nonsense"}])
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# expects_by
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUndoNamesItsRecorder:
    def test_the_right_sequence_by_the_wrong_phone_is_stale(self, auth_client):
        """B saw A's #1; A undid it and scored again, so #1 is now A's new
        point... and B's undo of "#1 by B" must not delete it."""
        match, (host, a), (guest, b) = _shared(auth_client)
        _sync(b, match, [{"seq": 1, "type": "point", "side": "team2"}])
        # Guest's #1 is undone by the host, who scores their own #1.
        _sync(a, match, [
            {"seq": 1, "type": "undo", "expects": 1, "expects_by": guest.pk},
            {"seq": 2, "type": "point", "side": "team1"},
        ])
        assert _points(match) == [(1, 1, host.pk)]
        data = _sync(b, match, [
            {"seq": 2, "type": "undo", "expects": 1, "expects_by": guest.pk},
        ]).json()
        assert data["rejected"] == {"seq": 2, "error": live_ops.STALE_UNDO}
        assert _points(match) == [(1, 1, host.pk)]

    def test_the_right_pair_applies(self, auth_client):
        match, (host, a), (_, b) = _shared(auth_client)
        _sync(a, match, [{"seq": 1, "type": "point", "side": "team1"}])
        data = _sync(b, match, [
            {"seq": 1, "type": "undo", "expects": 1, "expects_by": host.pk},
        ]).json()
        assert data["rejected"] is None
        assert _points(match) == []

    def test_expects_alone_is_still_honoured(self, auth_client):
        match, (_, a), (_, b) = _shared(auth_client)
        _sync(a, match, [{"seq": 1, "type": "point", "side": "team1"}])
        data = _sync(b, match, [{"seq": 1, "type": "undo", "expects": 1}]).json()
        assert data["rejected"] is None

    def test_the_single_op_endpoint_reads_it_too(self, auth_client):
        match, (host, a), (guest, b) = _shared(auth_client)
        _sync(a, match, [{"seq": 1, "type": "point", "side": "team1"}])
        resp = _post(b, "live_undo", match, {"seq": 1, "expects": 1, "expects_by": guest.pk})
        assert resp.status_code == 409
        assert resp.json()["error"] == live_ops.STALE_UNDO

    @pytest.mark.parametrize("bad", [0, -1, "3", True, 1.5])
    def test_a_malformed_expects_by_is_400(self, auth_client, bad):
        match, (_, a), _ = _shared(auth_client)
        resp = _sync(a, match, [{"seq": 1, "type": "undo", "expects": 1, "expects_by": bad}])
        assert resp.status_code == 400
        assert "expects_by" in resp.json()["error"]

    def test_check_undo_expects_unit(self):
        class Trailing:
            sequence = 4
            recorded_by_id = 7

        class Solo:
            is_shared = False

        check = live_ops.check_undo_expects
        assert check(Solo, {"expects": 4, "expects_by": 7}, Trailing) is None
        assert check(Solo, {"expects": 4, "expects_by": 8}, Trailing) == live_ops.STALE_UNDO
        assert check(Solo, {"expects": 4}, Trailing) is None
        assert check(Solo, {"expects": 3, "expects_by": 7}, Trailing) == live_ops.STALE_UNDO


# ---------------------------------------------------------------------------
# Remove a phone
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRemovePhone:
    def test_the_host_removes_the_guest_and_scores_both(self, auth_client):
        match, (_, a), (guest, _) = _shared(auth_client)
        rev = _rev(match)
        resp = _remove(a, match, guest)
        assert resp.status_code == 200
        data = resp.json()
        assert data["me"]["can_score_both"] is True
        assert _rev(match) == rev + 1
        assert _row(match, "guest").revoked_at is not None
        assert _row(match, "host").can_score_both is True
        assert {s["role"]: s["revoked"] for s in data["scorers"]} == {"host": False, "guest": True}
        # And the host can now score side 2.
        point = _sync(a, match, [{"seq": 1, "type": "point", "side": "team2"}]).json()
        assert point["rejected"] is None

    def test_the_removal_wakes_the_rev_key(self, auth_client, django_capture_on_commit_callbacks):
        match, (_, a), (guest, _) = _shared(auth_client)
        with django_capture_on_commit_callbacks(execute=True):
            _remove(a, match, guest)
        assert cache.get(cache_utils.live_rev_key(match.pk)) == _rev(match)

    def test_the_removed_guest_gets_403_removed_everywhere(self, auth_client):
        match, (_, a), (guest, b) = _shared(auth_client)
        _remove(a, match, guest)
        for resp in (
            _sync(b, match, [{"seq": 1, "type": "point", "side": "team2"}]),
            _post(b, "live_point", match, {"seq": 1, "side": "team2"}),
            _post(b, "live_undo", match, {"seq": 1, "expects": 1}),
            _state(b, match),
            _state(b, match, since=0),
            _state(b, match, since=_rev(match), wait=1),
        ):
            assert resp.status_code == 403
            assert resp.json() == {"error": "removed"}
        assert _row(match, "guest").applied_seq == 0
        assert _points(match) == []

    def test_the_removed_guest_is_sent_from_the_scoreboard_to_today(self, auth_client):
        match, (_, a), (guest, b) = _shared(auth_client)
        _remove(a, match, guest)
        resp = b.get(reverse("pingpong:live_scoreboard", args=[match.pk]), follow=True)
        assert resp.redirect_chain[-1][0] == reverse("pingpong:dashboard")
        assert "You were removed from scoring this match." in resp.content.decode()

    def test_a_removal_during_a_held_poll_is_answered_403(self, auth_client, monkeypatch):
        match, (_, a), (guest, b) = _shared(auth_client)

        def removed_while_waiting(request, m, since):
            LiveScorer.objects.filter(pk=_row(match, "guest").pk).update(revoked_at=timezone.now())
            return True

        monkeypatch.setattr(views.LiveStateView, "_hold", staticmethod(removed_while_waiting))
        resp = _state(b, match, since=_rev(match), wait=1)
        assert resp.status_code == 403
        assert resp.json() == {"error": "removed"}

    def test_only_the_host_may_remove(self, auth_client):
        match, (host, _), (_, b) = _shared(auth_client)
        resp = _remove(b, match, host)
        assert resp.status_code == 403
        assert resp.json() == {"error": "host only"}
        assert _row(match, "host").revoked_at is None

    def test_the_host_cannot_remove_itself_or_a_stranger(self, auth_client):
        match, (host, a), _ = _shared(auth_client)
        assert _remove(a, match, host).status_code == 404
        stranger = live_match().scorekeeper
        assert _remove(a, match, stranger).status_code == 404

    def test_removing_twice_is_harmless(self, auth_client):
        match, (_, a), (guest, _) = _shared(auth_client)
        _remove(a, match, guest)
        rev = _rev(match)
        assert _remove(a, match, guest).status_code == 200
        assert _rev(match) == rev

    def test_a_finished_match_is_not_live(self, auth_client):
        match, (_, a), (guest, _) = _shared(auth_client)
        Match.all_objects.filter(pk=match.pk).update(is_live=False)
        resp = _remove(a, match, guest)
        assert resp.status_code == 409
        assert _row(match, "guest").revoked_at is None

    def test_get_is_not_allowed(self, auth_client):
        match, (_, a), (guest, _) = _shared(auth_client)
        resp = a.get(reverse("pingpong:live_remove_scorer", args=[match.pk, guest.pk]))
        assert resp.status_code == 405

    def test_after_removal_the_match_is_no_longer_shared(self, auth_client):
        match, (_, a), (guest, _) = _shared(auth_client)
        _remove(a, match, guest)
        fresh = Match.all_objects.get(pk=match.pk)
        assert fresh.is_shared is False


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPresence:
    def test_polling_is_true_while_the_phone_holds_a_poll(self, auth_client):
        match, (host, a), (guest, b) = _shared(auth_client)
        data = _state(a, match).json()
        assert {s["role"]: s["polling"] for s in data["scorers"]} == {"host": False, "guest": False}
        cache.set(cache_utils.live_poll_key(match.pk, guest.user_id), 1, 30)
        data = _state(a, match).json()
        assert {s["role"]: s["polling"] for s in data["scorers"]} == {"host": False, "guest": True}

    def test_the_payload_carries_the_server_clock(self, auth_client):
        match, (_, a), _ = _shared(auth_client)
        data = _state(a, match).json()
        assert data["server_now"]

    def test_the_thresholds_come_from_settings_in_the_bootstrap(self, auth_client, settings):
        settings.LIVE_PRESENCE_IN_SYNC = 3
        settings.LIVE_PRESENCE_LOST = 9
        match, (_, a), _ = _shared(auth_client)
        resp = a.get(reverse("pingpong:live_scoreboard", args=[match.pk]))
        config = resp.context["scoreboard_config"]
        assert config["presence"] == {"in_sync_s": 3, "lost_s": 9}
        assert config["remove_url_template"] == reverse(
            "pingpong:live_remove_scorer", args=[match.pk, 0]
        )

    def test_the_defaults_are_ten_seconds_and_two_minutes(self):
        from ttstats.settings import base

        assert base.LIVE_PRESENCE_IN_SYNC == 10
        assert base.LIVE_PRESENCE_LOST == 120

    def test_presence_costs_no_query(self, auth_client, django_assert_num_queries):
        match, _, _ = _shared(auth_client)
        live_ops.load_scorers(match)
        with django_assert_num_queries(0):
            live_ops.presence(match)


# ---------------------------------------------------------------------------
# Today: "You're scoring this match" for every scorer
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTodayForEveryScorer:
    def _today(self, client):
        return client.get(reverse("pingpong:dashboard"))

    def test_both_scorers_see_the_live_match(self, auth_client):
        match, (_, a), (_, b) = _shared(auth_client)
        for client in (a, b):
            resp = self._today(client)
            assert [lm["pk"] for lm in resp.context["live_matches"]] == [match.pk]
            assert "You’re scoring this match" in resp.content.decode()

    def test_the_guest_reads_the_score_from_its_own_side(self, auth_client):
        match, (host, a), (guest, b) = _shared(auth_client)
        _sync(b, match, [{"seq": 1, "type": "point", "side": "team2"}])
        banner = self._today(b).context["live_matches"][0]
        assert banner["team1_points"] == 1  # mine
        assert banner["team2_points"] == 0
        assert banner["opponent"] == host.name
        host_banner = self._today(a).context["live_matches"][0]
        assert host_banner["team1_points"] == 0
        assert host_banner["opponent"] == guest.name

    def test_a_removed_scorer_does_not(self, auth_client):
        match, (_, a), (guest, b) = _shared(auth_client)
        _remove(a, match, guest)
        assert self._today(b).context["live_matches"] == []
        assert len(self._today(a).context["live_matches"]) == 1

    def test_a_row_less_match_still_shows_for_its_scorekeeper(self, auth_client):
        match = live_match(started=True)
        LiveScorer.objects.filter(match=match).delete()
        client = auth_client(match.scorekeeper.user)
        assert [lm["pk"] for lm in self._today(client).context["live_matches"]] == [match.pk]
