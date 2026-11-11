"""KAN-165: the JS fake server's contract is generated from the real endpoints.

``live_contract.SCRIPTS`` run through ``live_sync`` and ``/live/state/``;
``fixtures/live_contract.json`` must be what they answer *today*.
``tests/js/live_contract.test.mjs`` replays the same scripts through
``tests/js/live_fakes.mjs`` and demands the same answers, so the fake the
scoreboard client is tested against cannot drift from the server.
"""
import json
import os

import pytest
from django.urls import reverse

from pingpong.models import LiveScorer, Match

from . import live_contract
from .conftest import live_match


def _run(script, auth_client):
    match = live_match(best_of=script["best_of"], shared=script["shared"])
    scorers = {s.role: s for s in LiveScorer.objects.filter(match=match).select_related("player__user")}
    clients = {role: auth_client(s.player.user) for role, s in scorers.items()}
    roles = {s.player_id: role for role, s in scorers.items()}
    answers = []
    for step in script["steps"]:
        client = clients[step["as"]]
        if step["call"] == "sync":
            ops = [
                {**op, "expects_by": scorers[op["expects_by"]].player_id}
                if "expects_by" in op else op
                for op in step["ops"]
            ]
            resp = client.post(
                reverse("pingpong:live_sync", args=[match.pk]),
                data=json.dumps({"ops": ops}),
                content_type="application/json",
            )
        elif step["call"] == "remove":
            resp = client.post(reverse(
                "pingpong:live_remove_scorer",
                args=[match.pk, scorers[step["target"]].player_id],
            ))
        else:
            params = {}
            since = step.get("since")
            if since == "rev":
                since = Match.all_objects.get(pk=match.pk).live_rev
            if since is not None:
                params["since"] = str(since)
            if step.get("wait"):
                params["wait"] = "1"
            resp = client.get(reverse("pingpong:live_state", args=[match.pk]), params)
        body = json.loads(resp.content) if resp.content else None
        answers.append(live_contract.normalise(resp.status_code, body, roles))
    return {**script, "answers": answers}


def generate(auth_client):
    return [_run(script, auth_client) for script in live_contract.SCRIPTS]


@pytest.mark.django_db
class TestFakeServerContract:
    def test_committed_fixture_matches_the_real_endpoints(self, auth_client):
        fresh = live_contract.render(generate(auth_client))
        if os.environ.get(live_contract.WRITE_ENV):
            live_contract.CONTRACT_PATH.write_text(fresh)
        committed = live_contract.CONTRACT_PATH.read_text()
        assert fresh == committed, (
            "live_contract.json is stale: the live endpoints (or the scripts) "
            "changed without regenerating it. Run:\n\n"
            f"    {live_contract.REGENERATE_COMMAND}\n\n"
            "then make tests/js/live_fakes.mjs pass `npm run test:js`."
        )

    def test_the_scripts_cover_what_the_fake_models(self):
        """Per-stream seqs, rev, side restriction, expects, start race,
        duplicate, gap, completion and the 204: each must be exercised."""
        data = json.loads(live_contract.CONTRACT_PATH.read_text())
        answers = [a for s in data["scripts"] for a in s["answers"]]
        errors = {a.get("error") for a in answers} | {
            (a.get("rejected") or {}).get("error") for a in answers
        }
        assert {"gap", "not your side", "stale undo", "Match already started",
                "match complete", "removed", "only scorer"} <= errors
        assert any(a["status"] == 204 for a in answers)
        assert any(a.get("redirect_url") == "confirm" for a in answers)
