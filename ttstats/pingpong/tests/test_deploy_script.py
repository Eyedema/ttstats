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


class TestOneMigrate:
    """KAN-114: makemigrations --check, migrate and collectstatic run once,
    with the new image, before the swap."""

    def test_pre_swap_tasks_run_once_before_up(self, tmp_path):
        box = Box(tmp_path)
        assert box.run(NEW, "new222").returncode == 0
        verbs = box.verbs()

        assert verbs.count("compose run") == 1
        assert verbs.index("compose pull") < verbs.index("compose run") < verbs.index("compose up")
        run = next(c for c in box.state["calls"] if "run" in c["argv"])
        command = " ".join(run["argv"])
        assert command.count("migrate --noinput") == 1
        assert "makemigrations --check --dry-run &&" in command
        assert "collectstatic --noinput" in command
        assert run["web_image"] == NEW
        # Never via the entrypoint (it would exec gunicorn), never with deps.
        assert "--entrypoint" in run["argv"] and "--no-deps" in run["argv"]

    def test_nothing_migrates_after_the_swap(self, tmp_path):
        box = Box(tmp_path)
        box.run(NEW, "new222")
        assert not any("migrate" in " ".join(e) for e in box.state.get("execs", []))

    def test_a_failed_pre_swap_task_never_swaps(self, tmp_path):
        box = Box(tmp_path, fail=["run"])
        (box.state_dir / ".last-good-image").write_text(OLD + "\n")

        result = box.run(NEW, "new222")

        assert result.returncode == 3
        assert "compose up" not in box.verbs()
        assert box.state["running"] == OLD
        assert box.last_good == OLD

    def test_up_waits_for_the_healthcheck(self, tmp_path):
        box = Box(tmp_path)
        box.run(NEW, "new222")
        up = next(c for c in box.state["calls"] if "up" in c["argv"] and "web" in c["argv"])
        assert "--wait" in up["argv"]

    def test_db_and_redis_come_up_without_touching_web(self, tmp_path):
        box = Box(tmp_path)
        box.run(NEW, "new222")
        first_up = next(c for c in box.state["calls"] if "up" in c["argv"])
        assert first_up["argv"][-2:] == ["db", "redis"]
        assert "--no-deps" in first_up["argv"]


class TestSmokeAndRollback:
    """KAN-116: the public /healthz must report the new release and the
    smoke pages must answer, or the previous image goes back."""

    def test_smoke_checks_release_then_pages(self, tmp_path):
        box = Box(tmp_path)
        result = box.run(NEW, "new222", PUBLIC_URL="https://tt.example")

        assert result.returncode == 0, result.stdout + result.stderr
        curls = box.state["curls"]
        assert curls[0] == "https://tt.example/healthz"
        for path in ("/accounts/login/", "/pingpong/login/", "/sw.js"):
            assert f"https://tt.example{path}" in curls

    def _assert_rolled_back(self, box, result):
        assert result.returncode == 1, result.stdout + result.stderr
        assert box.state["running"] == OLD
        last_up = [c for c in box.state["calls"] if "up" in c["argv"]][-1]
        assert last_up["web_image"] == OLD
        assert last_up["release"] == "old111"
        assert box.last_good is None or box.last_good == OLD
        assert "rolled back" in result.stdout
        # Reported from inside the container that is serving again.
        assert any("python" in e for e in box.state.get("execs", []))

    def test_unhealthy_new_container_rolls_back(self, tmp_path):
        box = Box(tmp_path, healthy={NEW: False})
        self._assert_rolled_back(box, box.run(NEW, "new222", SMOKE_TIMEOUT="0"))

    def test_broken_page_rolls_back(self, tmp_path):
        box = Box(tmp_path, broken_pages={NEW: ["/pingpong/login/"]})
        result = box.run(NEW, "new222")
        self._assert_rolled_back(box, result)
        assert "/pingpong/login/ answered 500" in result.stdout

    def test_wrong_release_on_healthz_rolls_back(self, tmp_path):
        """The proxy still reaching the old container counts as a failure."""
        box = Box(tmp_path)
        # The image is tagged differently from the SHA we were asked to ship.
        result = box.run(NEW, "not-this-sha", SMOKE_TIMEOUT="0")
        self._assert_rolled_back(box, result)

    def test_failed_rollback_is_exit_4(self, tmp_path):
        box = Box(tmp_path, healthy={NEW: False, OLD: False})
        result = box.run(NEW, "new222", SMOKE_TIMEOUT="0")
        assert result.returncode == 4
        assert "ROLLBACK FAILED" in result.stdout

    def test_nothing_to_roll_back_to_is_exit_4(self, tmp_path):
        box = Box(tmp_path, running="", healthy={NEW: False})
        result = box.run(NEW, "new222", SMOKE_TIMEOUT="0")
        assert result.returncode == 4

    def test_previous_image_falls_back_to_last_good(self, tmp_path):
        box = Box(tmp_path, running="", healthy={NEW: False})
        (box.state_dir / ".last-good-image").write_text(OLD + "\n")
        result = box.run(NEW, "new222", SMOKE_TIMEOUT="0")
        assert result.returncode == 1
        assert box.state["running"] == OLD

    def test_a_sentry_failure_does_not_change_the_outcome(self, tmp_path):
        box = Box(tmp_path, healthy={NEW: False}, fail=["exec"])
        result = box.run(NEW, "new222", SMOKE_TIMEOUT="0")
        assert result.returncode == 1


class TestFailures:
    def test_failed_pull_is_exit_3_and_changes_nothing(self, tmp_path):
        box = Box(tmp_path, fail=["pull"])
        (box.state_dir / ".last-good-image").write_text(OLD + "\n")
        result = box.run(NEW, "new222")
        assert result.returncode == 3
        assert "compose up" not in box.verbs()
        assert box.last_good == OLD

    def test_failed_login_fails_before_pulling(self, tmp_path):
        box = Box(tmp_path, fail=["login"])
        result = box.run(NEW, "new222", GHCR_USER="bot", GHCR_TOKEN="t0ken")
        assert result.returncode != 0
        assert "compose pull" not in box.verbs()


class TestRollout:
    """KAN-115: SWAP_MODE=rollout starts the new container beside the old one
    and stops the old one only once the new one is healthy."""

    def test_new_container_healthy_then_old_stopped(self, tmp_path):
        box = Box(tmp_path)
        result = box.run(NEW, "new222", SWAP_MODE="rollout")

        assert result.returncode == 0, result.stdout + result.stderr
        assert [c["image"] for c in box.state["containers"]] == [NEW]
        verbs = box.verbs()
        scale_up = next(i for i, c in enumerate(box.state["calls"])
                        if "--scale" in c["argv"])
        assert "--no-recreate" in box.state["calls"][scale_up]["argv"]
        assert box.state["calls"][scale_up]["web_image"] == NEW
        # The old container is stopped (drained) only after the new is healthy.
        assert verbs.index("stop") > scale_up
        stop = next(c for c in box.state["calls"] if c["argv"][0] == "stop")
        assert stop["argv"][1:3] == ["-t", "30"] and stop["argv"][-1] == "c0"

    def test_unhealthy_new_container_is_removed_and_old_never_touched(self, tmp_path):
        box = Box(tmp_path, healthy={NEW: False})
        result = box.run(NEW, "new222", SWAP_MODE="rollout", SMOKE_TIMEOUT="0")

        assert "c0" not in box.state.get("removed", [])
        assert [c["image"] for c in box.state["containers"]] == [OLD]
        # Then the rollback finds the old image already serving and keeps it.
        assert result.returncode == 1, result.stdout + result.stderr

    def test_smoke_failure_after_rollout_rolls_back_the_same_way(self, tmp_path):
        box = Box(tmp_path, broken_pages={NEW: ["/sw.js"]})
        result = box.run(NEW, "new222", SWAP_MODE="rollout")

        assert result.returncode == 1
        assert [c["image"] for c in box.state["containers"]] == [OLD]
        assert box.state["containers"][0]["id"] != "c0"  # a fresh old-image container

    def test_default_mode_is_recreate(self, tmp_path):
        box = Box(tmp_path)
        box.run(NEW, "new222")
        assert not any("--scale" in c["argv"] for c in box.state["calls"])


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="needs shellcheck")
def test_shellcheck_clean():
    """GitHub's ubuntu runners ship shellcheck, so CI runs this."""
    result = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout
