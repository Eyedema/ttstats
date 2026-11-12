"""Golden vectors for the live-scoring rules (KAN-91).

Runs seeded op sequences through ``pingpong.live_scoring`` and records what
the rules say after every op. ``static/pingpong/js/live_rules.js`` is a mirror
of that module for the offline scoreboard; ``tests/js/live_rules.test.mjs``
replays these vectors through it and demands identical state after every op,
and ``test_live_vectors.py`` regenerates them in memory and fails if the
committed file is stale. Pure: no Django, no DB.

Regenerate after changing ``live_scoring.py``::

    cd ttstats && ../.venv/bin/python -m pingpong.tests.live_vectors

File format (one sequence per line, so a diff names the sequences that moved):

- ``config``: ``live_scoring.rules_config()``; the JS engine is built from it.
- ``state_keys``: every ``live_state`` key except ``events``.
- each sequence: ``name``, ``best_of``, ``ops`` and ``steps``, where
  ``steps[0]`` describes ``initial_state(best_of)`` and ``steps[i + 1]``
  follows ``ops[i]``. Ops are ``["start", side]``, ``["point", side, now]``,
  ``["undo"]`` and ``["switch"]``; ``now`` is an opaque timestamp string.
  A point recorded with an author (KAN-150) is ``["point", side, now, by]``,
  ``by`` an opaque player id the event carries as ``"by"``.
- each step describes a flat record: the state's ``state_keys`` plus ``srv``
  (``current_server``), ``sw`` (``should_prompt_side_switch``), ``done``
  (``is_match_complete``), ``win`` (``match_winner``) and ``r`` (the op's
  second return value -- ``completed_game`` / ``undone_game_end`` -- or
  ``{"error": message}`` when the op raised, leaving the state unchanged).
  To keep the file small a step lists only the keys whose value differs from
  the previous record, and ``e`` = ``[drop, keep, push]`` when the events
  changed: ``events = previous[drop:drop + keep] + push``. Both are plain
  list/dict deltas and encode no rule.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

from pingpong import live_scoring as ls

VECTORS_PATH = Path(__file__).resolve().parent / "fixtures" / "live_vectors.json"
REGENERATE_COMMAND = "cd ttstats && ../.venv/bin/python -m pingpong.tests.live_vectors"

STATE_KEYS = [k for k in ls.initial_state(1) if k != "events"]
SIDES = ("team1", "team2")
BEST_OFS = (1, 3, 5, 7)


_MISSING = object()


def _same(a, b) -> bool:
    """Equal as JSON: ``False == 0`` in Python, but not in the file."""
    return type(a) is type(b) and a == b


def _events_delta(prev: list, new: list) -> list:
    """``[drop, keep, push]`` with ``new == prev[drop:drop + keep] + push``."""
    best = None
    for drop in range(min(len(prev), 2) + 1):
        tail = prev[drop:]
        keep = 0
        while keep < len(tail) and keep < len(new) and tail[keep] == new[keep]:
            keep += 1
        candidate = [drop, keep, new[keep:]]
        if best is None or len(candidate[2]) < len(best[2]):
            best = candidate
    return best


class Recorder:
    """Applies ops to a live_state and records each resulting step."""

    def __init__(self, name: str, best_of: int):
        self.name = name
        self.best_of = best_of
        self.state = ls.initial_state(best_of)
        self.ops: list = []
        self.steps: list = []
        self.clock = 0
        self._prev_flat: dict = {}
        self._record(None)

    # -- recording -----------------------------------------------------------
    def _record(self, result, prev_events=None):
        state = self.state
        flat = {k: state[k] for k in STATE_KEYS}
        flat.update(
            srv=ls.current_server(state),
            sw=ls.should_prompt_side_switch(state),
            done=ls.is_match_complete(state),
            win=ls.match_winner(state),
            r=result,
        )
        step = {k: v for k, v in flat.items() if not _same(self._prev_flat.get(k, _MISSING), v)}
        prev_events = prev_events or []
        if not self.steps or state["events"] != prev_events:
            step["e"] = _events_delta(prev_events, state["events"])
        self._prev_flat = flat
        self.steps.append(step)

    def _do(self, op, fn):
        prev_events = self.state["events"]
        try:
            self.state, result = fn()
        except ValueError as exc:
            result = {"error": str(exc)}
        self.ops.append(op)
        self._record(result, prev_events)
        return result

    # -- ops -----------------------------------------------------------------
    def start(self, side):
        return self._do(["start", side], lambda: (ls.set_initial_server(self.state, side), None))

    def point(self, side, by=None):
        self.clock += 1
        now = f"t{self.clock}"
        if by is None:
            return self._do(["point", side, now], lambda: ls.apply_point(self.state, side, now=now))
        return self._do(
            ["point", side, now, by], lambda: ls.apply_point(self.state, side, now=now, by=by)
        )

    def undo(self):
        return self._do(["undo"], lambda: ls.undo_last_point(self.state))

    def switch(self):
        return self._do(["switch"], lambda: (ls.confirm_side_switch(self.state), None))

    # -- helpers -------------------------------------------------------------
    @property
    def complete(self):
        return ls.is_match_complete(self.state)

    def points(self):
        return self.state["team1_points"], self.state["team2_points"]

    def to_dict(self):
        return {"name": self.name, "best_of": self.best_of, "ops": self.ops, "steps": self.steps}


# -- scenarios ---------------------------------------------------------------


def _rally(rec: Recorder, rng: random.Random, bias: float, undo_rate: float, switch_rate: float):
    """One scorekeeper action: usually a point, sometimes undo(s) or a switch."""
    if rec.state["started"] and ls.should_prompt_side_switch(rec.state) and rng.random() < switch_rate:
        rec.switch()
        return
    roll = rng.random()
    if roll < undo_rate:
        for _ in range(rng.choice((1, 1, 1, 2, 3))):
            rec.undo()
        return
    rec.point("team1" if rng.random() < bias else "team2")


def random_match(name, rng, best_of, server, *, max_ops=None, undo_rate=0.08):
    rec = Recorder(name, best_of)
    rec.start(server)
    bias = rng.uniform(0.3, 0.7)
    switch_rate = rng.choice((0.0, 0.5, 1.0))
    limit = max_ops if max_ops is not None else 10_000
    while not rec.complete and len(rec.ops) < limit:
        _rally(rec, rng, bias, undo_rate, switch_rate)
    if rec.complete and rng.random() < 0.5:
        # After the match point: a stray tap is refused, and undo reopens it.
        rec.point(rng.choice(SIDES))
        rec.undo()
        rec.point(rng.choice(SIDES))
    return rec


def deuce_marathon(name, rng, best_of, server):
    rec = Recorder(name, best_of)
    rec.start(server)
    games = rng.randint(1, ls.games_to_win(best_of))
    for _ in range(games):
        if rec.complete:
            break
        # Reach DEUCE_AT-all, then trade points for a long while.
        while rec.points() != (ls.DEUCE_AT, ls.DEUCE_AT):
            t1, t2 = rec.points()
            rec.point("team1" if t1 < ls.DEUCE_AT and (t2 >= ls.DEUCE_AT or rng.random() < 0.5) else "team2")
        for _ in range(rng.randint(6, 40)):
            t1, t2 = rec.points()
            side = "team1" if t1 <= t2 else "team2"
            rec.point(side)
            if rng.random() < 0.1:
                rec.undo()
        rec.point(rng.choice(SIDES))
        while not rec.complete and rec.points() != (0, 0):
            rec.point(rng.choice(SIDES))
    return rec


def undo_across_game_ends(name, rng, best_of, server):
    rec = Recorder(name, best_of)
    rec.start(server)
    bias = rng.uniform(0.35, 0.65)
    undone = set()
    while not rec.complete:
        result = rec.point("team1" if rng.random() < bias else "team2")
        # Undo each game's end once (several points deep), then let it stand.
        if result and "game_number" in result and result["game_number"] not in undone:
            undone.add(result["game_number"])
            for _ in range(rng.randint(1, 4)):
                rec.undo()
    if rng.random() < 0.5:
        # Undo out of a finished match, across its last game end.
        rec.undo()
    return rec


def undo_past_cap(name, rng, best_of, server):
    """More undos than the capped event log holds: the extra ones are no-ops."""
    rec = Recorder(name, best_of)
    rec.start(server)
    target = ls.MAX_EVENTS + rng.randint(5, 40)
    while len(rec.ops) < target and not rec.complete:
        rec.point(rng.choice(SIDES))
    for _ in range(ls.MAX_EVENTS + rng.randint(2, 10)):
        rec.undo()
    for _ in range(rng.randint(5, 30)):
        if rec.complete:
            break
        rec.point(rng.choice(SIDES))
    return rec


def deciding_game_switch(name, rng, best_of, server):
    """Reach the deciding game, take (or ignore) the switch, undo back out of it."""
    rec = Recorder(name, best_of)
    rec.start(server)
    # Alternate game wins so neither side wins early.
    for game in range(best_of - 1):
        side = SIDES[game % 2]
        loser = ls.other(side)
        while True:
            # The loser may score, but never get close enough to take the game.
            near = rec.state[f"{loser}_points"] >= ls.WIN_POINTS - 3
            result = rec.point(side if near or rng.random() < 0.8 else loser)
            if result and "game_number" in result:
                break
    mode = rng.choice(("confirm", "ignore", "early"))
    if mode == "early":
        rec.switch()
    while not rec.complete:
        if ls.should_prompt_side_switch(rec.state) and mode == "confirm":
            rec.switch()
        rec.point(rng.choice(SIDES))
        if rng.random() < 0.05:
            rec.undo()
    for _ in range(rng.randint(1, 25)):
        rec.undo()
    return rec


def odd_ops(name, rng, best_of, server):
    """Ops the rules refuse or ignore: before start, bad side, after the end."""
    rec = Recorder(name, best_of)
    rec.point(rng.choice(SIDES))
    rec.undo()
    if rng.random() < 0.5:
        rec.switch()
    rec.start(server)
    rec.point("team3")
    rec.undo()
    while not rec.complete:
        rec.point(rng.choice(SIDES))
    rec.point(rng.choice(SIDES))
    rec.switch()
    return rec


def attributed(name, rng, best_of, server):
    """Two scorers (KAN-150): every point carries ``by``, the id of whoever
    tapped it -- usually the scoring side's own phone, sometimes a solo host
    scoring both. Undos cross game ends with attributed events."""
    rec = Recorder(name, best_of)
    rec.start(server)
    phones = {"team1": rng.randint(1, 500), "team2": rng.randint(501, 1000)}
    bias = rng.uniform(0.35, 0.65)
    while not rec.complete:
        side = "team1" if rng.random() < bias else "team2"
        by = phones[side] if rng.random() < 0.8 else phones[ls.other(side)]
        result = rec.point(side, by=by)
        if result and "game_number" in result and rng.random() < 0.5:
            for _ in range(rng.randint(1, 3)):
                rec.undo()
        elif rng.random() < 0.05:
            rec.undo()
    if rng.random() < 0.5:
        rec.undo()
    return rec


def generate() -> dict:
    sequences = []
    scenarios = (
        ("random", random_match, 44),
        ("deuce", deuce_marathon, 6),
        ("undo_game_end", undo_across_game_ends, 4),
        ("undo_past_cap", undo_past_cap, 4),
        ("deciding_switch", deciding_game_switch, 4),
        ("odd_ops", odd_ops, 2),
        # Appended, so every sequence above keeps its seed and its bytes.
        ("attributed", attributed, 2),
    )
    seed = 0
    for label, build, per_combo in scenarios:
        for best_of in BEST_OFS:
            for server in SIDES:
                for i in range(per_combo):
                    seed += 1
                    rng = random.Random(seed)
                    name = f"{label}-bo{best_of}-{server}-{i}"
                    kwargs = {}
                    if build is random_match and i % 5:
                        kwargs["max_ops"] = rng.randint(5, 40)
                    sequences.append(build(name, rng, best_of, server, **kwargs).to_dict())
    return {
        "regenerate": REGENERATE_COMMAND,
        "config": ls.rules_config(),
        "state_keys": STATE_KEYS,
        "sequences": sequences,
    }


def render(data: dict) -> str:
    """Compact JSON, one sequence per line."""
    dump = lambda obj: json.dumps(obj, separators=(",", ":"), sort_keys=False)  # noqa: E731
    head = ",".join(f"{json.dumps(k)}:{dump(v)}" for k, v in data.items() if k != "sequences")
    body = ",\n".join(dump(seq) for seq in data["sequences"])
    return "{" + head + ',"sequences":[\n' + body + "\n]}\n"


def main() -> int:
    VECTORS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = generate()
    VECTORS_PATH.write_text(render(data))
    steps = sum(len(s["steps"]) for s in data["sequences"])
    print(f"Wrote {len(data['sequences'])} sequences, {steps} steps to {VECTORS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
