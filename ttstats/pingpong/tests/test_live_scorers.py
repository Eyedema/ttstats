"""S13.1 / KAN-149 -- per-scorer op streams.

A live match can have more than one phone writing to it. Each phone is a
``LiveScorer`` row with its own ``applied_seq``: seqs are per *scorer*, not per
match, so phone B's first op is seq 1 whatever phone A has sent. One
``select_for_update()`` on the match row still serializes both streams.

A match with no scorer rows is the legacy shape (a match completed before the
migration, or a fixture built straight from ``MatchFactory``): its stream is
``Match.live_applied_seq`` and its only scorer is ``match.scorekeeper``. Those
are covered by ``test_live_seq.py`` / ``test_live_sync.py`` unchanged.
"""
import json
from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from pingpong import live_ops
from pingpong.models import LiveScorer, Match, PointEvent, Side
from pingpong.services import create_live_match

from .conftest import (
    ClubFactory,
    ClubMembershipFactory,
    LiveScorerFactory,
    MatchFactory,
    PlayerFactory,
    UserFactory,
    live_match,
)


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


def _sync(client, match, ops):
    return _post(client, "live_sync", match, {"ops": ops})


def _state(client, match):
    return client.get(reverse("pingpong:live_state", args=[match.pk]))


def _host(match):
    return LiveScorer.objects.get(match=match, role=LiveScorer.Role.HOST)


def _guest(match):
    return LiveScorer.objects.get(match=match, role=LiveScorer.Role.GUEST)


def _shared(auth_client, **kwargs):
    """A started shared match: A (host, side 1) and B (guest, side 2)."""
    match = live_match(started=True, shared=True, **kwargs)
    a, b = _host(match).player, _guest(match).player
    return match, auth_client(a.user), auth_client(b.user)


def _rows(match):
    return list(
        PointEvent.objects.filter(match=match).values_list(
            "sequence", "scoring_side"
        )
    )


def _staff_with_player():
    user = UserFactory(is_staff=True)
    PlayerFactory(user=user)
    return user


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLiveScorerModel:
    def test_one_row_per_player_per_match(self):
        match = live_match()
        host = _host(match)
        with pytest.raises(IntegrityError), transaction.atomic():
            LiveScorerFactory(match=match, player=host.player)

    def test_defaults(self):
        row = LiveScorerFactory()
        assert row.applied_seq == 0
        assert row.can_score_both is False
        assert row.revoked_at is None
        assert row.joined_at is not None

    def test_match_starts_at_rev_zero(self):
        assert live_match().live_rev == 0

    def test_is_shared_counts_non_revoked_scorers(self):
        match = live_match()
        assert match.is_shared is False

        guest = LiveScorerFactory(match=match)
        assert Match.all_objects.get(pk=match.pk).is_shared is True

        LiveScorer.objects.filter(pk=guest.pk).update(revoked_at=timezone.now())
        assert Match.all_objects.get(pk=match.pk).is_shared is False

    def test_rowless_match_is_not_shared(self):
        assert MatchFactory(is_live=True).is_shared is False

    def test_deleting_the_match_deletes_its_scorers(self):
        match = live_match()
        Match.all_objects.filter(pk=match.pk).delete()
        assert not LiveScorer.objects.filter(match_id=match.pk).exists()


# ---------------------------------------------------------------------------
# Creating a live match (the service MatchCreateView and the join flow share)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateLiveMatch:
    def test_service_creates_the_match_and_its_host_row(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        match = create_live_match(
            Match(best_of=3),
            scorekeeper=p1,
            side1_players=[p1],
            side2_players=[p2],
        )
        match = Match.all_objects.get(pk=match.pk)
        assert match.is_live is True
        assert match.scorekeeper == p1
        assert match.live_state["best_of"] == 3
        assert list(match.side1_players) == [p1]
        assert list(match.side2_players) == [p2]

        (row,) = LiveScorer.objects.filter(match=match)
        assert row.player == p1
        assert row.side == Side.ONE
        assert row.role == LiveScorer.Role.HOST
        assert row.applied_seq == 0
        # A solo scorer scores both sides; the join flow passes False.
        assert row.can_score_both is True
        assert row.last_seen_at is not None

    def test_host_row_takes_the_scorekeepers_side(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        match = create_live_match(
            Match(), scorekeeper=p2, side1_players=[p1], side2_players=[p2],
        )
        assert _host(match).side == Side.TWO

    def test_can_score_both_is_the_callers_choice(self):
        p1, p2 = PlayerFactory(), PlayerFactory()
        match = create_live_match(
            Match(), scorekeeper=p1, side1_players=[p1], side2_players=[p2],
            can_score_both=False,
        )
        assert _host(match).can_score_both is False

    def test_an_explicit_club_is_kept(self):
        club = ClubFactory()
        p1, p2 = PlayerFactory(), PlayerFactory()
        match = create_live_match(
            Match(club=club), scorekeeper=p1, side1_players=[p1], side2_players=[p2],
        )
        assert Match.all_objects.get(pk=match.pk).club == club

    def test_match_create_view_creates_the_host_row(self, auth_client):
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        resp = auth_client(p1.user).post(
            reverse("pingpong:match_add"),
            data={
                "player1": p1.pk, "player2": p2.pk, "player3": "", "player4": "",
                "is_double": "False", "date_played": "2026-05-17T12:00",
                "location": "", "match_type": "casual", "best_of": 5,
                "notes": "", "start_live": "1",
            },
        )
        match = Match.all_objects.latest("pk")
        assert resp.url == reverse("pingpong:live_scoreboard", args=[match.pk])
        host = _host(match)
        assert host.player == p1
        assert host.side == Side.ONE
        assert host.can_score_both is True

    def test_a_normal_match_gets_no_scorer_row(self, auth_client):
        p1 = PlayerFactory(with_user=True)
        p2 = PlayerFactory(with_user=True)
        auth_client(p1.user).post(
            reverse("pingpong:match_add"),
            data={
                "player1": p1.pk, "player2": p2.pk, "player3": "", "player4": "",
                "is_double": "False", "date_played": "2026-05-17T12:00",
                "location": "", "match_type": "casual", "best_of": 5, "notes": "",
            },
        )
        assert not LiveScorer.objects.exists()


@pytest.mark.django_db
class TestLiveMatchHelper:
    def test_solo(self):
        match = live_match()
        assert match.is_live is True
        assert match.is_shared is False
        assert match.live_state["started"] is False

    def test_shared_and_started(self):
        match = live_match(shared=True, started=True, initial_server="team2")
        assert match.is_shared is True
        assert match.live_state["initial_server"] == "team2"
        assert _host(match).can_score_both is False
        assert _guest(match).side == Side.TWO


# ---------------------------------------------------------------------------
# scorer_for
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestScorerFor:
    def test_finds_the_players_row(self):
        match = live_match(shared=True)
        guest = _guest(match)
        assert live_ops.scorer_for(match, guest.player) == guest

    def test_none_for_a_player_without_a_row(self):
        match = live_match()
        assert live_ops.scorer_for(match, PlayerFactory()) is None

    def test_returns_a_revoked_row_too(self):
        """The caller decides what revoked means (403 for ops)."""
        match = live_match(shared=True)
        LiveScorer.objects.filter(pk=_guest(match).pk).update(revoked_at=timezone.now())
        match = Match.all_objects.get(pk=match.pk)
        assert live_ops.scorer_for(match, _guest(match).player).revoked_at is not None


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestScorerAuthorization:
    def test_guest_can_open_the_scoreboard(self, auth_client):
        match, _, b = _shared(auth_client)
        resp = b.get(reverse("pingpong:live_scoreboard", args=[match.pk]))
        assert resp.status_code == 200
        assert resp.context["bootstrap"]["me"]["role"] == "guest"

    def test_guest_can_post_ops(self, auth_client):
        match, _, b = _shared(auth_client)
        assert _point(b, match, seq=1, side="team2").status_code == 200

    def test_participant_without_a_row_is_403(self, auth_client):
        match = live_match(started=True)  # solo: only the host has a row
        opponent = match.side2_players.get()
        client = auth_client(opponent.user)
        assert _point(client, match, seq=1).status_code == 403
        assert _sync(client, match, [{"seq": 1, "type": "undo"}]).status_code == 403
        assert _state(client, match).status_code == 403
        assert client.get(
            reverse("pingpong:live_scoreboard", args=[match.pk])
        ).status_code == 403
        assert _rows(match) == []

    @pytest.mark.parametrize(
        "name,body",
        [
            ("live_point", {"side": "team2", "seq": 1}),
            ("live_undo", {"seq": 1}),
            ("live_side_switch", {"seq": 1}),
            ("live_start", {"initial_server": "team1", "seq": 1}),
            ("live_sync", {"ops": [{"seq": 1, "type": "point", "side": "team2"}]}),
        ],
    )
    def test_revoked_scorer_is_403(self, auth_client, name, body):
        match, _, b = _shared(auth_client)
        LiveScorer.objects.filter(pk=_guest(match).pk).update(revoked_at=timezone.now())
        assert _post(b, name, match, body).status_code == 403
        assert _rows(match) == []

    def test_revoked_scorer_cannot_view(self, auth_client):
        match, _, b = _shared(auth_client)
        LiveScorer.objects.filter(pk=_guest(match).pk).update(revoked_at=timezone.now())
        assert _state(b, match).status_code == 403
        assert b.get(
            reverse("pingpong:live_scoreboard", args=[match.pk])
        ).status_code == 403

    def test_stranger_is_403(self, auth_client):
        match = live_match(started=True)
        stranger = PlayerFactory(with_user=True)
        assert _point(auth_client(stranger.user), match, seq=1).status_code == 403

    def test_staff_without_a_row_can_view_a_shared_match(self, client):
        match = live_match(started=True, shared=True)
        client.force_login(_staff_with_player())
        assert _state(client, match).status_code == 200
        resp = client.get(reverse("pingpong:live_scoreboard", args=[match.pk]))
        assert resp.status_code == 200
        assert resp.context["bootstrap"]["me"] is None

    @pytest.mark.parametrize(
        "name,body",
        [
            ("live_point", {"side": "team1", "seq": 1}),
            ("live_undo", {"seq": 1}),
            ("live_side_switch", {"seq": 1}),
            ("live_start", {"initial_server": "team1", "seq": 1}),
            ("live_sync", {"ops": [{"seq": 1, "type": "point", "side": "team1"}]}),
        ],
    )
    def test_staff_without_a_row_cannot_write_to_a_shared_match(self, client, name, body):
        match = live_match(started=True, shared=True)
        client.force_login(_staff_with_player())
        resp = _post(client, name, match, body)
        assert resp.status_code == 409
        assert resp.json() == {"error": "not a scorer"}
        assert _rows(match) == []
        assert Match.all_objects.get(pk=match.pk).live_rev == 0

    def test_staff_without_a_row_still_writes_to_a_solo_match(self, client):
        """Unchanged from before: staff drive the single stream."""
        match = live_match(started=True)
        client.force_login(_staff_with_player())
        assert _point(client, match, seq=1).status_code == 200
        assert _host(match).applied_seq == 1

    def test_cross_club_request_is_404(self, auth_client, settings):
        settings.FEATURE_FLAGS = {"clubs": True}
        home, away = ClubFactory(), ClubFactory()
        match = live_match(started=True, shared=True, club=away)
        guest = _guest(match).player
        # The guest's only club is another one: the match is invisible.
        ClubMembershipFactory(club=home, user=guest.user)
        client = auth_client(guest.user)
        assert _point(client, match, seq=1, side="team2").status_code == 404
        assert _state(client, match).status_code == 404

    def test_same_club_guest_is_let_in(self, auth_client, settings):
        settings.FEATURE_FLAGS = {"clubs": True}
        club = ClubFactory()
        match = live_match(started=True, shared=True, club=club)
        guest = _guest(match).player
        ClubMembershipFactory(club=club, user=guest.user)
        client = auth_client(guest.user)
        assert _point(client, match, seq=1, side="team2").status_code == 200


# ---------------------------------------------------------------------------
# Per-stream seq: the interleaving matrix
# ---------------------------------------------------------------------------

# A: host, side 1, seqs 1..3. B: guest, side 2, seqs 1..2. Every order that
# keeps each stream's own order: C(5, 2) = 10 interleavings.
_A_OPS = [("A", 1), ("A", 2), ("A", 3)]
_B_OPS = [("B", 1), ("B", 2)]


def _interleavings(a, b):
    if not a:
        return [list(b)]
    if not b:
        return [list(a)]
    return [[a[0], *rest] for rest in _interleavings(a[1:], b)] + [
        [b[0], *rest] for rest in _interleavings(a, b[1:])
    ]


_ORDERS = _interleavings(_A_OPS, _B_OPS)


def _order_id(order):
    return "".join(f"{who}{seq}" for who, seq in order)


@pytest.mark.django_db
class TestTwoStreams:
    def test_there_are_ten_orders(self):
        assert len(_ORDERS) == 10

    @pytest.mark.parametrize("order", _ORDERS, ids=_order_id)
    def test_every_interleaving_applies_everything(self, auth_client, order):
        match, a, b = _shared(auth_client)
        clients = {"A": (a, "team1"), "B": (b, "team2")}
        for who, seq in order:
            client, side = clients[who]
            resp = _point(client, match, seq=seq, side=side)
            assert resp.status_code == 200, (who, seq, resp.content)
            assert resp.json()["applied_seq"] == seq

        assert len(_rows(match)) == 5
        assert sorted(s for _, s in _rows(match)) == [1, 1, 1, 2, 2]
        assert _host(match).applied_seq == 3
        assert _guest(match).applied_seq == 2
        assert Match.all_objects.get(pk=match.pk).live_rev == 5

    @pytest.mark.parametrize("order", _ORDERS, ids=_order_id)
    def test_every_resend_is_a_duplicate(self, auth_client, order):
        match, a, b = _shared(auth_client)
        clients = {"A": (a, "team1"), "B": (b, "team2")}
        for who, seq in order:
            client, side = clients[who]
            _point(client, match, seq=seq, side=side)
        before = (_rows(match), Match.all_objects.get(pk=match.pk).live_state)

        for who, seq in order:
            client, side = clients[who]
            resp = _point(client, match, seq=seq, side=side)
            assert resp.status_code == 200
            assert resp.json()["applied_seq"] == (3 if who == "A" else 2)

        assert (_rows(match), Match.all_objects.get(pk=match.pk).live_state) == before
        assert Match.all_objects.get(pk=match.pk).live_rev == 5

    @pytest.mark.parametrize("order", _ORDERS, ids=_order_id)
    def test_guest_gap_is_independent_of_the_host(self, auth_client, order):
        match, a, b = _shared(auth_client)
        clients = {"A": (a, "team1"), "B": (b, "team2")}
        for who, seq in order:
            client, side = clients[who]
            _point(client, match, seq=seq, side=side)

        resp = _point(b, match, seq=4, side="team2")
        assert resp.status_code == 409
        assert resp.json() == {"error": "gap", "applied_seq": 2}
        # A's stream is at 3, which would make B's 4 look applicable if the
        # streams were shared; it is still A's next op that applies.
        assert _point(a, match, seq=4, side="team1").status_code == 200
        assert len(_rows(match)) == 6

    def test_first_op_of_the_second_stream_is_seq_one(self, auth_client):
        match, a, b = _shared(auth_client)
        for seq in range(1, 6):
            _point(a, match, seq=seq)
        resp = _point(b, match, seq=1, side="team2")
        assert resp.status_code == 200
        assert resp.json()["applied_seq"] == 1

    def test_sync_batches_from_both_streams(self, auth_client):
        match, a, b = _shared(auth_client)
        ra = _sync(a, match, [{"seq": i, "type": "point", "side": "team1"} for i in (1, 2, 3)])
        rb = _sync(b, match, [{"seq": i, "type": "point", "side": "team2"} for i in (1, 2)])
        assert ra.json()["applied_seq"] == 3
        assert rb.json()["applied_seq"] == 2
        # Overlapping re-send from B plus one new op: only the new one applies.
        rb2 = _sync(b, match, [{"seq": i, "type": "point", "side": "team2"} for i in (1, 2, 3)])
        assert rb2.json()["applied_seq"] == 3
        assert rb2.json()["rejected"] is None
        assert len(_rows(match)) == 6
        assert Match.all_objects.get(pk=match.pk).live_rev == 6

    def test_sync_gap_reports_the_callers_stream(self, auth_client):
        match, a, b = _shared(auth_client)
        _sync(a, match, [{"seq": i, "type": "point", "side": "team1"} for i in (1, 2, 3)])
        resp = _sync(b, match, [{"seq": 2, "type": "point", "side": "team2"}])
        assert resp.status_code == 409
        assert resp.json() == {"error": "gap", "applied_seq": 0}

    def test_rule_rejection_consumes_only_the_callers_seq(self, auth_client):
        match = live_match(started=False, shared=True)
        a = auth_client(_host(match).player.user)
        b = auth_client(_guest(match).player.user)
        assert _post(a, "live_start", match, {"initial_server": "team1", "seq": 1}).status_code == 200
        # B starts too: the first to arrive won, B's start is rejected...
        resp = _post(b, "live_start", match, {"initial_server": "team2", "seq": 1})
        assert resp.status_code == 409
        assert resp.json() == {"error": "Match already started", "applied_seq": 1}
        # ...and consumed B's seq 1 only.
        assert _guest(match).applied_seq == 1
        assert _host(match).applied_seq == 1
        assert _point(b, match, seq=2, side="team2").status_code == 200


@pytest.mark.django_db
class TestOpsWithoutSeqInASharedMatch:
    @pytest.mark.parametrize(
        "name,body",
        [
            ("live_point", {"side": "team1"}),
            ("live_undo", {}),
            ("live_side_switch", {}),
            ("live_start", {"initial_server": "team1"}),
        ],
    )
    def test_is_a_409_reload(self, auth_client, name, body):
        match, a, _ = _shared(auth_client)
        resp = _post(a, name, match, body)
        assert resp.status_code == 409
        assert resp.json() == {"error": "reload"}
        assert _rows(match) == []

    def test_still_applies_in_a_solo_match_with_a_host_row(self, auth_client):
        match = live_match(started=True)
        client = auth_client(_host(match).player.user)
        assert _point(client, match).status_code == 200
        assert len(_rows(match)) == 1
        # The legacy contract: no seq is consumed.
        assert _host(match).applied_seq == 0


# ---------------------------------------------------------------------------
# The host stream mirrors Match.live_applied_seq for one release
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestHostMirrorsLiveAppliedSeq:
    def test_host_ops_write_both(self, auth_client):
        match = live_match(started=True)
        client = auth_client(_host(match).player.user)
        for seq in (1, 2):
            _point(client, match, seq=seq)
        match.refresh_from_db()
        assert match.live_applied_seq == 2
        assert _host(match).applied_seq == 2

    def test_guest_ops_leave_the_match_column_alone(self, auth_client):
        match, a, b = _shared(auth_client)
        _point(a, match, seq=1)
        _point(b, match, seq=1, side="team2")
        _point(b, match, seq=2, side="team2")
        match.refresh_from_db()
        assert match.live_applied_seq == 1

    def test_a_host_rule_rejection_is_mirrored(self, auth_client):
        match = live_match(started=False)
        client = auth_client(_host(match).player.user)
        resp = _point(client, match, seq=1)  # not started: rejected, consumed
        assert resp.status_code == 409
        match.refresh_from_db()
        assert match.live_applied_seq == 1
        assert _host(match).applied_seq == 1


# ---------------------------------------------------------------------------
# rev
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRev:
    def test_each_applied_op_bumps_rev_by_one(self, auth_client):
        match = live_match(started=False)
        client = auth_client(_host(match).player.user)
        expected = 0
        for name, body in [
            ("live_start", {"initial_server": "team1"}),
            ("live_point", {"side": "team1"}),
            ("live_point", {"side": "team2"}),
            ("live_undo", {}),
            ("live_side_switch", {}),
        ]:
            expected += 1
            resp = _post(client, name, match, {**body, "seq": expected})
            assert resp.status_code == 200, name
            assert resp.json()["rev"] == expected
        assert Match.all_objects.get(pk=match.pk).live_rev == expected

    def test_duplicates_and_gaps_leave_rev_alone(self, auth_client):
        match = live_match(started=True)
        client = auth_client(_host(match).player.user)
        _point(client, match, seq=1)
        assert _point(client, match, seq=1).json()["rev"] == 1
        assert _point(client, match, seq=5).status_code == 409
        assert Match.all_objects.get(pk=match.pk).live_rev == 1

    def test_a_rule_rejection_leaves_rev_alone(self, auth_client):
        """Decision (KAN-149): ``rev`` counts state changes, so a rejection --
        which consumes its seq but changes nothing anyone renders -- does not
        bump it. A poller woken by it would re-fetch an identical state."""
        match = live_match(started=True)
        client = auth_client(_host(match).player.user)
        resp = _post(client, "live_start", match, {"initial_server": "team1", "seq": 1})
        assert resp.status_code == 409
        assert _host(match).applied_seq == 1
        assert Match.all_objects.get(pk=match.pk).live_rev == 0

    def test_a_legacy_op_without_seq_still_bumps_rev(self, auth_client):
        match = live_match(started=True)
        client = auth_client(_host(match).player.user)
        _point(client, match)
        assert Match.all_objects.get(pk=match.pk).live_rev == 1

    def test_the_completing_point_bumps_rev_once_and_survives_game_save(self, auth_client):
        """The game-ending path saves the whole match instance through
        ``Game.save()``; it must not write a stale rev back."""
        match = live_match(best_of=1, started=True)
        client = auth_client(_host(match).player.user)
        ops = [{"seq": i, "type": "point", "side": "team1"} for i in range(1, 12)]
        resp = _sync(client, match, ops)
        assert resp.json()["is_match_complete"] is True
        assert resp.json()["rev"] == 11
        assert Match.all_objects.get(pk=match.pk).live_rev == 11

    def test_rev_is_match_wide(self, auth_client):
        match, a, b = _shared(auth_client)
        _point(a, match, seq=1)
        resp = _point(b, match, seq=1, side="team2")
        assert resp.json()["rev"] == 2


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPayload:
    def test_scorers_and_me(self, auth_client):
        match, a, b = _shared(auth_client)
        _point(a, match, seq=1)
        data = _state(b, match).json()

        host, guest = _host(match), _guest(match)
        assert data["applied_seq"] == 0
        assert data["rev"] == 1
        assert data["me"] == {"side": 2, "role": "guest", "can_score_both": False}
        by_player = {s["player_id"]: s for s in data["scorers"]}
        assert by_player[host.player_id]["applied_seq"] == 1
        assert by_player[host.player_id]["role"] == "host"
        assert by_player[host.player_id]["side"] == 1
        assert by_player[host.player_id]["name"] == host.player.name
        assert by_player[host.player_id]["revoked"] is False
        assert by_player[guest.player_id]["last_seen_at"] is not None

    def test_revoked_scorer_is_listed_as_revoked(self, auth_client):
        match, a, _ = _shared(auth_client)
        LiveScorer.objects.filter(pk=_guest(match).pk).update(revoked_at=timezone.now())
        data = _state(a, match).json()
        assert {s["role"]: s["revoked"] for s in data["scorers"]} == {
            "host": False, "guest": True,
        }

    def test_rowless_match_has_no_scorers_and_no_me(self, auth_client):
        p1 = PlayerFactory(with_user=True)
        match = MatchFactory(player1=p1, is_live=True, scorekeeper=p1)
        data = _state(auth_client(p1.user), match).json()
        assert data["scorers"] == []
        assert data["me"] is None
        assert data["rev"] == 0

    def test_bootstrap_carries_the_callers_stream(self, auth_client):
        match, a, b = _shared(auth_client)
        _point(a, match, seq=1)
        _point(a, match, seq=2)
        resp = b.get(reverse("pingpong:live_scoreboard", args=[match.pk]))
        assert resp.context["bootstrap"]["applied_seq"] == 0
        assert resp.context["bootstrap"]["rev"] == 2

    def test_sync_payload_carries_rev_and_me(self, auth_client):
        match, a, _ = _shared(auth_client)
        data = _sync(a, match, [{"seq": 1, "type": "point", "side": "team1"}]).json()
        assert data["rev"] == 1
        assert data["me"]["role"] == "host"


# ---------------------------------------------------------------------------
# last_seen_at, throttled
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLastSeen:
    def _stale(self, row, seconds=60):
        LiveScorer.objects.filter(pk=row.pk).update(
            last_seen_at=timezone.now() - timedelta(seconds=seconds)
        )

    def test_state_get_bumps_a_stale_last_seen(self, auth_client):
        match, _, b = _shared(auth_client)
        self._stale(_guest(match))
        before = _guest(match).last_seen_at
        _state(b, match)
        assert _guest(match).last_seen_at > before

    def test_an_op_bumps_a_stale_last_seen(self, auth_client):
        match, _, b = _shared(auth_client)
        self._stale(_guest(match))
        before = _guest(match).last_seen_at
        _point(b, match, seq=1, side="team2")
        assert _guest(match).last_seen_at > before

    def test_a_fresh_last_seen_is_not_rewritten(self, auth_client):
        match, _, b = _shared(auth_client)
        self._stale(_guest(match), seconds=2)
        before = _guest(match).last_seen_at
        _state(b, match)
        assert _guest(match).last_seen_at == before

    def test_three_polls_write_once(self, auth_client):
        """The first poll (stale) writes; the next two (within 5 s) don't, so
        each costs exactly one query less than the first."""
        match, _, b = _shared(auth_client)
        self._stale(_guest(match))
        _state(b, match)  # warm the session/user lookups
        self._stale(_guest(match))

        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        counts = []
        for _ in range(3):
            with CaptureQueriesContext(connection) as ctx:
                assert _state(b, match).status_code == 200
            counts.append(len(ctx.captured_queries))
            writes = [
                q["sql"] for q in ctx.captured_queries
                if q["sql"].startswith("UPDATE") and "pingpong_livescorer" in q["sql"]
            ]
            assert len(writes) == (1 if len(counts) == 1 else 0)
        assert counts[1] == counts[2] == counts[0] - 1

    def test_staff_viewing_does_not_mark_the_host_seen(self, client):
        match = live_match(started=True)
        self._stale(_host(match))
        before = _host(match).last_seen_at
        client.force_login(_staff_with_player())
        _state(client, match)
        assert _host(match).last_seen_at == before


# ---------------------------------------------------------------------------
# Query cost with scorer rows
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestQueryCost:
    def test_forty_point_batch_stays_under_the_ceiling(
        self, auth_client, django_assert_max_num_queries
    ):
        """Same ceiling as the row-less ``test_live_sync`` pin: the scorer row
        is read once after the lock and written once per request, not per op."""
        match = live_match(best_of=5, started=False)
        client = auth_client(_host(match).player.user)
        _sync(client, match, [{"seq": 1, "type": "start", "initial_server": "team1"}])
        game = ["team1", "team2"] * 9 + ["team1", "team1"]
        ops = [
            {"seq": 2 + i, "type": "point", "side": s}
            for i, s in enumerate(game * 2)
        ]
        with django_assert_max_num_queries(230):
            resp = _sync(client, match, ops)
        assert resp.status_code == 200
        assert resp.json()["applied_seq"] == 41
        assert _host(match).applied_seq == 41
