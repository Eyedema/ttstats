"""The fake server's contract (KAN-165): scripts run through the REAL endpoints.

``tests/js/live_fakes.mjs`` models ``live_sync`` and ``/live/state/`` for the
scoreboard client's tests (per-stream seqs, ``rev``, side restriction,
``expects``, the long-poll). A fake that drifts from the server makes those
tests prove nothing, so the same op scripts are run here through the real
endpoints and the normalised answers are committed as
``fixtures/live_contract.json``. ``tests/js/live_contract.test.mjs`` replays
every script through the fake and demands the same answers;
``test_live_contract.py`` regenerates the file in memory and fails if the
committed one is stale.

Regenerate after changing the live endpoints (needs the test database, so it
runs through pytest)::

    cd ttstats && ../.venv/bin/python -m pingpong.tests.live_contract

A script is ``{"name", "best_of", "shared", "steps"}``; a step is
``{"as": "host" | "guest", "call": "sync", "ops": [...]}`` or
``{"as", "call": "state", "since": int | "rev" | None, "wait": bool}``
(``"rev"``: the match's current rev, read before the call). No step holds a
long-poll: a held wait is timing, and is covered by ``test_live_longpoll.py``
on the server and by the fake's own tests.

Normalisation (also in the JS test, keep them in step): timestamps are
dropped (``last_point_at``, an event's ``t``, ``last_seen_at``), player ids
become the scorer's role (an event's and ``last_point``'s ``by``, the
``scorers`` rows), ``redirect_url`` becomes ``"confirm"``/``"detail"``, and
``csrf_token`` is dropped.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

CONTRACT_PATH = Path(__file__).resolve().parent / "fixtures" / "live_contract.json"
REGENERATE_COMMAND = "cd ttstats && ../.venv/bin/python -m pingpong.tests.live_contract"
WRITE_ENV = "LIVE_CONTRACT_WRITE"


def _points(side, first_seq, n):
    return [{"seq": first_seq + i, "type": "point", "side": side} for i in range(n)]


SCRIPTS = [
    {
        "name": "shared_two_streams",
        "best_of": 1,
        "shared": True,
        "steps": [
            {"as": "host", "call": "state", "since": None},
            {"as": "host", "call": "state", "since": "rev"},
            {"as": "host", "call": "state", "since": "not-a-number"},
            # The guest starts; the host's start loses (consumes its seq).
            {"as": "guest", "call": "sync", "ops": [{"seq": 1, "type": "start", "initial_server": "team2"}]},
            {"as": "host", "call": "sync", "ops": [{"seq": 1, "type": "start", "initial_server": "team1"}]},
            {"as": "host", "call": "state", "since": 0},
            # Side restriction: the host may not score for side 2.
            {"as": "host", "call": "sync", "ops": [{"seq": 2, "type": "point", "side": "team2"}]},
            {"as": "host", "call": "sync", "ops": _points("team1", 3, 2)},
            {"as": "guest", "call": "sync", "ops": _points("team2", 2, 1)},
            # Conditional undo: stale expects, then missing expects, then right.
            {"as": "host", "call": "sync", "ops": [{"seq": 5, "type": "undo", "expects": 2}]},
            {"as": "host", "call": "sync", "ops": [{"seq": 6, "type": "undo"}]},
            {"as": "guest", "call": "sync", "ops": [{"seq": 3, "type": "undo", "expects": 3}]},
            # A duplicate, then a gap, in the host's stream.
            {"as": "host", "call": "sync", "ops": [{"seq": 6, "type": "undo", "expects": 2}]},
            {"as": "host", "call": "sync", "ops": [{"seq": 9, "type": "point", "side": "team1"}]},
            {"as": "guest", "call": "state", "since": "rev"},
            # Deciding game: the switch prompt at 5, confirmed by the guest.
            {"as": "host", "call": "sync", "ops": _points("team1", 7, 3)},
            {"as": "guest", "call": "sync", "ops": [{"seq": 4, "type": "side_switch"}]},
            {"as": "guest", "call": "sync", "ops": _points("team2", 5, 2)},
            {"as": "host", "call": "state", "since": 0},
            # The host finishes the match; its op after the end is refused.
            {"as": "host", "call": "sync", "ops": _points("team1", 10, 7)},
            # The guest's late point: "match complete", seq not consumed.
            {"as": "guest", "call": "sync", "ops": _points("team2", 7, 1)},
            {"as": "guest", "call": "state", "since": None},
            {"as": "guest", "call": "state", "since": "rev", "wait": True},
            {"as": "host", "call": "state", "since": 0},
        ],
    },
    {
        "name": "solo_host",
        "best_of": 3,
        "shared": False,
        "steps": [
            {"as": "host", "call": "sync", "ops": [
                {"seq": 1, "type": "start", "initial_server": "team1"},
                *_points("team1", 2, 2),
                {"seq": 4, "type": "point", "side": "team2"},
            ]},
            # A solo host scores both sides; an undo without expects applies.
            {"as": "host", "call": "sync", "ops": [{"seq": 5, "type": "undo"}]},
            {"as": "host", "call": "sync", "ops": [{"seq": 6, "type": "undo", "expects": 9}]},
            {"as": "host", "call": "sync", "ops": [{"seq": 7, "type": "undo", "expects": 2}]},
            {"as": "host", "call": "state", "since": "rev"},
            {"as": "host", "call": "state", "since": 0},
        ],
    },
]


# --- normalisation ------------------------------------------------------------


def _redirect(url):
    if url is None:
        return None
    if re.search(r"/confirm/$", url):
        return "confirm"
    return "detail"


def _state(state, roles):
    out = {k: v for k, v in (state or {}).items() if k not in ("last_point_at", "events")}
    if state and "events" in state:
        events = []
        for e in state["events"]:
            event = {k: v for k, v in e.items() if k != "t"}
            if "by" in event:
                event["by"] = roles.get(event["by"], "?")
            events.append(event)
        out["events"] = events
    return out


def normalise(status, body, roles):
    """One answer as the fixture records it. ``roles``: player id -> role."""
    if body is None:
        return {"status": status}
    out = {"status": status}
    for key in ("error", "applied_seq", "rev", "is_live", "is_match_complete",
                "current_server", "should_prompt_side_switch", "rejected", "me"):
        if key in body:
            out[key] = body[key]
    if "state" in body:
        out["state"] = _state(body["state"], roles)
    if "redirect_url" in body:
        out["redirect_url"] = _redirect(body["redirect_url"])
    if "last_point" in body:
        lp = body["last_point"]
        out["last_point"] = None if lp is None else {
            "sequence": lp["sequence"], "side": lp["side"], "by": roles.get(lp["by"], "?"),
        }
    if "scorers" in body:
        out["scorers"] = [
            {"role": s["role"], "side": s["side"], "applied_seq": s["applied_seq"],
             "revoked": s["revoked"]}
            for s in body["scorers"]
        ]
    return out


def render(results) -> str:
    """The file's bytes: one answer per line, so a diff names what moved."""
    lines = ["{", '  "scripts": [']
    for i, script in enumerate(results):
        head = {k: v for k, v in script.items() if k != "answers"}
        lines.append("    {")
        lines.append('      "script": ' + json.dumps(head, sort_keys=True) + ",")
        lines.append('      "answers": [')
        answers = script["answers"]
        for j, answer in enumerate(answers):
            sep = "," if j < len(answers) - 1 else ""
            lines.append("        " + json.dumps(answer, sort_keys=True, ensure_ascii=False) + sep)
        lines.append("      ]")
        lines.append("    }" + ("," if i < len(results) - 1 else ""))
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main() -> int:
    """Regenerate the fixture through pytest (it needs the test database)."""
    import pytest

    os.environ[WRITE_ENV] = "1"
    here = Path(__file__).resolve().parent / "test_live_contract.py"
    return pytest.main([str(here), "-q", "-p", "no:randomly"])


if __name__ == "__main__":
    sys.exit(main())
