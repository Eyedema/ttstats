"""KAN-73: Elo is gone, and stays gone.

A grep over the app source (not migrations, which are history, and not the
tests, which name the symbols to forbid them) for every symbol the
retirement deleted. A hit means a dormant copy or a stale call site, which is
exactly what the ticket's first acceptance criterion forbids.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings

APP = Path(__file__).resolve().parents[1]

REMOVED = [
    # The arithmetic and its entry points (pingpong/elo.py).
    r"\bcalculate_k_factor\b",
    r"\bcalculate_expected_score\b",
    r"\bcalculate_elo_change\b",
    r"\bEloProjection\b",
    r"\bprojected_elo_changes?\b",
    r"\bprojected_elo_change_for\b",
    r"\bupdate_player_elo\b",
    r"\bPROVISIONAL_MATCHES\b",
    r"\bBASE_K\b",
    # Modules.
    r"\bfrom \.+ import elo\b|\bpingpong\.elo\b|from \.elo import",
    r"\belo_replay\b",
    r"\belo_parity\b",
    r"\brating_divergence\b",
    r"\brecalculate_elo\b",
    # The dropped Elo columns.
    r"\belo_rating\b",
    r"\belo_peak\b",
    r"\bmatches_for_elo\b",
    # The rating-system switch.
    r"\bratings\.(system|enabled|ELO)\b",
    r"\bglicko_may_be_shown\b",
]

SOURCE_SUFFIXES = {".py", ".html", ".js", ".txt"}


def _app_sources():
    for path in APP.rglob("*"):
        if path.suffix not in SOURCE_SUFFIXES or not path.is_file():
            continue
        rel = path.relative_to(APP)
        if rel.parts[0] in {"migrations", "tests", "static", "__pycache__"}:
            continue
        yield rel, path.read_text(encoding="utf-8", errors="replace")


class TestEloIsRetired:
    @pytest.mark.parametrize("pattern", REMOVED)
    def test_no_call_site_remains(self, pattern):
        rx = re.compile(pattern)
        hits = [
            f"{rel}:{n}: {line.strip()}"
            for rel, text in _app_sources()
            for n, line in enumerate(text.splitlines(), 1)
            if rx.search(line)
        ]
        assert not hits, f"{pattern} still referenced:\n" + "\n".join(hits)

    def test_the_elo_modules_are_deleted(self):
        for name in ("elo.py", "elo_replay.py", "elo_parity.py", "rating_divergence.py"):
            assert not (APP / name).exists(), name
        assert not (APP / "management" / "commands" / "recalculate_elo.py").exists()

    def test_the_rating_flags_are_retired(self):
        assert "seasons" not in settings.FEATURE_FLAGS
        assert "glicko2" not in settings.FEATURE_FLAGS

    def test_no_template_says_elo(self):
        rx = re.compile(r"\bElo\b")
        hits = [
            f"{rel}:{n}"
            for rel, text in _app_sources()
            if rel.suffix == ".html"
            for n, line in enumerate(text.splitlines(), 1)
            if rx.search(line)
        ]
        assert not hits, "templates still say Elo:\n" + "\n".join(hits)
