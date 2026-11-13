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


def _workflow():
    return yaml.safe_load((REPO / ".github" / "workflows" / "main.yml").read_text())


class TestDeployJob:
    def _step(self):
        job = _workflow()["jobs"]["deploy"]
        return job, next(s for s in job["steps"] if "ssh-action" in s.get("uses", ""))

    def test_runs_only_on_a_master_push_after_everything_passed(self):
        job, _ = self._step()
        assert set(job["needs"]) >= {"test", "e2e", "build-image"}
        assert "refs/heads/master" in job["if"] and "push" in job["if"]

    def test_the_script_is_pull_then_deploy_sh(self):
        _, step = self._step()
        script = step["with"]["script"]
        lines = [line.strip() for line in script.splitlines() if line.strip()]
        assert lines[0] == "set -e"
        assert lines.index("git pull") < next(
            i for i, line in enumerate(lines) if "deploy/deploy.sh" in line
        )
        assert 'deploy/deploy.sh "$WEB_IMAGE" "$SENTRY_RELEASE"' in script
        # Everything else lives in the script, where it is tested.
        assert "docker compose" not in script
        assert "migrate" not in script

    def test_image_and_release_are_the_same_sha(self):
        _, step = self._step()
        env = step["env"]
        assert env["WEB_IMAGE"].endswith(":${{ github.sha }}")
        assert env["SENTRY_RELEASE"] == "${{ github.sha }}"
        assert {"WEB_IMAGE", "SENTRY_RELEASE", "GHCR_TOKEN", "GHCR_USER"} <= set(
            step["with"]["envs"].split(",")
        )

    def test_timeout_covers_migrate_swap_and_smoke(self):
        _, step = self._step()
        assert int(step["with"]["command_timeout"].rstrip("m")) >= 15


class TestBootTasks:
    def test_prod_web_skips_boot_tasks(self):
        """deploy.sh runs them once, before the swap (KAN-114)."""
        env = _compose()["services"]["web"]["environment"]
        assert str(env["SKIP_BOOT_TASKS"]) == "1"

    def test_entrypoint_honours_the_flag_and_keeps_the_guard(self):
        entrypoint = (REPO / "docker" / "django" / "entrypoint.sh").read_text()
        assert 'if [ "${SKIP_BOOT_TASKS:-0}" = "1" ]' in entrypoint
        assert "makemigrations --check --dry-run" in entrypoint
