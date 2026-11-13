"""The production compose file and the deploy workflow (KAN-106).

These files are only exercised for real on the VPS, where a mistake is a
failed or broken deploy, so their load-bearing lines are pinned here.
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[3]


def _compose():
    return yaml.safe_load((REPO / "compose.prod.yml").read_text())


class TestWebHealthcheck:
    def test_web_probes_healthz_over_loopback(self):
        check = _compose()["services"]["web"]["healthcheck"]
        command = " ".join(check["test"])

        assert "http://127.0.0.1:8000/healthz" in command
        # The image has no curl.
        assert check["test"][:2] == ["CMD", "python"]

    def test_the_probe_gives_up_before_docker_does(self):
        check = _compose()["services"]["web"]["healthcheck"]
        command = " ".join(check["test"])
        probe_timeout = int(command.split("timeout=")[1].split(")")[0])
        docker_timeout = int(check["timeout"].rstrip("s"))

        assert probe_timeout < docker_timeout

    def test_boot_tasks_fit_in_the_start_period(self):
        check = _compose()["services"]["web"]["healthcheck"]
        assert int(check["start_period"].rstrip("s")) >= 60
