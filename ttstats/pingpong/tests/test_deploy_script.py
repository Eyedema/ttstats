"""deploy/deploy.sh (KAN-110), driven with a fake `docker` on PATH.

The fake (deploy_fakes/docker) keeps a JSON state: which image `web` runs,
which steps fail, which images come up unhealthy, and every call made with
the WEB_IMAGE / SENTRY_RELEASE it saw. No Docker, no network.
"""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "deploy" / "deploy.sh"
FAKES = Path(__file__).resolve().parent / "deploy_fakes"

OLD = "ghcr.io/eyedema/ttstats-web:old111"
NEW = "ghcr.io/eyedema/ttstats-web:new222"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


class Box:
    """A fake host: a bin dir with the fakes first on PATH, and the state."""

    def __init__(self, tmp_path, **state):
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for fake in FAKES.iterdir():
            target = self.bin / fake.name
            shutil.copy(fake, target)
            target.chmod(target.stat().st_mode | stat.S_IEXEC)
        self.state_file = tmp_path / "docker.json"
        self.state_dir = tmp_path / "state"
        self.state_dir.mkdir()
        state.setdefault("running", OLD)
        self.state_file.write_text(json.dumps(state))

    def run(self, *args, **env):
        full = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "FAKE_DOCKER_STATE": str(self.state_file),
            "DEPLOY_STATE_DIR": str(self.state_dir),
            "SMOKE_INTERVAL": "0",
            **env,
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args], env=full, capture_output=True, text=True, timeout=60
        )

    @property
    def state(self):
        return json.loads(self.state_file.read_text())

    def verbs(self):
        """Each call reduced to its docker verb, e.g. 'compose pull'."""
        out = []
        for call in self.state["calls"]:
            argv = call["argv"]
            if argv[0] == "compose":
                rest = argv[1:]
                while rest and rest[0] == "-f":
                    rest = rest[2:]
                out.append(f"compose {rest[0]}")
            else:
                out.append(" ".join(argv[:2]) if argv[0] == "image" else argv[0])
        return out

    @property
    def last_good(self):
        f = self.state_dir / ".last-good-image"
        return f.read_text().strip() if f.exists() else None


class TestUsage:
    def test_needs_image_and_sha(self, tmp_path):
        box = Box(tmp_path)
        assert box.run().returncode == 2
        assert box.run(NEW).returncode == 2
        assert box.run("", "new222").returncode == 2


class TestHappyPath:
    def test_records_the_previous_image_before_changing_anything(self, tmp_path):
        box = Box(tmp_path)
        result = box.run(NEW, "new222")

        assert result.returncode == 0, result.stderr
        assert f"previous: {OLD}" in result.stdout
        first_change = next(
            i for i, v in enumerate(box.verbs()) if v in ("compose pull", "compose up")
        )
        assert "inspect" in box.verbs()[:first_change]

    def test_every_compose_call_is_pinned_to_the_new_image_and_release(self, tmp_path):
        box = Box(tmp_path)
        box.run(NEW, "new222")

        for call in box.state["calls"]:
            if call["argv"][0] == "compose" and "ps" not in call["argv"]:
                assert call["web_image"] == NEW, call
                assert call["release"] == "new222", call

    def test_writes_last_good_image(self, tmp_path):
        box = Box(tmp_path)
        box.run(NEW, "new222")
        assert box.last_good == NEW

    def test_logs_in_and_out_only_with_a_token(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        with_token = Box(tmp_path / "a")
        with_token.run(NEW, "new222", GHCR_USER="bot", GHCR_TOKEN="t0ken")
        verbs = with_token.verbs()
        assert verbs.index("login") < verbs.index("compose pull")
        assert verbs[-1] == "logout"

        without = Box(tmp_path / "b")
        without.run(NEW, "new222")
        assert "login" not in without.verbs()
        assert "logout" not in without.verbs()

    def test_the_token_never_reaches_argv(self, tmp_path):
        box = Box(tmp_path)
        result = box.run(NEW, "new222", GHCR_USER="bot", GHCR_TOKEN="t0ken")
        assert "t0ken" not in json.dumps(box.state["calls"])
        assert "t0ken" not in result.stdout + result.stderr

    def test_prints_a_timeline(self, tmp_path):
        result = Box(tmp_path).run(NEW, "new222")
        assert "[deploy +" in result.stdout

    def test_first_deploy_with_nothing_running(self, tmp_path):
        box = Box(tmp_path, running="")
        result = box.run(NEW, "new222")
        assert result.returncode == 0, result.stderr
        assert "previous: none" in result.stdout


class TestFailures:
    @pytest.mark.parametrize("step", ["pull", "up", "login"])
    def test_a_failed_step_fails_the_deploy_and_keeps_last_good(self, tmp_path, step):
        box = Box(tmp_path, fail=[step])
        (box.state_dir / ".last-good-image").write_text(OLD + "\n")

        result = box.run(NEW, "new222", GHCR_USER="bot", GHCR_TOKEN="t0ken")

        assert result.returncode != 0
        assert box.last_good == OLD
