"""Pure scoring logic for the Live Scoreboard.

Server-authoritative state lives as JSON on ``Match.live_state``. This
module holds the rules (game-end, match-end, deuce, serve rotation) so the
HTTP view stays a thin shim around them. Pure functions only — no DB IO,
no Django imports — so the rules can be exercised with plain pytest.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

Side = Literal["team1", "team2"]

# Cap the event log to avoid unbounded JSON growth on long matches. 50 is
# more than enough for undo (we only ever need the last event) and gives a
# little audit trail.
MAX_EVENTS = 50


def games_to_win(best_of: int) -> int:
    """Games one side needs to win the match. best_of=5 → 3, best_of=7 → 4."""
    return (best_of // 2) + 1


def other(side: Side) -> Side:
    return "team2" if side == "team1" else "team1"


def initial_state(best_of: int) -> dict:
    """Build a fresh live_state for a new match.

    initial_server is None until the scorekeeper picks who serves first.
    """
    return {
        "best_of": best_of,
        "team1_games": 0,
        "team2_games": 0,
        "team1_points": 0,
        "team2_points": 0,
        "current_game_number": 1,
        "initial_server": None,
        "started": False,
        "side_switched": False,
        "last_point_at": None,
        "events": [],
    }


def set_initial_server(state: dict, server: Side) -> dict:
    state = dict(state)
    state["initial_server"] = server
    state["started"] = True
    return state


def server_for_game(initial_server: Side, game_number: int) -> Side:
    """In table tennis, server alternates between games."""
    if (game_number - 1) % 2 == 0:
        return initial_server
    return other(initial_server)


def current_server(state: dict) -> Side | None:
    """Who's serving the next point given current state.

    Rules:
      - Server alternates each game (game 1 starts with initial_server,
        game 2 with the other side, etc.).
      - Within a game, each player serves 2 consecutive points.
      - Once both sides reach 10 (deuce), each player serves 1 point.
    """
    if not state.get("started") or state.get("initial_server") is None:
        return None

    game_starter = server_for_game(state["initial_server"], state["current_game_number"])
    t1 = state["team1_points"]
    t2 = state["team2_points"]

    if t1 >= 10 and t2 >= 10:
        # First 20 points used 2-per-rotation (10 rotations), then 1 per point.
        rotations = 10 + ((t1 + t2) - 20)
    else:
        rotations = (t1 + t2) // 2

    return game_starter if rotations % 2 == 0 else other(game_starter)


def is_game_won(t1_points: int, t2_points: int) -> Side | None:
    """Table tennis: first to 11 by 2."""
    if t1_points >= 11 and t1_points - t2_points >= 2:
        return "team1"
    if t2_points >= 11 and t2_points - t1_points >= 2:
        return "team2"
    return None


def is_match_complete(state: dict) -> bool:
    target = games_to_win(state["best_of"])
    return state["team1_games"] >= target or state["team2_games"] >= target


def match_winner(state: dict) -> Side | None:
    target = games_to_win(state["best_of"])
    if state["team1_games"] >= target:
        return "team1"
    if state["team2_games"] >= target:
        return "team2"
    return None


def is_deciding_game(state: dict) -> bool:
    """The deciding game = game N of best-of-N (last possible game)."""
    return state["current_game_number"] == state["best_of"]


def should_prompt_side_switch(state: dict) -> bool:
    """ITTF rule (singles): in the deciding game, switch sides when either
    side first reaches 5. We prompt once per match.
    """
    if state.get("side_switched"):
        return False
    if not is_deciding_game(state):
        return False
    return state["team1_points"] >= 5 or state["team2_points"] >= 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply_point(state: dict, side: Side, *, now: str | None = None) -> tuple[dict, dict | None]:
    """Add one point for ``side``. Returns (new_state, completed_game).

    ``completed_game`` is None unless the point ended a game; otherwise it
    carries ``{"game_number": int, "team1_score": int, "team2_score": int,
    "winner": Side}`` so the caller can persist a Game row.

    Caller is responsible for: (a) confirming the match isn't already
    complete, (b) persisting the completed Game row if returned, (c) saving
    the new state.
    """
    if not state.get("started"):
        raise ValueError("Match has not started — initial_server not set")
    if is_match_complete(state):
        raise ValueError("Match is already complete")
    if side not in ("team1", "team2"):
        raise ValueError(f"Invalid side: {side!r}")

    timestamp = now or _now_iso()
    new_state = dict(state)

    # Increment current game's points
    if side == "team1":
        new_state["team1_points"] = state["team1_points"] + 1
    else:
        new_state["team2_points"] = state["team2_points"] + 1

    new_state["last_point_at"] = timestamp

    # Check game-end. Compute first so we can decorate the event log.
    winner = is_game_won(new_state["team1_points"], new_state["team2_points"])
    completed_game = None
    if winner is not None:
        completed_game = {
            "game_number": new_state["current_game_number"],
            "team1_score": new_state["team1_points"],
            "team2_score": new_state["team2_points"],
            "winner": winner,
        }

    # Append to event log (bounded). game_end events carry the final scores
    # so the undo path can rebuild the prior in-progress state.
    event = {
        "side": side,
        "t": timestamp,
        "game": state["current_game_number"],
    }
    if completed_game is not None:
        event["game_end"] = True
        event["final_t1"] = completed_game["team1_score"]
        event["final_t2"] = completed_game["team2_score"]
    events = list(state.get("events", []))
    events.append(event)
    if len(events) > MAX_EVENTS:
        events = events[-MAX_EVENTS:]
    new_state["events"] = events

    if completed_game is not None:
        # Increment game count, reset in-progress, advance game number
        if winner == "team1":
            new_state["team1_games"] = state["team1_games"] + 1
        else:
            new_state["team2_games"] = state["team2_games"] + 1
        new_state["team1_points"] = 0
        new_state["team2_points"] = 0
        new_state["current_game_number"] = state["current_game_number"] + 1

    return new_state, completed_game


def undo_last_point(state: dict) -> tuple[dict, dict | None]:
    """Roll back the most recent point.

    Returns (new_state, undone_game_end) where ``undone_game_end`` is
    ``None`` for a regular point or ``{"game_number": int, "winner": Side}``
    when the undone point had ended a game (caller must delete that Game
    row).

    No-op (returns state unchanged, None) when there are no events to undo.
    """
    events = list(state.get("events", []))
    if not events:
        return dict(state), None

    last = events[-1]
    new_state = dict(state)
    new_state["events"] = events[:-1]

    if last.get("game_end"):
        # The popped point ended a game. Restore the in-progress game's
        # scores from the saved final_t1/final_t2 minus the popped point.
        side = last["side"]
        t1 = last["final_t1"] - (1 if side == "team1" else 0)
        t2 = last["final_t2"] - (1 if side == "team2" else 0)
        new_state["team1_points"] = t1
        new_state["team2_points"] = t2
        new_state["current_game_number"] = last["game"]
        # Decrement the winner's game count
        if side == "team1":
            new_state["team1_games"] = max(0, state["team1_games"] - 1)
        else:
            new_state["team2_games"] = max(0, state["team2_games"] - 1)
        # Don't keep side_switched=True if we backtracked out of the
        # deciding-game-at-5 threshold — but conservatively leave it as-is;
        # re-prompting would be more surprising than not.
        return new_state, {"game_number": last["game"], "winner": side}

    # Regular point — decrement the appropriate side's points
    if last["side"] == "team1":
        new_state["team1_points"] = max(0, state["team1_points"] - 1)
    else:
        new_state["team2_points"] = max(0, state["team2_points"] - 1)
    return new_state, None


def confirm_side_switch(state: dict) -> dict:
    """Mark the deciding-game side switch as done so we don't prompt again."""
    new_state = dict(state)
    new_state["side_switched"] = True
    return new_state
