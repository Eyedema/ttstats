"""KAN-79 / KAN-91: the golden vectors for the client-side rules mirror.

``static/pingpong/js/live_rules.js`` mirrors ``live_scoring.py`` so the
scoreboard keeps scoring offline; ``tests/js/live_rules.test.mjs`` (``npm run
test:js``) replays ``fixtures/live_vectors.json`` through it. These tests make
sure that file is what ``live_scoring.py`` says *today*.

No ``django_db`` marker anywhere here: the generator and the rules are pure.
"""
import json

from django.template import Context, Template

from pingpong import live_scoring
from pingpong.tests import live_vectors


def _committed():
    return live_vectors.VECTORS_PATH.read_text()


def _apply(state, op):
    """One vector op through the Python rules; a refused op leaves state as is."""
    try:
        if op[0] == "start":
            return live_scoring.set_initial_server(state, op[1])
        if op[0] == "point":
            return live_scoring.apply_point(state, op[1], now=op[2])[0]
        if op[0] == "undo":
            return live_scoring.undo_last_point(state)[0]
        return live_scoring.confirm_side_switch(state)
    except ValueError:
        return state


def _replay(seq, keys):
    """Decode a sequence's steps into (state, flat record) pairs."""
    flat, events = {}, []
    for step in seq["steps"]:
        flat.update({k: v for k, v in step.items() if k != "e"})
        if "e" in step:
            drop, keep, push = step["e"]
            events = events[drop:drop + keep] + push
        yield {**{k: flat[k] for k in keys}, "events": events}, dict(flat)


class TestVectorsAreCurrent:
    def test_committed_file_matches_a_fresh_generation(self):
        fresh = live_vectors.render(live_vectors.generate())
        committed = _committed()
        if fresh != committed:
            old = {s["name"]: s for s in json.loads(committed)["sequences"]}
            new = json.loads(fresh)
            moved = [s["name"] for s in new["sequences"] if old.get(s["name"]) != s]
            raise AssertionError(
                "live_vectors.json is stale: live_scoring.py (or the generator) "
                "changed without regenerating it. Run:\n\n"
                f"    {live_vectors.REGENERATE_COMMAND}\n\n"
                "then make static/pingpong/js/live_rules.js pass `npm run test:js`. "
                f"Config now {new['config']}; sequences that moved: {moved[:10]}"
            )

    def test_generation_is_deterministic(self):
        first = live_vectors.render(live_vectors.generate())
        assert first == live_vectors.render(live_vectors.generate())


class TestVectorCoverage:
    """What KAN-91 promised the vectors would exercise."""

    def _data(self):
        return json.loads(_committed())

    def test_at_least_500_sequences(self):
        assert len(self._data()["sequences"]) >= 500

    def test_config_is_the_rules_config(self):
        assert self._data()["config"] == live_scoring.rules_config()

    def test_every_best_of_with_both_initial_servers(self):
        seen = {
            (s["best_of"], s["ops"][0][1])
            for s in self._data()["sequences"]
            if s["ops"][0][0] == "start"
        }
        assert seen == {(b, side) for b in (1, 3, 5, 7) for side in ("team1", "team2")}

    def test_decoded_states_are_what_live_scoring_produces(self):
        """The delta encoding round-trips: replaying every op through Python
        gives back every decoded state, so the encoder can't hide a change."""
        data = self._data()
        for seq in data["sequences"]:
            decoded = [state for state, _ in _replay(seq, data["state_keys"])]
            state = live_scoring.initial_state(seq["best_of"])
            assert decoded[0] == state
            for i, (op, expected) in enumerate(zip(seq["ops"], decoded[1:])):
                state = _apply(state, op)
                assert state == expected, f"{seq['name']} op {i}"

    def test_covers_the_hard_cases(self):
        data = self._data()
        seen = set()
        for seq in data["sequences"]:
            prev = None
            for i, (state, flat) in enumerate(_replay(seq, data["state_keys"])):
                op = seq["ops"][i - 1] if i else None
                r = flat["r"] if i else None
                if max(state["team1_points"], state["team2_points"]) >= 15:
                    seen.add("long deuce")
                if flat["sw"]:
                    seen.add("side switch prompt")
                if state["side_switched"] and state["current_game_number"] == state["best_of"]:
                    seen.add("deciding game switched")
                if op == ["undo"] and r and "winner" in r:
                    seen.add("undo across game end")
                if len(state["events"]) == live_scoring.MAX_EVENTS:
                    seen.add("event cap reached")
                if (
                    op == ["undo"]
                    and prev is not None
                    and not prev["events"]
                    and prev["team1_points"] + prev["team2_points"]
                    and state == prev
                ):
                    seen.add("undo past the cap is a no-op with points left")
                if r and "error" in r:
                    seen.add("refused op")
                prev = state
        assert seen == {
            "long deuce",
            "side switch prompt",
            "deciding game switched",
            "undo across game end",
            "event cap reached",
            "undo past the cap is a no-op with points left",
            "refused op",
        }


class TestRulesConfig:
    def test_names_every_constant_the_mirror_needs(self):
        assert live_scoring.rules_config() == {
            "WIN_POINTS": live_scoring.WIN_POINTS,
            "MIN_LEAD": live_scoring.MIN_LEAD,
            "DEUCE_AT": live_scoring.DEUCE_AT,
            "SERVES_PER_TURN": live_scoring.SERVES_PER_TURN,
            "SIDE_SWITCH_AT": live_scoring.SIDE_SWITCH_AT,
            "MAX_EVENTS": live_scoring.MAX_EVENTS,
        }

    def test_values_are_the_table_tennis_rules(self):
        assert live_scoring.rules_config() == {
            "WIN_POINTS": 11,
            "MIN_LEAD": 2,
            "DEUCE_AT": 10,
            "SERVES_PER_TURN": 2,
            "SIDE_SWITCH_AT": 5,
            "MAX_EVENTS": 50,
        }

    def test_is_a_fresh_dict_each_call(self):
        config = live_scoring.rules_config()
        config["WIN_POINTS"] = 21
        assert live_scoring.rules_config()["WIN_POINTS"] == 11


class TestLiveRulesConfigTag:
    def _render(self, src):
        return Template("{% load live_tags %}" + src).render(Context({}))

    def test_emits_json_script_with_the_config(self):
        html = self._render("{% live_rules_config %}")
        assert html.startswith('<script id="live-rules-config" type="application/json">')
        payload = html.split(">", 1)[1].rsplit("</script>", 1)[0]
        assert json.loads(payload) == live_scoring.rules_config()

    def test_element_id_can_be_overridden(self):
        assert 'id="rules"' in self._render('{% live_rules_config "rules" %}')
