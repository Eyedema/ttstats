"""deploy/backup/backup.sh and restore_drill.sh (KAN-118, KAN-108).

The scripts run for real under bash; `docker`, `age`, `age-keygen`, `rclone`
and `curl` are fakes placed first on PATH. The fake rclone treats its paths as
local directories, so BACKUP_REMOTE is a temp dir the tests can inspect. The
fake docker emulates pg_dump / pg_restore / psql against a JSON "database"
(`prod.state`), so a dump carries that state and a restore exposes it to the
drill's queries. No Django DB is needed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
BACKUP_SH = REPO / "deploy" / "backup" / "backup.sh"
DRILL_SH = REPO / "deploy" / "backup" / "restore_drill.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not installed")

MAIN_PUB = "age1mainrecipient"
DRILL_PUB = "age1drillrecipient"
TABLES = ("pingpong_match", "pingpong_pointevent", "pingpong_playerrating", "auth_user")

# --------------------------------------------------------------------- fakes
# All fakes are POSIX sh: the scripts call them dozens of times per run, and
# an interpreter start-up per call would make the suite slow.
#
# FAKE_FAIL is a comma list of "name" or "name:substring" (fail only when an
# argument contains the substring).

SH_COMMON = textwrap.dedent(
    r"""
    #!/bin/sh
    S=$FAKE_STATE_DIR
    printf '%s %s\n' "$TOOL" "$*" >>"$S/calls.log"
    fails() {
        [ -n "${FAKE_FAIL:-}" ] || return 1
        old_ifs=$IFS; IFS=,; set -f; list=$FAKE_FAIL; set +f; IFS=$old_ifs
        IFS=,
        for f in $list; do
            IFS=$old_ifs
            case "$f" in
                "$1") return 0 ;;
                "$1:"*) case "$2" in *"${f#*:}"*) return 0 ;; esac ;;
            esac
        done
        IFS=$old_ifs
        return 1
    }
    """
).lstrip()


def _fake(tool: str, body: str) -> str:
    return SH_COMMON.replace('"$TOOL"', tool) + textwrap.dedent(body)


# The "database" is a key=value file: count_<table>=N, migration=..,
# match_age_days=N. A dump is that file between a PGDMP-FAKE line and an END
# line; a restore copies it to containers/<name>.state.
FAKE_DOCKER = _fake(
    "docker",
    r"""
    answer() {  # answer STATEFILE, SQL on stdin
        sql=$(cat)
        case "$sql" in
            *"count(*) FROM "*)
                t=$(printf '%s' "$sql" | sed -n 's/.*count(\*) FROM \([a-z0-9_]*\).*/\1/p')
                v=$(sed -n "s/^count_$t=//p" "$1"); echo "${v:-0}" ;;
            *django_migrations*) sed -n 's/^migration=//p' "$1" ;;
            *"max(date_played)"*) sed -n 's/^match_age_days=//p' "$1" ;;
            *to_timestamp*) echo "${FAKE_DUMP_AGE_HOURS:-1}" ;;
            *) echo "fake psql: unknown query $sql" >&2; exit 3 ;;
        esac
    }
    valid_dump() {  # valid_dump FILE
        [ "$(head -n 1 "$1")" = PGDMP-FAKE ] && [ "$(tail -n 1 "$1")" = END ]
    }
    case "$1" in
    compose)
        while [ "$1" != exec ]; do shift; done
        shift; [ "$1" = -T ] && shift; shift   # exec, -T, the service
        case "$1 ${3:-}" in
        *pg_dump*)
            case "${FAKE_DUMP_MODE:-ok}" in
                fail) echo "pg_dump: error: connection refused" >&2; exit 1 ;;
                empty) exit 0 ;;
                truncated) echo PGDMP-FAKE; cat "$S/prod.state" ;;
                *) echo PGDMP-FAKE; cat "$S/prod.state"; echo END ;;
            esac ;;
        "pg_restore "*)
            cat >"$S/stdin.$$"
            valid_dump "$S/stdin.$$" || { rm -f "$S/stdin.$$"; echo "pg_restore: error: could not read input file" >&2; exit 1; }
            rm -f "$S/stdin.$$"
            printf ';\n; Archive created by fake\n;\n1; 0 0 TABLE public pingpong_match\n' ;;
        *psql*)
            fails prod-psql && exit 2
            answer "$S/prod.state" ;;
        *) echo "fake docker compose: unexpected $*" >&2; exit 2 ;;
        esac ;;
    run)
        fails docker-run && exit 125
        while [ "$1" != --name ]; do shift; done
        mkdir -p "$S/containers" && echo running >"$S/containers/$2"
        echo fakecontainerid ;;
    exec)
        shift; [ "$1" = -i ] && shift
        name=$1; shift
        case "$1" in
        pg_isready) exit 0 ;;
        pg_restore)
            cat >"$S/stdin.$$"
            if fails restore || ! valid_dump "$S/stdin.$$"; then
                rm -f "$S/stdin.$$"; echo "pg_restore: error: could not execute query" >&2; exit 1
            fi
            sed '1d;$d' "$S/stdin.$$" >"$S/containers/$name.state"; rm -f "$S/stdin.$$" ;;
        psql) answer "$S/containers/$name.state" ;;
        *) echo "fake docker exec: unexpected $*" >&2; exit 2 ;;
        esac ;;
    rm) eval "last=\${$#}"; echo "$last" >>"$S/removed.log" ;;
    *) echo "fake docker: unexpected $*" >&2; exit 2 ;;
    esac
    """,
)

# "Encryption" is a header line, AGE-FAKE <payload length> <recipients...>,
# then the plaintext. Decryption checks the length (so truncation fails, as
# it does with real age) and that the identity is one of the recipients.
FAKE_AGE = _fake(
    "age",
    r"""
    fails age && { echo "age: error: fake failure" >&2; exit 1; }
    decrypt=0 recipients="" ident="" out=""
    while [ $# -gt 1 ]; do
        case "$1" in
            -d) decrypt=1 ;;
            -r) recipients="$recipients $2"; shift ;;
            -R) recipients="$recipients $(grep -v '^#' "$2" | tr '\n' ' ')"; shift ;;
            -i) ident=$2; shift ;;
            -o) out=$2; shift ;;
        esac
        shift
    done
    src=$1
    if [ "$decrypt" = 1 ]; then
        pub=$(sed -n 's/^# public key: //p' "$ident")
        header=$(head -n 1 "$src")
        set -- $header
        hlen=$(( ${#header} + 1 ))
        total=$(wc -c <"$src" | tr -d ' ')
        if [ "${1:-}" != AGE-FAKE ] || [ $((total - hlen)) != "${2:-x}" ]; then
            echo "age: error: failed to decrypt and authenticate payload chunk" >&2; exit 1
        fi
        case " $header " in *" $pub "*) ;; *) echo "age: error: no identity matched any of the recipients" >&2; exit 1 ;; esac
        tail -c +$((hlen + 1)) "$src" >"$out"
    else
        [ -n "$recipients" ] || { echo "age: error: missing recipients" >&2; exit 1; }
        printf 'AGE-FAKE %s%s\n' "$(wc -c <"$src" | tr -d ' ')" "$(echo $recipients | sed 's/^/ /')" >"$out"
        cat "$src" >>"$out"
    fi
    """,
)

FAKE_AGE_KEYGEN = _fake(
    "age-keygen",
    r"""
    case "$1" in
        -o) printf '# public key: age1throwaway\nAGE-SECRET-KEY-FAKE\n' >"$2"; echo "Public key: age1throwaway" >&2 ;;
        -y) sed -n 's/^# public key: //p' "$2" ;;
    esac
    """,
)

FAKE_CURL = _fake(
    "curl",
    r"""
    eval "url=\${$#}"
    echo "$url" >>"$S/checkins.log"
    prev=""
    for a in "$@"; do
        [ "$prev" = "--data" ] && printf '%s\n' "$a" >>"$S/checkin_bodies.log"
        prev=$a
    done
    fails curl && exit 22
    exit 0
    """,
)

# The fake rclone treats every path as a local path.
FAKE_RCLONE = _fake(
    "rclone",
    r"""
    cmd=$1; shift
    case "$cmd" in
    copyto)
        fails rclone-copyto "$*" && { echo "fake rclone: copy failed" >&2; exit 1; }
        mkdir -p "$(dirname "$2")" && cp "$1" "$2" ;;
    moveto)
        fails rclone-moveto "$*" && { echo "fake rclone: move failed" >&2; exit 1; }
        mkdir -p "$(dirname "$2")" && mv "$1" "$2" ;;
    lsf)
        [ "$1" = --files-only ] && shift
        fails rclone-lsf "$*" && { echo "fake rclone: list failed" >&2; exit 1; }
        [ -d "$1" ] || { echo "directory not found" >&2; exit 3; }
        for f in "$1"/*; do [ -f "$f" ] && basename "$f"; done; exit 0 ;;
    deletefile)
        fails rclone-deletefile "$*" && exit 1
        rm "$1" ;;
    delete)
        # delete ROOT --include '*.partial' --min-age 24h
        root=$1; age=$5
        fails rclone-delete "$*" && exit 1
        case "$age" in *h) mins=$(( ${age%h} * 60 )) ;; *d) mins=$(( ${age%d} * 1440 )) ;; *) exit 2 ;; esac
        find "$root" -type f -name '*.partial' -mmin +"$mins" -exec rm {} + ;;
    *) echo "fake rclone: unexpected $cmd" >&2; exit 2 ;;
    esac
    """,
)


# ------------------------------------------------------------------- harness


def _install_fakes(bin_dir: Path) -> None:
    for name, body in (
        ("docker", FAKE_DOCKER),
        ("age", FAKE_AGE),
        ("age-keygen", FAKE_AGE_KEYGEN),
        ("curl", FAKE_CURL),
        ("rclone", FAKE_RCLONE),
    ):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)


class Env:
    def __init__(self, root: Path, bin_dir: Path):
        self.root = root
        self.bin = bin_dir
        self.state = root / "state"
        self.remote = root / "remote"
        self.local = root / "local"
        self.tmp = root / "tmp"
        self.project = root / "project"
        for d in (self.state, self.remote, self.local, self.tmp, self.project):
            d.mkdir()
        self.drill_key = root / "drill.key"
        self.drill_key.write_text(f"# public key: {DRILL_PUB}\nAGE-SECRET-KEY-DRILL\n")
        self.set_prod()
        self.config = {
            "BACKUP_PROJECT_DIR": str(self.project),
            "BACKUP_REMOTE": str(self.remote),
            "BACKUP_AGE_RECIPIENTS": f"{MAIN_PUB},{DRILL_PUB}",
            "BACKUP_LOCAL_DIR": str(self.local),
            "BACKUP_TMPDIR": str(self.tmp),
            "BACKUP_DRILL_IDENTITY": str(self.drill_key),
            "SENTRY_CRON_URL_BACKUP": "https://o1.ingest.sentry.io/api/9/cron/ttstats-backup/key/",
            "SENTRY_CRON_URL_DRILL": "https://o1.ingest.sentry.io/api/9/cron/ttstats-drill/key/",
        }

    def set_prod(self, counts=None, migration="pingpong.0052_live_lobby_open_to_club", match_age_days=0):
        counts = counts or {t: 100 for t in TABLES}
        lines = [f"count_{t}={n}" for t, n in counts.items()]
        lines += [f"migration={migration}", f"match_age_days={match_age_days}"]
        (self.state / "prod.state").write_text("\n".join(lines) + "\n")

    def write_env_file(self) -> Path:
        path = self.root / "backup.env"
        path.write_text("".join(f'{k}="{v}"\n' for k, v in self.config.items()))
        return path

    def run(self, script: Path, *args: str, now: str | None = None, **fake_env: str):
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.root),
            "BACKUP_ENV_FILE": str(self.write_env_file()),
            "FAKE_STATE_DIR": str(self.state),
        }
        if now:
            env["BACKUP_NOW"] = now
        env.update(fake_env)
        return subprocess.run(
            [BASH, str(script), *args], env=env, capture_output=True, text=True, timeout=60
        )

    def backup(self, *args, now="2026-11-13T02:45", **fake_env):
        return self.run(BACKUP_SH, *args, now=now, **fake_env)

    def drill(self, *args, **fake_env):
        return self.run(DRILL_SH, *args, **fake_env)

    # inspection
    def remote_files(self, prefix: str | None = None) -> list[str]:
        base = self.remote / prefix if prefix else self.remote
        if not base.exists():
            return []
        return sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file())

    def checkins(self) -> list[str]:
        log = self.state / "checkins.log"
        if not log.exists():
            return []
        return [line.rsplit("status=", 1)[1] for line in log.read_text().split()]

    def calls(self, tool: str) -> list[str]:
        log = self.state / "calls.log"
        if not log.exists():
            return []
        return [line for line in log.read_text().splitlines() if line.startswith(tool + " ")]

    def started_containers(self) -> list[str]:
        d = self.state / "containers"
        return sorted(p.name for p in d.iterdir() if p.suffix != ".state") if d.exists() else []

    def removed_containers(self) -> list[str]:
        log = self.state / "removed.log"
        return sorted(log.read_text().split()) if log.exists() else []


@pytest.fixture(scope="module")
def fake_bin(tmp_path_factory):
    # Once per module: the fakes are stateless (state lives in FAKE_STATE_DIR),
    # and macOS scans every new executable on its first launch (~150 ms each).
    bin_dir = tmp_path_factory.mktemp("fake-bin")
    _install_fakes(bin_dir)
    return bin_dir


@pytest.fixture
def env(tmp_path, fake_bin):
    return Env(tmp_path, fake_bin)


def _out(result) -> str:
    return result.stdout + result.stderr


# ------------------------------------------------------------------- backup


class TestBackupHappyPath:
    def test_weekday_lands_in_daily_only(self, env):
        r = env.backup(now="2026-11-13T02:45")  # a Friday
        assert r.returncode == 0, _out(r)
        assert env.remote_files() == ["daily/ttstats-20261113-0245.dump.age"]
        assert env.checkins() == ["in_progress", "ok"]
        assert "backup OK" in r.stdout

    def test_sunday_also_lands_in_weekly(self, env):
        r = env.backup(now="2026-11-15T02:45")
        assert r.returncode == 0, _out(r)
        assert env.remote_files() == [
            "daily/ttstats-20261115-0245.dump.age",
            "weekly/ttstats-20261115-0245.dump.age",
        ]

    def test_first_of_month_also_lands_in_monthly(self, env):
        r = env.backup(now="2026-12-01T02:45")  # a Tuesday
        assert r.returncode == 0, _out(r)
        assert env.remote_files() == [
            "daily/ttstats-20261201-0245.dump.age",
            "monthly/ttstats-20261201-0245.dump.age",
        ]

    def test_a_sunday_first_lands_everywhere(self, env):
        r = env.backup(now="2026-11-01T02:45")
        assert r.returncode == 0, _out(r)
        assert len(env.remote_files()) == 3

    def test_upload_goes_through_a_partial_name(self, env):
        env.backup()
        rclone = env.calls("rclone")
        final = f"{env.remote}/daily/ttstats-20261113-0245.dump.age"
        assert any(c.startswith("rclone copyto") and c.endswith(final + ".partial") for c in rclone)
        assert f"rclone moveto {final}.partial {final}" in rclone

    def test_encrypted_to_every_recipient_and_no_plaintext_left(self, env):
        recipients_file = env.root / "recipients.txt"
        recipients_file.write_text("# offline key\nage1fromfile\n")
        env.config["BACKUP_AGE_RECIPIENTS_FILE"] = str(recipients_file)
        r = env.backup()
        assert r.returncode == 0, _out(r)
        blob = (env.remote / "daily" / "ttstats-20261113-0245.dump.age").read_bytes()
        header = blob.split(b"\n", 1)[0].decode()
        assert header.split()[2:] == [MAIN_PUB, DRILL_PUB, "age1fromfile"]
        assert list(env.tmp.iterdir()) == []

    def test_local_copy_keeps_the_newest_two(self, env):
        for day in (10, 11, 12):
            assert env.backup(now=f"2026-11-{day}T02:45").returncode == 0
        assert sorted(p.name for p in env.local.iterdir()) == [
            "ttstats-20261111-0245.dump.age",
            "ttstats-20261112-0245.dump.age",
        ]

    def test_check_in_url_with_a_query_string_gets_an_ampersand(self, env):
        env.config["SENTRY_CRON_URL_BACKUP"] = "https://example.invalid/cron/?env=prod"
        env.backup()
        urls = (env.state / "checkins.log").read_text().split()
        assert urls[0] == "https://example.invalid/cron/?env=prod&status=in_progress"

    def test_no_check_in_url_means_no_curl(self, env):
        del env.config["SENTRY_CRON_URL_BACKUP"]
        r = env.backup()
        assert r.returncode == 0, _out(r)
        assert env.calls("curl") == []


class TestBackupFailures:
    @pytest.mark.parametrize(
        ("fake_env", "code", "message"),
        [
            ({"FAKE_DUMP_MODE": "fail"}, 3, "pg_dump failed"),
            ({"FAKE_DUMP_MODE": "empty"}, 4, "dump is empty"),
            ({"FAKE_DUMP_MODE": "truncated"}, 4, "pg_restore --list rejected"),
            ({"FAKE_FAIL": "age"}, 5, "age encryption failed"),
            ({"FAKE_FAIL": "rclone-copyto"}, 6, "upload of"),
            ({"FAKE_FAIL": "rclone-moveto"}, 6, "upload of"),
        ],
    )
    def test_failing_step_exits_with_its_code_and_leaves_no_valid_backup(
        self, env, fake_env, code, message
    ):
        r = env.backup(**fake_env)
        assert r.returncode == code, _out(r)
        assert message in r.stdout
        assert [f for f in env.remote_files() if f.endswith(".dump.age")] == []
        assert env.checkins() == ["in_progress", "error"]
        assert list(env.tmp.iterdir()) == []

    def test_failed_weekly_copy_skips_retention(self, env):
        daily = env.remote / "daily"
        daily.mkdir()
        for day in range(1, 11):
            (daily / f"ttstats-202611{day:02d}-0245.dump.age").write_text("old")
        r = env.backup(now="2026-11-15T02:45", FAKE_FAIL="rclone-copyto:/weekly/")
        assert r.returncode == 6, _out(r)
        assert len(env.remote_files("daily")) == 11  # nothing pruned
        assert env.calls("rclone deletefile") == []
        assert env.checkins() == ["in_progress", "error"]

    def test_retention_failure_is_7_but_the_backup_is_uploaded(self, env):
        r = env.backup(FAKE_FAIL="rclone-lsf")
        assert r.returncode == 7, _out(r)
        assert env.remote_files("daily") == ["ttstats-20261113-0245.dump.age"]
        assert env.checkins() == ["in_progress", "error"]

    def test_check_in_failure_never_fails_the_backup(self, env):
        r = env.backup(FAKE_FAIL="curl")
        assert r.returncode == 0, _out(r)
        assert env.remote_files("daily") == ["ttstats-20261113-0245.dump.age"]
        assert "check-in 'ok' failed (ignored)" in r.stdout

    def test_missing_remote_is_a_config_error(self, env):
        del env.config["BACKUP_REMOTE"]
        r = env.backup()
        assert r.returncode == 2
        assert env.calls("docker") == []

    def test_missing_recipients_is_a_config_error(self, env):
        del env.config["BACKUP_AGE_RECIPIENTS"]
        assert env.backup().returncode == 2

    @pytest.mark.parametrize("args", [("--bogus",), ("--tag",), ("--tag", "bad/name")])
    def test_usage_errors_exit_2(self, env, args):
        r = env.backup(*args)
        assert r.returncode == 2
        assert env.checkins() == []

    def test_help(self, env):
        r = env.backup("--help")
        assert r.returncode == 0
        assert "Exit codes" in r.stdout
        assert "--dry-run" in r.stdout

    def test_bad_backup_now_is_rejected(self, env):
        assert env.backup(now="13/11/2026").returncode == 2


class TestRetention:
    def test_forty_days_leave_exactly_the_retention_set(self, env):
        env.config["BACKUP_LOCAL_DIR"] = "none"
        del env.config["SENTRY_CRON_URL_BACKUP"]
        start = date(2026, 9, 25)
        days = [start + timedelta(days=i) for i in range(40)]  # to 2026-11-03
        for d in days:
            r = env.backup(now=f"{d.isoformat()}T02:45")
            assert r.returncode == 0, _out(r)

        def names(ds):
            return sorted(f"ttstats-{d:%Y%m%d}-0245.dump.age" for d in ds)

        sundays = [d for d in days if d.weekday() == 6]
        firsts = [d for d in days if d.day == 1]
        assert env.remote_files("daily") == names(days[-7:])
        assert env.remote_files("weekly") == names(sundays[-4:])
        assert env.remote_files("monthly") == names(firsts)  # 2 of a possible 6
        assert env.remote_files("adhoc") == []

    def test_monthly_keeps_six_by_name_not_mtime(self, env):
        monthly = env.remote / "monthly"
        monthly.mkdir()
        for month in range(3, 11):  # 2026-03 .. 2026-10
            f = monthly / f"ttstats-2026{month:02d}01-0245.dump.age"
            f.write_text("old")
        # mtimes in the reverse order of the names: mtime must not matter
        now = time.time()
        for i, f in enumerate(sorted(monthly.iterdir())):
            os.utime(f, (now - i * 60, now - i * 60))
        r = env.backup(now="2026-11-01T02:45")
        assert r.returncode == 0, _out(r)
        assert env.remote_files("monthly") == [
            f"ttstats-2026{m:02d}01-0245.dump.age" for m in range(6, 12)
        ]

    def test_unknown_files_are_never_deleted(self, env):
        daily = env.remote / "daily"
        daily.mkdir()
        (daily / "README.txt").write_text("hands off")
        for day in range(1, 12):
            (daily / f"ttstats-202610{day:02d}-0245.dump.age").write_text("old")
        assert env.backup().returncode == 0
        assert "README.txt" in env.remote_files("daily")
        assert len(env.remote_files("daily")) == 8

    def test_stale_partials_are_swept_fresh_ones_kept(self, env):
        daily = env.remote / "daily"
        daily.mkdir()
        stale = daily / "ttstats-20261101-0245.dump.age.partial"
        fresh = daily / "ttstats-20261113-0100.dump.age.partial"
        stale.write_text("x")
        fresh.write_text("x")
        two_days_ago = time.time() - 2 * 86400
        os.utime(stale, (two_days_ago, two_days_ago))
        assert env.backup().returncode == 0
        assert not stale.exists()
        assert fresh.exists()

    def test_adhoc_keeps_the_newest_ten_and_dailies_are_untouched(self, env):
        env.config["BACKUP_LOCAL_DIR"] = "none"
        del env.config["SENTRY_CRON_URL_BACKUP"]
        assert env.backup(now="2026-11-01T02:45").returncode == 0
        for i in range(12):
            r = env.backup("--tag", f"pre-deploy-{i:02d}", now=f"2026-11-13T10:{i:02d}")
            assert r.returncode == 0, _out(r)
        adhoc = env.remote_files("adhoc")
        assert len(adhoc) == 10
        assert adhoc[0] == "ttstats-20261113-1002-pre-deploy-02.dump.age"
        assert env.remote_files("daily") == ["ttstats-20261101-0245.dump.age"]


class TestDryRunAndTag:
    def test_dry_run_uploads_nothing_and_checks_in_nothing(self, env):
        r = env.backup("--dry-run")
        assert r.returncode == 0, _out(r)
        assert env.calls("rclone") == []
        assert env.calls("curl") == []
        assert env.remote_files() == []
        assert list(env.local.iterdir()) == []
        assert list(env.tmp.iterdir()) == []
        assert len(env.calls("age")) == 1

    def test_dry_run_without_recipients_uses_a_throwaway_key(self, env):
        del env.config["BACKUP_AGE_RECIPIENTS"]
        del env.config["BACKUP_REMOTE"]
        r = env.backup("--dry-run")
        assert r.returncode == 0, _out(r)
        assert "throwaway key" in r.stdout
        assert "-r age1throwaway" in env.calls("age")[0]

    def test_dry_run_still_fails_on_a_bad_dump(self, env):
        r = env.backup("--dry-run", FAKE_DUMP_MODE="truncated")
        assert r.returncode == 4
        assert env.calls("curl") == []

    def test_tagged_backup_goes_to_adhoc_only(self, env):
        r = env.backup("--tag", "pre-deploy-abc1234", now="2026-11-15T09:00")  # a Sunday
        assert r.returncode == 0, _out(r)
        assert env.remote_files() == ["adhoc/ttstats-20261115-0900-pre-deploy-abc1234.dump.age"]


# -------------------------------------------------------------------- drill


def _backed_up(env, now="2026-11-13T02:45"):
    r = env.backup(now=now)
    assert r.returncode == 0, _out(r)
    (env.state / "checkins.log").unlink()
    (env.state / "calls.log").unlink()
    return env


class TestRestoreDrill:
    def test_happy_path(self, env):
        _backed_up(env)
        r = env.drill()
        assert r.returncode == 0, _out(r)
        assert "drill OK: ttstats-20261113-0245.dump.age" in r.stdout
        for t in TABLES:
            assert f"ok: {t} restore=100 prod=100" in r.stdout
        assert env.checkins() == ["in_progress", "ok"]
        assert env.started_containers() == env.removed_containers() != []
        assert list(env.tmp.iterdir()) == []

    def test_container_is_isolated_and_capped(self, env):
        _backed_up(env)
        env.drill()
        run = next(c for c in env.calls("docker") if c.startswith("docker run "))
        argv = run.split()
        assert argv[argv.index("--network") + 1] == "none"
        assert argv[argv.index("--memory") + 1] == "512m"
        assert any(a.startswith("/var/lib/postgresql/data") for a in argv)
        assert argv[-1] == "postgres:16"

    def test_picks_the_newest_daily_by_name(self, env):
        _backed_up(env, now="2026-11-12T02:45")
        _backed_up(env, now="2026-11-13T02:45")
        r = env.drill()
        assert "downloading daily/ttstats-20261113-0245.dump.age" in r.stdout

    def test_count_mismatch_fails_and_tears_down(self, env):
        _backed_up(env)
        env.set_prod(counts={**{t: 100 for t in TABLES}, "pingpong_pointevent": 150})
        r = env.drill()
        assert r.returncode == 7, _out(r)
        assert "pingpong_pointevent: restore has 100 rows, prod has 150" in r.stdout
        assert env.checkins() == ["in_progress", "error"]
        assert env.started_containers() == env.removed_containers() != []
        assert list(env.tmp.iterdir()) == []

    def test_empty_table_fails(self, env):
        env.set_prod(counts={**{t: 100 for t in TABLES}, "auth_user": 0})
        _backed_up(env)
        r = env.drill()
        assert r.returncode == 7
        assert "auth_user: restore has no rows" in r.stdout

    def test_within_one_percent_passes(self, env):
        _backed_up(env)
        env.set_prod(counts={**{t: 100 for t in TABLES}, "pingpong_match": 101})
        assert env.drill().returncode == 0

    def test_migration_mismatch_fails(self, env):
        _backed_up(env)
        env.set_prod(migration="pingpong.0053_next")
        r = env.drill()
        assert r.returncode == 7
        assert "latest migration differs" in r.stdout

    def test_truncated_file_fails_decryption_before_any_container(self, env):
        _backed_up(env)
        src = env.remote / "daily" / "ttstats-20261113-0245.dump.age"
        cut = env.root / "ttstats-20261113-0245.dump.age"
        cut.write_bytes(src.read_bytes()[:-20])
        r = env.drill("--file", str(cut))
        assert r.returncode == 4, _out(r)
        assert env.started_containers() == []
        assert env.checkins() == ["in_progress", "error"]
        assert list(env.tmp.iterdir()) == []

    def test_wrong_key_fails_decryption(self, env):
        env.config["BACKUP_AGE_RECIPIENTS"] = MAIN_PUB  # drill key not a recipient
        _backed_up(env)
        assert env.drill().returncode == 4

    def test_restore_failure_tears_down(self, env):
        _backed_up(env)
        r = env.drill(FAKE_FAIL="restore")
        assert r.returncode == 6, _out(r)
        assert env.started_containers() == env.removed_containers() != []
        assert list(env.tmp.iterdir()) == []

    def test_container_start_failure(self, env):
        _backed_up(env)
        r = env.drill(FAKE_FAIL="docker-run")
        assert r.returncode == 5
        assert env.removed_containers() == []

    def test_prod_query_failure(self, env):
        _backed_up(env)
        r = env.drill(FAKE_FAIL="prod-psql")
        assert r.returncode == 8
        assert env.started_containers() == env.removed_containers() != []

    def test_nothing_on_the_remote(self, env):
        r = env.drill()
        assert r.returncode == 3
        assert env.checkins() == ["in_progress", "error"]

    def test_stale_backup_name_fails(self, env):
        _backed_up(env)
        r = env.drill(FAKE_DUMP_AGE_HOURS="100")
        assert r.returncode == 7
        assert "is the nightly backup running?" in r.stdout

    def test_old_newest_match_only_warns_by_default(self, env):
        env.set_prod(match_age_days=9)
        _backed_up(env)
        r = env.drill()
        assert r.returncode == 0, _out(r)
        assert "WARNING: newest match is 9 day(s) old" in r.stdout

    def test_strict_freshness_fails_on_an_old_match(self, env):
        env.set_prod(match_age_days=9)
        _backed_up(env)
        assert env.drill("--strict-freshness").returncode == 7

    def test_skip_freshness_skips_the_check(self, env):
        env.set_prod(match_age_days=9)
        _backed_up(env)
        r = env.drill("--skip-freshness")
        assert r.returncode == 0
        assert "newest match" not in r.stdout

    def test_local_file_drill_skips_the_name_age_check(self, env):
        _backed_up(env)
        local = env.local / "ttstats-20261113-0245.dump.age"
        r = env.drill("--file", str(local), FAKE_DUMP_AGE_HOURS="1000")
        assert r.returncode == 0, _out(r)
        assert env.calls("rclone") == []

    def test_missing_identity_is_a_config_error(self, env):
        del env.config["BACKUP_DRILL_IDENTITY"]
        r = env.drill()
        assert r.returncode == 2
        assert env.checkins() == []


class TestScriptHygiene:
    @pytest.mark.parametrize("script", [BACKUP_SH, DRILL_SH])
    def test_executable_with_strict_mode(self, script):
        assert os.access(script, os.X_OK)
        assert "set -euo pipefail" in script.read_text()

    @pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
    def test_shellcheck_clean(self):
        r = subprocess.run(
            ["shellcheck", str(BACKUP_SH), str(DRILL_SH)], capture_output=True, text=True
        )
        assert r.returncode == 0, r.stdout

    def test_real_env_file_is_gitignored(self):
        assert "deploy/backup/backup.env" in (REPO / ".gitignore").read_text().split()


class TestMonitorUpsert:
    """KAN-111: the first check-in of a run carries the monitor's config, so
    the Sentry monitors are created and kept in step from the repo."""

    def _bodies(self, env):
        import json as _json
        log = env.state / "checkin_bodies.log"
        return [_json.loads(line) for line in log.read_text().splitlines()]

    def test_backup_in_progress_carries_the_schedule(self, env):
        r = env.backup()
        assert r.returncode == 0, r.stdout + r.stderr
        bodies = self._bodies(env)
        config = bodies[0]["monitor_config"]
        assert config["schedule"] == {"type": "crontab", "value": "45 2 * * *"}
        assert config["timezone"] == "UTC"
        assert bodies[-1] == {}
        # Same schedule as the crontab line.
        cron = (REPO / "deploy" / "cron" / "ttstats.cron").read_text()
        assert "45  2  *   *   *    /root/ttstats/deploy/backup/backup.sh" in cron

    def test_drill_in_progress_carries_the_monthly_schedule(self, env):
        env.backup()
        r = env.drill()
        assert r.returncode == 0, r.stdout + r.stderr
        config = self._bodies(env)[-2]["monitor_config"]
        assert config["schedule"]["value"] == "30 4 2 * *"
        cron = (REPO / "deploy" / "cron" / "ttstats.cron").read_text()
        assert "30  4  2   *   *    /root/ttstats/deploy/backup/restore_drill.sh" in cron
