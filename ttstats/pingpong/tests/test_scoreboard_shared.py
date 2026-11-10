"""KAN-153: the co-scoring scoreboard, server side.

The page tells the client whether the match is shared and who the caller is
(``scoreboard-config``), and renders the "my side" layout itself: ``data-shared``
and ``data-my-side`` on ``#scoreboard``, one ``.tap-zone`` button for my side
and a plain ``<div>`` for the opponent's read-only score. CSS (not JS-added
classes) does the rest. What a browser does with it is in
``tests/e2e/shared-scoreboard*.spec.js``.
"""
import json
import re

import pytest
from django.urls import reverse

from pingpong.models import LiveScorer, Match

from .conftest import live_match


def _scoreboard(client, match):
    return client.get(reverse("pingpong:live_scoreboard", args=[match.pk]))


def _config(resp):
    html = resp.content.decode()
    raw = re.search(
        r'<script id="scoreboard-config" type="application/json">(.*?)</script>', html, re.S
    ).group(1)
    return json.loads(raw)


def _root(resp):
    return re.search(r'<div\s+id="scoreboard"[^>]*>', resp.content.decode(), re.S).group(0)


def _scorer(match, role):
    return LiveScorer.objects.get(match=match, role=role).player


@pytest.mark.django_db
class TestBootstrap:
    def test_solo_match_is_not_shared(self, auth_client):
        match = live_match(started=True)
        resp = _scoreboard(auth_client(_scorer(match, "host").user), match)
        config = _config(resp)
        assert config["shared"] is False
        assert config["me"] == {"side": 1, "role": "host", "can_score_both": True}
        assert "longpoll_abort_ms" in config

    def test_shared_match_tells_each_phone_who_it_is(self, auth_client):
        match = live_match(started=True, shared=True)
        host = _config(_scoreboard(auth_client(_scorer(match, "host").user), match))
        guest = _config(_scoreboard(auth_client(_scorer(match, "guest").user), match))
        assert host["shared"] is True and guest["shared"] is True
        assert host["me"] == {"side": 1, "role": "host", "can_score_both": False}
        assert guest["me"] == {"side": 2, "role": "guest", "can_score_both": False}


@pytest.mark.django_db
class TestMySideLayout:
    def test_solo_keeps_two_tap_zones_and_no_shared_attributes(self, auth_client):
        match = live_match(started=True)
        resp = _scoreboard(auth_client(_scorer(match, "host").user), match)
        root = _root(resp)
        assert "data-shared" not in root and "data-my-side" not in root
        html = resp.content.decode()
        assert len(re.findall(r'<button type="button" data-side="team[12]"', html)) == 2
        assert "+1 " not in html

    @pytest.mark.parametrize("role,mine,theirs", [("host", "team1", "team2"), ("guest", "team2", "team1")])
    def test_shared_renders_my_zone_as_the_only_button(self, auth_client, role, mine, theirs):
        match = live_match(started=True, shared=True)
        player = _scorer(match, role)
        resp = _scoreboard(auth_client(player.user), match)
        root = _root(resp)
        assert "data-shared" in root
        assert f'data-my-side="{mine}"' in root
        html = resp.content.decode()
        # My side: the big "+1 {name}" button.
        mine_zone = re.search(rf'<button type="button" data-side="{mine}" data-zone="mine"[^>]*>', html)
        assert mine_zone and "tap-zone" in mine_zone.group(0)
        assert f"+1 {player.name}" in html
        # Theirs: read-only digits. No button, no role, no tabindex, no tap-zone.
        theirs_zone = re.search(rf'<div data-side="{theirs}" data-zone="theirs"[^>]*>', html, re.S)
        assert theirs_zone is not None
        for forbidden in ("tap-zone", "role=", "tabindex", "<button"):
            assert forbidden not in theirs_zone.group(0)
        assert f'<button type="button" data-side="{theirs}"' not in html
        # The digits and pips the client renders are still there for both sides.
        assert f'data-score="{theirs}"' in html and f'data-score="{mine}"' in html

    def test_a_phone_that_scores_both_keeps_the_two_zones(self, auth_client):
        match = live_match(started=True, shared=True)
        LiveScorer.objects.filter(match=match, role="host").update(can_score_both=True)
        resp = _scoreboard(auth_client(_scorer(match, "host").user), match)
        assert "data-shared" not in _root(resp)
        assert _config(resp)["shared"] is True  # still rebases on the guest's ops

    def test_undo_label_hook_and_pull_to_refresh_opt_out_survive(self, auth_client):
        match = live_match(started=True, shared=True)
        resp = _scoreboard(auth_client(_scorer(match, "guest").user), match)
        assert "data-undo-label" in resp.content.decode()
        assert "data-no-pull-refresh" in _root(resp)


@pytest.mark.django_db
class TestFinishedMatchRedirect:
    """The phone that did not tap the final point learns of the end from the
    long-poll: it must be handed match_confirm, as live_sync hands the other."""

    def test_state_answers_match_confirm_once_finished(self, auth_client):
        match = live_match(best_of=1, started=True, shared=True)
        host = auth_client(_scorer(match, "host").user)
        guest = auth_client(_scorer(match, "guest").user)
        ops = [{"seq": i + 1, "type": "point", "side": "team1"} for i in range(11)]
        resp = host.post(
            reverse("pingpong:live_sync", args=[match.pk]),
            data=json.dumps({"ops": ops}), content_type="application/json",
        )
        confirm = reverse("pingpong:match_confirm", args=[match.pk])
        assert resp.json()["redirect_url"] == confirm
        polled = guest.get(reverse("pingpong:live_state", args=[match.pk]), {"since": "0"})
        assert polled.status_code == 200
        assert polled.json()["is_live"] is False
        assert polled.json()["redirect_url"] == confirm

    def test_a_match_closed_without_a_winner_still_goes_to_detail(self, auth_client):
        match = live_match(started=True)
        Match.all_objects.filter(pk=match.pk).update(is_live=False, live_state=None)
        resp = auth_client(_scorer(match, "host").user).get(
            reverse("pingpong:live_state", args=[match.pk])
        )
        assert resp.json()["redirect_url"] == reverse("pingpong:match_detail", args=[match.pk])
