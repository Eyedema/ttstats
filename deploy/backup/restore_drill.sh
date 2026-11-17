#!/usr/bin/env bash
# TTStats restore drill (KAN-108).
#
# Downloads the newest daily/ backup (or takes --file), decrypts it with the
# drill-only age identity, restores it into a throwaway postgres:16 container
# (no network, tmpfs data dir, capped memory) and checks it against prod:
#
#   - row counts of the key tables are > 0 and >= DRILL_MIN_PERCENT of prod's
#   - the latest django_migrations row equals prod's
#   - the newest match is at most DRILL_MAX_MATCH_AGE_DAYS old (warn-only by
#     default: a quiet fortnight is plausible; --strict-freshness fails on it)
#   - when downloading: the newest daily object's name is at most
#     DRILL_MAX_DUMP_AGE_HOURS old (this one always fails: it means the
#     nightly backup stopped)
#
# The restore is pg_restore --no-owner --no-privileges into an empty database.
# NEVER `manage.py migrate` a fresh Postgres: migration 0010 cannot apply to
# one (KAN-75). Prod is only read, in read-only transactions.
#
# Usage: restore_drill.sh [--file PATH.dump.age] [--strict-freshness]
#                         [--skip-freshness] [--keep-container] [--help]
#
# Exit codes:
#   0  ok (warnings may have been logged)
#   1  unexpected error
#   2  usage or configuration error
#   3  download failed, or no backup found on the remote
#   4  decryption failed (wrong key, truncated or corrupt file)
#   5  the throwaway container did not start or never became ready
#   6  pg_restore failed
#   7  a sanity check failed
#   8  querying prod failed
#
# Configuration: the same env file as backup.sh (BACKUP_ENV_FILE, default
# /root/ttstats/deploy/backup/backup.env). See backup.env.example.

set -euo pipefail

PROG=ttstats-drill
DEFAULT_ENV_FILE=/root/ttstats/deploy/backup/backup.env

log() { printf '%s %s: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PROG" "$*"; }
usage() { sed -n '2,/^# Configuration/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; }
fail() {
    local code=$1
    shift
    log "ERROR: $*"
    exit "$code"
}

FILE=""
FRESHNESS=warn   # warn | strict | skip
KEEP_CONTAINER=0
while [ $# -gt 0 ]; do
    case "$1" in
        --file)
            [ $# -ge 2 ] || { echo "--file needs a path" >&2; exit 2; }
            FILE=$2
            shift
            ;;
        --file=*) FILE=${1#--file=} ;;
        --strict-freshness) FRESHNESS=strict ;;
        --skip-freshness) FRESHNESS=skip ;;
        --keep-container) KEEP_CONTAINER=1 ;;
        -h | --help) usage; exit 0 ;;
        *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

if [ -n "${BACKUP_ENV_FILE:-}" ]; then
    [ -r "$BACKUP_ENV_FILE" ] || { echo "BACKUP_ENV_FILE not readable: $BACKUP_ENV_FILE" >&2; exit 2; }
    # shellcheck source=/dev/null
    . "$BACKUP_ENV_FILE"
elif [ -r "$DEFAULT_ENV_FILE" ]; then
    # shellcheck source=/dev/null
    . "$DEFAULT_ENV_FILE"
fi

BACKUP_PROJECT_DIR=${BACKUP_PROJECT_DIR:-/root/ttstats}
BACKUP_COMPOSE_FILE=${BACKUP_COMPOSE_FILE:-compose.prod.yml}
BACKUP_DB_SERVICE=${BACKUP_DB_SERVICE:-db}
BACKUP_REMOTE=${BACKUP_REMOTE:-}
BACKUP_DRILL_IDENTITY=${BACKUP_DRILL_IDENTITY:-}
BACKUP_TMPDIR=${BACKUP_TMPDIR:-${TMPDIR:-/tmp}}
DRILL_IMAGE=${DRILL_IMAGE:-postgres:16}
DRILL_MEMORY=${DRILL_MEMORY:-512m}
DRILL_READY_TIMEOUT=${DRILL_READY_TIMEOUT:-90}
DRILL_MIN_PERCENT=${DRILL_MIN_PERCENT:-99}
DRILL_MAX_MATCH_AGE_DAYS=${DRILL_MAX_MATCH_AGE_DAYS:-2}
DRILL_MAX_DUMP_AGE_HOURS=${DRILL_MAX_DUMP_AGE_HOURS:-50}
DRILL_TABLES=${DRILL_TABLES:-"pingpong_match pingpong_pointevent pingpong_playerrating auth_user"}
SENTRY_CRON_URL_DRILL=${SENTRY_CRON_URL_DRILL:-}
if [ -n "${BACKUP_RCLONE_CONFIG:-}" ]; then
    export RCLONE_CONFIG=$BACKUP_RCLONE_CONFIG
fi
for n in "$DRILL_MIN_PERCENT" "$DRILL_MAX_MATCH_AGE_DAYS" "$DRILL_MAX_DUMP_AGE_HOURS" "$DRILL_READY_TIMEOUT"; do
    case "$n" in '' | *[!0-9]*) fail 2 "DRILL_* thresholds must be non-negative integers (got '$n')" ;; esac
done

CHECKIN_ARMED=0
WORK=""
CONTAINER=""
WARNINGS=0

checkin() {
    local status=$1 url=$SENTRY_CRON_URL_DRILL sep='?'
    [ -n "$url" ] || return 0
    case "$url" in *\?*) sep='&' ;; esac
    if curl -fsS --max-time 10 --retry 2 -o /dev/null "${url}${sep}status=${status}" 2>/dev/null; then
        log "check-in: $status"
    else
        log "WARNING: Sentry check-in '$status' failed (ignored)"
    fi
    return 0
}

on_exit() {
    local rc=$?
    set +e
    if [ -n "$CONTAINER" ]; then
        if [ "$KEEP_CONTAINER" = 1 ]; then
            log "keeping container $CONTAINER (--keep-container); remove it with: docker rm -f $CONTAINER"
        else
            docker rm -f "$CONTAINER" >/dev/null 2>&1 && log "removed container $CONTAINER"
        fi
    fi
    [ -n "$WORK" ] && rm -rf "$WORK"
    if [ "$rc" -ne 0 ] && [ "$CHECKIN_ARMED" = 1 ]; then
        checkin error
        log "drill FAILED (exit $rc)"
    fi
    exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[ -n "$BACKUP_DRILL_IDENTITY" ] || fail 2 "BACKUP_DRILL_IDENTITY is not set"
[ -r "$BACKUP_DRILL_IDENTITY" ] || fail 2 "BACKUP_DRILL_IDENTITY not readable: $BACKUP_DRILL_IDENTITY"
if [ -n "$FILE" ]; then
    [ -r "$FILE" ] || fail 2 "--file not readable: $FILE"
    case "$FILE" in /*) ;; *) FILE="$PWD/$FILE" ;; esac
else
    [ -n "$BACKUP_REMOTE" ] || fail 2 "BACKUP_REMOTE is not set (or pass --file)"
fi
cd "$BACKUP_PROJECT_DIR" || fail 2 "BACKUP_PROJECT_DIR not found: $BACKUP_PROJECT_DIR"

CHECKIN_ARMED=1
log "starting restore drill"
checkin in_progress

umask 077
WORK=$(mktemp -d "$BACKUP_TMPDIR/ttstats-drill.XXXXXX") || fail 1 "mktemp failed"

# ---------------------------------------------------------------- fetch
BACKUP_NAME_RE='^ttstats-[0-9]{8}-[0-9]{4}(-[A-Za-z0-9._-]+)?\.dump\.age$'
is_backup_name() { [[ $1 =~ $BACKUP_NAME_RE ]]; }

if [ -n "$FILE" ]; then
    ENC=$FILE
    NAME=$(basename "$FILE")
    log "drilling local file $FILE"
else
    REMOTE=${BACKUP_REMOTE%/}
    listing=$(rclone lsf --files-only "$REMOTE/daily/") || fail 3 "could not list $REMOTE/daily/"
    NAME=""
    while IFS= read -r n; do
        if is_backup_name "$n"; then
            printf '%s %s\n' "${n:8:13}" "$n"
        fi
    done <<EOF >"$WORK/candidates"
$listing
EOF
    NAME=$(sort -r "$WORK/candidates" | head -n 1 | cut -d' ' -f2-)
    [ -n "$NAME" ] || fail 3 "no backup found under $REMOTE/daily/"
    ENC="$WORK/$NAME"
    log "downloading daily/$NAME"
    rclone copyto "$REMOTE/daily/$NAME" "$ENC" || fail 3 "download of daily/$NAME failed"
fi
[ -s "$ENC" ] || fail 4 "$NAME is empty"

# -------------------------------------------------------------- decrypt
DUMP="$WORK/drill.dump"
age -d -i "$BACKUP_DRILL_IDENTITY" -o "$DUMP" "$ENC" \
    || fail 4 "could not decrypt $NAME with the drill identity (wrong key, or truncated/corrupt)"
[ -s "$DUMP" ] || fail 4 "decrypted dump is empty"
log "decrypted $NAME ($(wc -c <"$DUMP" | tr -d ' ') bytes)"

# ------------------------------------------------------------ container
CONTAINER="ttstats-drill-$(date -u +%Y%m%d%H%M%S)-$$"
docker run -d --rm --name "$CONTAINER" \
    --network none --memory "$DRILL_MEMORY" \
    --tmpfs /var/lib/postgresql/data:rw,size=2g \
    -e POSTGRES_PASSWORD=drill -e POSTGRES_DB=drill \
    "$DRILL_IMAGE" >/dev/null || { CONTAINER=""; fail 5 "docker run $DRILL_IMAGE failed"; }
log "started throwaway container $CONTAINER"

# The image's init runs a socket-only server first, then restarts. Waiting on
# TCP (127.0.0.1, inside the container) skips that first server.
waited=0
until docker exec "$CONTAINER" pg_isready -q -h 127.0.0.1 -U postgres -d drill >/dev/null 2>&1; do
    waited=$((waited + 1))
    [ "$waited" -lt "$DRILL_READY_TIMEOUT" ] || fail 5 "container never became ready (${DRILL_READY_TIMEOUT}s)"
    sleep 1
done

drill_sql() {
    printf '%s\n' "$1" | docker exec -i "$CONTAINER" \
        psql -h 127.0.0.1 -U postgres -d drill -XAtq -v ON_ERROR_STOP=1
}
prod_sql() {
    # shellcheck disable=SC2016  # $POSTGRES_* expand inside the container
    printf '%s\n' "$1" | docker compose -f "$BACKUP_COMPOSE_FILE" exec -T "$BACKUP_DB_SERVICE" sh -c \
        'PGOPTIONS="-c default_transaction_read_only=on" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -XAtq -v ON_ERROR_STOP=1'
}

# -------------------------------------------------------------- restore
log "restoring"
docker exec -i "$CONTAINER" pg_restore -h 127.0.0.1 -U postgres -d drill \
    --no-owner --no-privileges --exit-on-error <"$DUMP" >"$WORK/restore.log" 2>&1 \
    || { sed 's/^/  /' "$WORK/restore.log" | tail -n 20; fail 6 "pg_restore failed"; }
rm -f "$DUMP"
log "restore complete"

# --------------------------------------------------------------- checks
failed=0
check_fail() { log "CHECK FAILED: $*"; failed=1; }

for t in $DRILL_TABLES; do
    case "$t" in *[!a-z0-9_]*) fail 2 "bad table name in DRILL_TABLES: $t" ;; esac
    got=$(drill_sql "SELECT count(*) FROM $t;") || { check_fail "$t: not queryable in the restore"; continue; }
    want=$(prod_sql "SELECT count(*) FROM $t;") || fail 8 "could not count $t on prod"
    case "$got$want" in *[!0-9]* | '') check_fail "$t: non-numeric counts ('$got' / '$want')"; continue ;; esac
    if [ "$got" -le 0 ]; then
        check_fail "$t: restore has no rows"
    elif [ $((got * 100)) -lt $((want * DRILL_MIN_PERCENT)) ]; then
        check_fail "$t: restore has $got rows, prod has $want (< ${DRILL_MIN_PERCENT}%)"
    else
        log "ok: $t restore=$got prod=$want"
    fi
done

MIG_SQL="SELECT app || '.' || name FROM django_migrations WHERE app = 'pingpong' ORDER BY id DESC LIMIT 1;"
got=$(drill_sql "$MIG_SQL") || got="(unreadable)"
want=$(prod_sql "$MIG_SQL") || fail 8 "could not read django_migrations on prod"
if [ "$got" = "$want" ]; then
    log "ok: latest migration $got"
else
    check_fail "latest migration differs: restore=$got prod=$want (a deploy since the dump?)"
fi

if [ "$FRESHNESS" != skip ]; then
    days=$(drill_sql "SELECT coalesce(floor(extract(epoch FROM now() - max(date_played)) / 86400)::int, -1) FROM pingpong_match;") \
        || days=-1
    if [ "$days" -ge 0 ] && [ "$days" -le "$DRILL_MAX_MATCH_AGE_DAYS" ]; then
        log "ok: newest match is $days day(s) old"
    elif [ "$FRESHNESS" = strict ]; then
        check_fail "newest match is $days day(s) old (> $DRILL_MAX_MATCH_AGE_DAYS)"
    else
        log "WARNING: newest match is $days day(s) old (> $DRILL_MAX_MATCH_AGE_DAYS); a quiet spell, or a stale backup"
        WARNINGS=$((WARNINGS + 1))
    fi
fi

if [ -z "$FILE" ]; then
    # The name's timestamp, aged by the restored server's clock (UTC).
    key="${NAME:8:8}${NAME:17:4}"   # YYYYmmddHHMM
    hours=$(drill_sql "SET TIME ZONE 'UTC'; SELECT floor(extract(epoch FROM now() - to_timestamp('$key', 'YYYYMMDDHH24MI')) / 3600)::int;") \
        || hours=-1
    if [ "$hours" -ge 0 ] && [ "$hours" -le "$DRILL_MAX_DUMP_AGE_HOURS" ]; then
        log "ok: newest daily backup is ${hours}h old"
    else
        check_fail "newest daily backup $NAME is ${hours}h old (> ${DRILL_MAX_DUMP_AGE_HOURS}h): is the nightly backup running?"
    fi
fi

[ "$failed" = 0 ] || fail 7 "sanity checks failed for $NAME"
checkin ok
log "drill OK: $NAME restored and checked ($WARNINGS warning(s))"
