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

KAN-76 extends it to delivery: every generated op script is also sent with
random duplicate re-sends (``TestDuplicateDeliveriesChangeNothing``) and
through ``live_sync`` in four batch splits (``TestBatchSplitsReplayIdentically``),
and must leave exactly the rows the clean single-op delivery leaves, with one
completion. Both have a has-teeth test that removes the seq dedupe.

Runnable standalone::

    cd ttstats && python -m pytest pingpong/tests/test_point_fidelity.py -q
"""
import json
import random

import pytest
from django.urls import reverse

from pingpong import live_scoring as ls
from pingpong import views
from pingpong.models import Game, LiveScorer, Match, PointEvent, Side

from .conftest import MatchFactory, PlayerFactory, live_match

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

        def broken(match, *, state_before, side, completed, new_state, **rest):
            # The bug: name the server from the state after the point.
            return original(
                match,
                state_before=new_state,
                side=side,
                completed=completed,
                new_state=new_state,
                **rest,
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

        def broken(match, *, state_before, side, completed, new_state, **rest):
            # The bug: drop the match's actual starting server.
            return original(
                match,
                state_before={**state_before, "initial_server": "team1"},
                side=side,
                completed=completed,
                new_state=new_state,
                **rest,
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


# ---------------------------------------------------------------------------
# Duplicate deliveries (KAN-77 / KAN-86)
#
# On flaky 4G a request can be applied while its response is lost, and the
# client retries. Every op carries a seq so that a retry is acknowledged and
# never re-applied. The invariant: a sequence delivered with random re-sends
# of already-applied ops -- including the match-completing point -- leaves
# exactly the rows a clean delivery leaves.
# ---------------------------------------------------------------------------

OP_ENDPOINTS = {
    "start": "live_start",
    "point": "live_point",
    "undo": "live_undo",
    "side_switch": "live_side_switch",
}


def generate_ops(seed, best_of, *, undo_probability=0.18, initial_server=None):
    """A scorekeeper's op script, decided by ``live_scoring`` alone.

    Returns ``(ops, oracle_state, expected_servers)``. ``ops[i]`` is the op the
    client numbers ``seq = i + 1``; the first is always the ``start``.
    """
    rng = random.Random(seed)
    if initial_server is None:
        initial_server = "team1" if seed % 2 == 0 else "team2"
    ops = [{"type": "start", "initial_server": initial_server}]
    oracle = ls.set_initial_server(ls.initial_state(best_of), initial_server)
    expected_servers = []

    for _ in range(MAX_OPERATIONS):
        if ls.is_match_complete(oracle):
            break
        if oracle.get("events") and rng.random() < undo_probability:
            ops.append({"type": "undo"})
            oracle, _ = ls.undo_last_point(oracle)
            if expected_servers:
                expected_servers.pop()
            continue
        side = "team1" if rng.random() < 0.5 else "team2"
        ops.append({"type": "point", "side": side})
        expected_servers.append(ls.current_server(oracle))
        oracle, _ = ls.apply_point(oracle, side)

    return ops, oracle, expected_servers


def send_op(client, match, seq, op):
    body = {k: v for k, v in op.items() if k != "type"}
    body["seq"] = seq
    url = reverse(f"pingpong:{OP_ENDPOINTS[op['type']]}", args=[match.pk])
    return _post(client, url, body)


def deliver(client, match, ops, *, duplicate_seed=None, duplicate_probability=0.3,
            check_status=True):
    """Send ``ops`` in order, one request each, with seeded re-sends.

    With ``duplicate_seed`` set, after each op a random already-applied op is
    re-sent with probability ``duplicate_probability``, and the last op -- the
    match point, when the script completes the match -- is always re-sent at
    the end. Returns the re-sends' responses.
    """
    rng = random.Random(duplicate_seed)
    resends = []
    for i, op in enumerate(ops):
        resp = send_op(client, match, i + 1, op)
        if check_status:
            assert resp.status_code == 200, resp.content
        if duplicate_seed is not None and rng.random() < duplicate_probability:
            j = rng.randint(0, i)
            resends.append(send_op(client, match, j + 1, ops[j]))
    if duplicate_seed is not None:
        resends.append(send_op(client, match, len(ops), ops[-1]))
    if check_status:
        for resp in resends:
            assert resp.status_code == 200, resp.content
    return resends


def match_record(match):
    """Everything a delivery could have changed, free of ids and timestamps."""
    m = Match.all_objects.get(pk=match.pk)
    live_state = m.live_state and {
        # Timestamps are wall-clock; everything else must agree. The events
        # themselves are compared without theirs, and without ``by``: two
        # matches compared here have different scorers, so attribution is
        # asserted on its own (``assert_attribution``).
        k: v for k, v in m.live_state.items() if k not in ("events", "last_point_at")
    }
    if live_state is not None:
        live_state["events"] = [
            {k: v for k, v in e.items() if k not in ("t", "by")}
            for e in m.live_state.get("events", [])
        ]
    return {
        "points": list(
            PointEvent.objects.filter(match=m).values_list(
                "sequence", "game_number", "scoring_side", "serving_side",
                "team1_score", "team2_score",
            )
        ),
        "games": list(
            Game.all_objects.filter(match=m).order_by("game_number").values_list(
                "game_number", "team1_score", "team2_score", "winner_side"
            )
        ),
        "match": (
            m.is_live, live_state, m.winner_side,
            m.team1_score_cache, m.team2_score_cache, m.live_applied_seq,
        ),
    }


def assert_same_record(actual, reference):
    assert actual == reference, (
        f"duplicate delivery changed the record:\n actual    {actual}\n"
        f" reference {reference}"
    )


@pytest.mark.django_db
class TestDuplicateDeliveriesChangeNothing:

    @pytest.mark.parametrize("seed", SEEDS)
    def test_generated_sequence_with_resends(self, auth_client, seed):
        best_of = 3 if seed % 3 == 0 else 5
        ops, oracle, expected_servers = generate_ops(seed, best_of)

        clean, p1 = _make_live_match(best_of=best_of)
        deliver(auth_client(p1.user), clean, ops)

        noisy, p2 = _make_live_match(best_of=best_of)
        resends = deliver(auth_client(p2.user), noisy, ops, duplicate_seed=seed)
        assert len(resends) >= 1

        reference = match_record(clean)
        assert_same_record(match_record(noisy), reference)
        assert reference["match"][-1] == len(ops)

        events = list(PointEvent.objects.filter(match=noisy))
        assert_sequence_is_contiguous(events)
        assert_servers_agree(events, expected_servers)
        assert_log_replays_to(events, oracle_state=oracle, best_of=best_of, match=noisy)

    def test_resent_match_point_reports_the_completion(self, auth_client):
        ops, oracle, _ = generate_ops(seed=4, best_of=3, undo_probability=0.0)
        assert ls.is_match_complete(oracle)
        match, p1 = _make_live_match(best_of=3)
        client = auth_client(p1.user)
        deliver(client, match, ops)
        before = match_record(match)

        resp = send_op(client, match, len(ops), ops[-1])

        assert resp.status_code == 200
        data = resp.json()
        assert data["is_match_complete"] is True
        assert data["redirect_url"] == reverse("pingpong:match_confirm", args=[match.pk])
        assert_same_record(match_record(match), before)


@pytest.mark.django_db
class TestTheHarnessCatchesAServerIgnoringSeq:
    """Proof the duplicate harness has teeth: with the dedupe removed, so
    every delivery is applied, the re-sends are detected as double counts."""

    def test_ignored_seq_is_detected(self, auth_client, monkeypatch):
        ops, _, _ = generate_ops(seed=2, best_of=5, undo_probability=0.0)

        clean, p1 = _make_live_match(best_of=5)
        deliver(auth_client(p1.user), clean, ops)

        monkeypatch.setattr(views, "_classify_seq", lambda applied, seq: "apply")
        noisy, p2 = _make_live_match(best_of=5)
        deliver(
            auth_client(p2.user), noisy, ops, duplicate_seed=2, check_status=False
        )

        with pytest.raises(AssertionError, match="duplicate delivery changed"):
            assert_same_record(match_record(noisy), match_record(clean))


# ---------------------------------------------------------------------------
# Batch splits through live_sync (KAN-78 / KAN-90)
#
# However the client happens to chunk its outbox -- one op per request after a
# flaky minute, a few at a time, everything at once after a dead spell, or
# chunks that overlap because an ack was lost -- the server must end up with
# exactly the rows the single-op endpoints produce, and complete the match
# exactly once.
# ---------------------------------------------------------------------------


def _numbered_ops(ops):
    return [{**op, "seq": i + 1} for i, op in enumerate(ops)]


def _sync(client, match, batch):
    resp = _post(client, reverse("pingpong:live_sync", args=[match.pk]), {"ops": batch})
    assert resp.status_code == 200, resp.content
    return resp.json()


def batch_splits(ops, seed):
    """The four ways the harness chunks a script, as lists of batches."""
    numbered = _numbered_ops(ops)
    rng = random.Random(seed)

    random_chunks, i = [], 0
    while i < len(numbered):
        size = rng.randint(1, 12)
        random_chunks.append(numbered[i:i + size])
        i += size

    # Each chunk restarts somewhere at or before the previous chunk's end, as
    # a client does when it cannot tell which of its last ops were acked.
    overlapping, end = [], 0
    while end < len(numbered):
        start = max(0, end - rng.randint(0, 6))
        stop = min(len(numbered), end + rng.randint(1, 12))
        overlapping.append(numbered[start:stop])
        end = stop
    # ...and the whole outbox once more after the match is over.
    overlapping.append(numbered)

    return {
        "one_op_batches": [[op] for op in numbered],
        "random_chunks": random_chunks,
        "one_batch": [numbered],
        "overlapping_resends": overlapping,
    }


def _completions(match):
    """How many times the completion pipeline ran for ``match``."""
    from pingpong.models import MatchPointSummary

    return MatchPointSummary.objects.filter(match=match).count()


@pytest.mark.django_db
class TestBatchSplitsReplayIdentically:

    @pytest.mark.parametrize("seed", SEEDS)
    def test_every_split_matches_the_single_op_path(self, auth_client, mailoutbox, seed):
        from unittest import mock

        from pingpong import point_rollups

        best_of = 3 if seed % 3 == 0 else 5
        ops, oracle, expected_servers = generate_ops(seed, best_of)
        complete = ls.is_match_complete(oracle)

        reference, p0 = _make_live_match(best_of=best_of)
        mail_before = len(mailoutbox)
        deliver(auth_client(p0.user), reference, ops)
        reference_mails = len(mailoutbox) - mail_before
        reference_record = match_record(reference)

        for mode, batches in batch_splits(ops, seed).items():
            match, p1 = _make_live_match(best_of=best_of)
            client = auth_client(p1.user)
            mail_before = len(mailoutbox)
            with mock.patch.object(
                point_rollups, "rebuild_match_summaries",
                wraps=point_rollups.rebuild_match_summaries,
            ) as rollup:
                last = None
                for batch in batches:
                    last = _sync(client, match, batch)

            record = match_record(match)
            assert record == reference_record, (
                f"{mode}: batch delivery diverged from the single-op path:\n"
                f" batch     {record}\n reference {reference_record}"
            )
            events = list(PointEvent.objects.filter(match=match))
            assert_sequence_is_contiguous(events)
            assert_servers_agree(events, expected_servers)
            assert_log_replays_to(events, oracle_state=oracle, best_of=best_of, match=match)

            # Exactly one completion: one rollup write, one round of
            # confirmation mail -- whatever the split.
            assert rollup.call_count == (1 if complete else 0), mode
            assert _completions(match) == (2 if complete else 0), mode
            assert len(mailoutbox) - mail_before == reference_mails, mode
            assert last["applied_seq"] == len(ops), mode
            assert last["is_match_complete"] is complete, mode
            if complete:
                assert last["redirect_url"] == reverse(
                    "pingpong:match_confirm", args=[match.pk]
                ), mode


@pytest.mark.django_db
class TestTheHarnessCatchesABatchServerIgnoringSeq:
    """The batch harness has teeth too: without the dedupe, the overlapping
    re-sends double-count and the comparison fails."""

    def test_ignored_seq_in_batches_is_detected(self, auth_client, monkeypatch):
        ops, _, _ = generate_ops(seed=3, best_of=5, undo_probability=0.0)

        reference, p0 = _make_live_match(best_of=5)
        deliver(auth_client(p0.user), reference, ops)

        monkeypatch.setattr(views, "_classify_seq", lambda applied, seq: "apply")
        match, p1 = _make_live_match(best_of=5)
        client = auth_client(p1.user)
        for batch in batch_splits(ops, seed=3)["overlapping_resends"]:
            _post(client, reverse("pingpong:live_sync", args=[match.pk]), {"ops": batch})

        with pytest.raises(AssertionError, match="duplicate delivery changed"):
            assert_same_record(match_record(match), match_record(reference))


# ---------------------------------------------------------------------------
# Two streams (KAN-150 / KAN-161)
#
# Two phones score one match: A (the host) on side 1, B (the guest) on side 2,
# each with its own seq stream. A generated points script is split by side
# into A's points and B's, and delivered in a seeded interleaving that keeps
# each stream's order, mixed with what two writers really send: undos with a
# correct ``expects`` and with a stale (or missing) one, with and without
# ``expects_by`` (KAN-154: the right sequence recorded by the wrong phone is
# stale too), the losing ``start``, a ``side_switch`` from both, and taps for
# the other side.
#
# The oracle is ``live_scoring`` replaying the same arrival order. Whatever
# the delivery (single-op endpoints with re-sends, or ``live_sync`` in one of
# four batch splits), the rows (side, game, server, scores, ``recorded_by``)
# must be the oracle's, the match completes at most once, and ``rev`` equals
# the number of applied ops. A single-stream replay through the endpoints
# (one solo host, the same arrival order) must leave the same rows too.
# Batches never span the other stream's ops, so arrival order is the
# script's; reordering itself is the documented game-boundary hazard
# (docs/decisions/kan-148-shared-scoring.md), not something to test away.
# ---------------------------------------------------------------------------

TWO_STREAM_SEEDS = list(range(50))
TWO_STREAM_MAX_POINTS = 70
BATCH_MODES = ("one_op_batches", "random_chunks", "one_batch_per_run", "overlapping_resends")
# One delivery per seed, rotating. Five is coprime with the starting server
# (seed % 2) and best-of (seed % 3), so every delivery meets both servers and
# both lengths; each runs on 10 of the 50 seeds. Two deliveries per seed
# would cost ~25 s more for the same coverage of the server code.
DELIVERIES = ("single_op_with_resends",) + BATCH_MODES


def _interleave(first, second, rng):
    """A random merge of two lists that keeps each list's own order."""
    out, i, j = [], 0, 0
    while i < len(first) or j < len(second):
        if j >= len(second) or (i < len(first) and rng.random() < 0.5):
            out.append(first[i])
            i += 1
        else:
            out.append(second[j])
            j += 1
    return out


def two_stream_script(seed, best_of):
    """Ops in arrival order, the oracle's final state, and its rows.

    Each op is ``{"stream", "seq", "type", ..., "applied", "shared_only"}``:
    ``applied`` is the oracle's verdict and ``shared_only`` marks an op whose
    refusal holds only in a shared match (a wrong-side tap, an undo without
    ``expects``), left out of the single-stream replay. Rows are
    ``(game, scoring side, serving side, team1, team2, stream)``.
    """
    rng = random.Random(seed)
    initial_server = "team1" if seed % 2 == 0 else "team2"

    # A points script, split by side into the two streams, then interleaved.
    sides = [rng.choice(("team1", "team2")) for _ in range(TWO_STREAM_MAX_POINTS)]
    stream_a = [s for s in sides if s == "team1"]
    stream_b = [s for s in sides if s == "team2"]
    arrival = _interleave(stream_a, stream_b, rng)

    starter = rng.choice(("A", "B"))
    ops = [{
        "stream": starter, "type": "start", "initial_server": initial_server,
        "applied": True, "shared_only": False,
    }]
    oracle = ls.set_initial_server(ls.initial_state(best_of), initial_server)
    rows = []
    late_start = rng.random() < 0.6
    switched = False

    def emit(stream, op_type, applied, shared_only=False, **fields):
        ops.append({"stream": stream, "type": op_type, "applied": applied,
                    "shared_only": shared_only, **fields})

    for side in arrival:
        if ls.is_match_complete(oracle):
            break
        if late_start and rng.random() < 0.2:
            # The other phone's start lost the race: ALREADY_STARTED.
            emit("B" if starter == "A" else "A", "start", False,
                 initial_server=rng.choice(("team1", "team2")))
            late_start = False
        if ls.should_prompt_side_switch(oracle) and not switched:
            for stream in rng.sample(("A", "B"), 2):
                emit(stream, "side_switch", True)
            oracle = ls.confirm_side_switch(oracle)
            switched = True
        roll = rng.random()
        if oracle["events"] and roll < 0.14:
            stream = rng.choice(("A", "B"))
            kind = rng.random()
            if kind < 0.6:
                # Half name the recorder (``expects_by``), half do not: an
                # older client sends the sequence alone.
                by = {"expects_by": rows[-1][5]} if rng.random() < 0.5 else {}
                emit(stream, "undo", True, expects=len(rows), **by)
                oracle, _ = ls.undo_last_point(oracle)
                rows.pop()
            elif kind < 0.78:
                # The other phone's point landed after this one looked.
                stale = len(rows) - 1 if len(rows) > 1 else len(rows) + 1
                emit(stream, "undo", False, expects=stale)
            elif kind < 0.9:
                # The right sequence, but this phone saw the other phone's
                # point there: it was undone and replaced since (KAN-154).
                other = "B" if rows[-1][5] == "A" else "A"
                emit(stream, "undo", False, expects=len(rows), expects_by=other)
            else:
                emit(stream, "undo", False, shared_only=True)
        elif roll < 0.17:
            stream = rng.choice(("A", "B"))
            emit(stream, "point", False, shared_only=True,
                 side="team2" if stream == "A" else "team1")
        stream = "A" if side == "team1" else "B"
        serving = ls.current_server(oracle)
        game = oracle["current_game_number"]
        oracle, completed = ls.apply_point(oracle, side)
        t1, t2 = (
            (completed["team1_score"], completed["team2_score"]) if completed
            else (oracle["team1_points"], oracle["team2_points"])
        )
        rows.append((game, side, serving, t1, t2, stream))
        emit(stream, "point", True, side=side)

    counters = {"A": 0, "B": 0}
    for op in ops:
        counters[op["stream"]] += 1
        op["seq"] = counters[op["stream"]]
    return ops, oracle, rows


def _wire(op):
    """The op as a client sends it. ``expects_by`` must have been bound to a
    player id first (:func:`_bind_recorders`)."""
    body = {"seq": op["seq"], "type": op["type"]}
    for key in ("side", "initial_server", "expects", "expects_by"):
        if key in op:
            body[key] = op[key]
    return body


def _bind_recorders(ops, players):
    """The script names a stream ("A"/"B") in ``expects_by``; the wire
    carries that stream's player id."""
    return [
        {**op, "expects_by": players[op["expects_by"]].pk} if "expects_by" in op else op
        for op in ops
    ]


def _send_single(client, match, op):
    body = {k: v for k, v in _wire(op).items() if k != "type"}
    return _post(client, reverse(f"pingpong:{OP_ENDPOINTS[op['type']]}", args=[match.pk]), body)


def deliver_two_streams_single(clients, match, ops, rng):
    """Every op through the single-op endpoints, with seeded re-sends."""
    sent = {"A": [], "B": []}
    for op in ops:
        resp = _send_single(clients[op["stream"]], match, op)
        assert resp.status_code == (200 if op["applied"] else 409), (op, resp.content)
        sent[op["stream"]].append(op)
        if rng.random() < 0.25:
            stream = rng.choice([s for s in sent if sent[s]])
            again = rng.choice(sent[stream])
            assert _send_single(clients[stream], match, again).status_code == 200
    for stream, done in sent.items():
        if done:
            assert _send_single(clients[stream], match, done[-1]).status_code == 200


def _sync_until_applied(client, match, batch):
    """Post a batch until its last seq is applied: a rule rejection stops a
    batch after consuming its seq, so the client posts the rest again."""
    last = batch[-1]["seq"]
    for _ in range(len(batch) + 1):
        data = _sync(client, match, batch)
        if data["applied_seq"] >= last:
            return data
    raise AssertionError(f"batch never applied: {batch}")


def two_stream_batches(ops, mode, rng):
    """``[(stream, batch)]`` in delivery order for one of the four splits.

    Batches are cut inside a *run* (consecutive ops of one stream in arrival
    order), so the arrival order is the script's whatever the split.
    """
    runs = []
    for op in ops:
        if runs and runs[-1][0] == op["stream"]:
            runs[-1][1].append(op)
        else:
            runs.append((op["stream"], [op]))

    own = {"A": [], "B": []}  # each stream's ops so far, for overlaps
    batches = []
    for stream, run in runs:
        wired = [_wire(op) for op in run]
        if mode == "one_op_batches":
            batches += [(stream, [w]) for w in wired]
        elif mode == "one_batch_per_run":
            batches.append((stream, wired))
        else:
            i = 0
            while i < len(wired):
                size = rng.randint(1, 8)
                chunk = wired[i:i + size]
                if mode == "overlapping_resends":
                    # Restart up to 4 of this stream's earlier ops back.
                    earlier = own[stream] + wired[:i]
                    chunk = earlier[max(0, len(earlier) - rng.randint(0, 4)):] + chunk
                batches.append((stream, chunk))
                i += size
        own[stream] += wired
    if mode == "overlapping_resends":
        # ...and each whole outbox once more at the end.
        batches += [(stream, own[stream]) for stream in ("A", "B") if own[stream]]
    return batches


def deliver_two_streams_batched(clients, match, ops, mode, rng):
    for stream, batch in two_stream_batches(ops, mode, rng):
        _sync_until_applied(clients[stream], match, batch)


def _shared_match(auth_client, best_of):
    match = live_match(best_of=best_of, shared=True)
    players = {
        "A": LiveScorer.objects.get(match=match, role=LiveScorer.Role.HOST).player,
        "B": LiveScorer.objects.get(match=match, role=LiveScorer.Role.GUEST).player,
    }
    return match, players, {s: auth_client(p.user) for s, p in players.items()}


def _point_rows(match):
    return list(
        PointEvent.objects.filter(match=match).values_list(
            "sequence", "game_number", "scoring_side", "serving_side",
            "team1_score", "team2_score", "recorded_by",
        )
    )


def _without_attribution(state):
    if state is None:
        return None
    out = {k: v for k, v in state.items() if k not in ("events", "last_point_at")}
    out["events"] = [
        {k: v for k, v in e.items() if k not in ("t", "by")} for e in state["events"]
    ]
    return out


def assert_two_stream_record(match, *, ops, oracle, rows, players, mode):
    """The delivered match is what the oracle says the arrival order makes."""
    actual = _point_rows(match)
    expected = [
        (i + 1, game, Side.from_live(side), Side.from_live(serving), t1, t2)
        for i, (game, side, serving, t1, t2, _) in enumerate(rows)
    ]
    assert [r[:6] for r in actual] == expected, (
        f"{mode}: two-stream delivery diverged from the oracle:\n"
        f" actual   {[r[:6] for r in actual]}\n expected {expected}"
    )
    assert [r[6] for r in actual] == [players[r[5]].pk for r in rows], (
        f"{mode}: attribution diverged"
    )

    m = Match.all_objects.get(pk=match.pk)
    complete = ls.is_match_complete(oracle)
    assert m.live_rev == sum(op["applied"] for op in ops), f"{mode}: rev != applied ops"
    for stream, player in players.items():
        scorer = LiveScorer.objects.get(match=m, player=player)
        assert scorer.applied_seq == sum(op["stream"] == stream for op in ops), mode
    expected_state = None if complete else _without_attribution(oracle)
    assert _without_attribution(m.live_state) == expected_state, mode
    assert m.is_live is not complete, mode
    if complete:
        assert m.winner_side == Side.from_live(ls.match_winner(oracle)), mode
    assert _completions(m) == (2 if complete else 0), mode


@pytest.mark.django_db
class TestTwoStreams:

    @pytest.mark.parametrize("seed", TWO_STREAM_SEEDS)
    def test_interleaved_streams_match_the_oracle(self, auth_client, seed):
        """Single-op delivery with re-sends or one of the four batch splits,
        rotating with the seed (``DELIVERIES``)."""
        from unittest import mock

        from pingpong import point_rollups

        best_of = 3 if seed % 3 else 5
        ops, oracle, rows = two_stream_script(seed, best_of)
        delivery = DELIVERIES[seed % len(DELIVERIES)]

        match, players, clients = _shared_match(auth_client, best_of)
        ops = _bind_recorders(ops, players)
        rng = random.Random(seed)
        with mock.patch.object(
            point_rollups, "rebuild_match_summaries",
            wraps=point_rollups.rebuild_match_summaries,
        ) as rollup:
            if delivery == "single_op_with_resends":
                deliver_two_streams_single(clients, match, ops, rng)
            else:
                deliver_two_streams_batched(clients, match, ops, delivery, rng)
        assert_two_stream_record(
            match, ops=ops, oracle=oracle, rows=rows, players=players, mode=delivery
        )
        # Exactly one completion, whatever the delivery.
        assert rollup.call_count == (1 if ls.is_match_complete(oracle) else 0), delivery

    def test_every_delivery_meets_both_servers_and_both_lengths(self):
        seen = {
            (DELIVERIES[s % len(DELIVERIES)], s % 2, 3 if s % 3 else 5)
            for s in TWO_STREAM_SEEDS
        }
        assert seen == {(d, p, b) for d in DELIVERIES for p in (0, 1) for b in (3, 5)}

    @pytest.mark.parametrize("seed", TWO_STREAM_SEEDS[::7])
    def test_a_single_stream_replay_of_the_arrival_order_leaves_the_same_rows(
        self, auth_client, seed
    ):
        best_of = 3 if seed % 3 else 5
        ops, _, _ = two_stream_script(seed, best_of)

        shared, players, clients = _shared_match(auth_client, best_of)
        deliver_two_streams_batched(
            clients, shared, _bind_recorders(ops, players), "one_batch_per_run",
            random.Random(seed),
        )

        # A solo host records every point itself, so a stale ``expects_by``
        # stays stale with its recorder named as the host, and a correct one
        # becomes the host.
        solo = live_match(best_of=best_of)
        host = solo.scorekeeper.pk
        single = [
            {**_wire({**op, "expects_by": host} if op.get("applied") and "expects_by" in op
                     else {**op, "expects_by": host + 10_000} if "expects_by" in op
                     else op),
             "seq": i + 1}
            for i, op in enumerate(o for o in ops if not o["shared_only"])
        ]
        _sync_until_applied(auth_client(solo.scorekeeper.user), solo, single)

        assert [r[:6] for r in _point_rows(solo)] == [r[:6] for r in _point_rows(shared)]
        assert match_record(solo)["games"] == match_record(shared)["games"]

    def test_the_scripts_exercise_every_two_writer_case(self):
        seen = set()
        for seed in TWO_STREAM_SEEDS:
            ops, oracle, rows = two_stream_script(seed, 3 if seed % 3 else 5)
            for op in ops:
                if op["type"] == "undo":
                    if op["applied"]:
                        seen.add("undo applied")
                        if "expects_by" in op:
                            seen.add("undo applied with expects_by")
                    elif "expects_by" in op:
                        seen.add("stale expects_by")
                    elif "expects" in op:
                        seen.add("stale expects")
                    else:
                        seen.add("undo without expects")
                elif op["type"] == "start" and not op["applied"]:
                    seen.add("second start")
                elif op["type"] == "side_switch":
                    seen.add("side switch from both")
                elif op["type"] == "point" and not op["applied"]:
                    seen.add("wrong side")
            if ls.is_match_complete(oracle):
                seen.add("complete")
            if ops[0]["stream"] == "B":
                seen.add("guest started")
            if any(r[0] > 1 for r in rows):
                seen.add("second game")
        assert seen == {
            "undo applied", "undo applied with expects_by", "stale expects",
            "stale expects_by", "undo without expects", "second start",
            "side switch from both", "wrong side", "complete", "guest started",
            "second game",
        }


@pytest.mark.django_db
class TestTheTwoStreamHarnessCatchesBrokenWriters:
    """Proof the two-stream harness has teeth: each broken writer below is
    detected by ``assert_two_stream_record``."""

    SEED = 5

    # A seed carrying stale ``expects_by`` undos (KAN-154).
    BY_SEED = 3

    def _deliver_unchecked(self, auth_client, seed=None, *, name_recorders=True):
        """Deliver a seed (``SEED`` by default) as one-op batches without
        waiting for a rejection: a broken server may apply what should be
        refused. ``name_recorders=False`` sends no ``expects_by``, for a
        canary that breaks attribution (which ``expects_by`` reads)."""
        seed = self.SEED if seed is None else seed
        best_of = 3 if seed % 3 else 5
        ops, oracle, rows = two_stream_script(seed, best_of)
        match, players, clients = _shared_match(auth_client, best_of)
        if name_recorders:
            ops = _bind_recorders(ops, players)
        else:
            # Only sound for a seed whose expects_by undos all apply: without
            # the recorder a stale one would apply.
            assert not any("expects_by" in op and not op["applied"] for op in ops)
            ops = [{k: v for k, v in op.items() if k != "expects_by"} for op in ops]
        for stream, batch in two_stream_batches(ops, "one_op_batches", random.Random(seed)):
            _post(clients[stream], reverse("pingpong:live_sync", args=[match.pk]), {"ops": batch})
        return dict(match=match, ops=ops, oracle=oracle, rows=rows, players=players)

    def test_a_writer_dropping_recorded_by_is_detected(self, auth_client, monkeypatch):
        original = views._record_point_event

        def broken(match, **kwargs):
            kwargs["recorded_by_id"] = None
            return original(match, **kwargs)

        monkeypatch.setattr(views, "_record_point_event", broken)
        delivered = self._deliver_unchecked(auth_client, name_recorders=False)
        with pytest.raises(AssertionError, match="attribution diverged"):
            assert_two_stream_record(mode="canary", **delivered)

    def test_an_undo_ignoring_expects_is_detected(self, auth_client, monkeypatch):
        from pingpong import live_ops

        monkeypatch.setattr(live_ops, "check_undo_expects", lambda match, op, trailing: None)
        delivered = self._deliver_unchecked(auth_client)
        assert any(
            op["type"] == "undo" and "expects" in op and not op["applied"]
            for op in delivered["ops"]
        ), "the canary seed must carry a stale expects"
        with pytest.raises(AssertionError, match="diverged from the oracle"):
            assert_two_stream_record(mode="canary", **delivered)

    def test_an_undo_ignoring_expects_by_is_detected(self, auth_client, monkeypatch):
        """KAN-154: a check that compares the sequence only lets an undo
        through that names the other phone's point at the right sequence."""
        from pingpong import live_ops

        original = live_ops.check_undo_expects
        monkeypatch.setattr(
            live_ops, "check_undo_expects",
            lambda match, op, trailing: original(match, {**op, "expects_by": None}, trailing),
        )
        delivered = self._deliver_unchecked(auth_client, self.BY_SEED)
        assert any(
            op["type"] == "undo" and "expects_by" in op and not op["applied"]
            for op in delivered["ops"]
        ), "the canary seed must carry a stale expects_by"
        with pytest.raises(AssertionError, match="diverged from the oracle"):
            assert_two_stream_record(mode="canary", **delivered)

    def test_the_unbroken_server_passes_the_same_check(self, auth_client):
        """The control: the canaries fail because of the break, not the setup."""
        assert_two_stream_record(mode="control", **self._deliver_unchecked(auth_client))
