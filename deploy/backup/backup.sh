#!/usr/bin/env bash
# TTStats off-host database backup (KAN-118).
#
# pg_dump (custom format) inside the prod `db` container -> verify with
# pg_restore --list -> encrypt with age -> upload with rclone (to a .partial
# name, then moveto) -> rotate daily/weekly/monthly on the remote -> Sentry
# cron check-in. See deploy/backup/README.md.
#
# Usage: backup.sh [--dry-run] [--tag NAME] [--help]
#
#   --dry-run   dump, verify and encrypt in the temp dir only. No upload, no
#               retention, no local copy, no check-ins. Without configured
#               recipients a throwaway age key is generated in the temp dir.
#   --tag NAME  ad-hoc backup (e.g. pre-deploy-<sha>): goes to adhoc/, never to
#               daily/weekly/monthly; adhoc/ keeps the newest BACKUP_KEEP_ADHOC.
#
# Exit codes:
#   0  ok
#   1  unexpected error
#   2  usage or configuration error
#   3  pg_dump failed
#   4  verification failed (empty dump, or pg_restore --list rejected it)
#   5  encryption failed
#   6  upload failed (nothing is deleted from the remote)
#   7  retention failed (the new backup IS on the remote)
#
# Configuration comes from the environment and from an env file sourced at
# start (BACKUP_ENV_FILE, default /root/ttstats/deploy/backup/backup.env;
# values in the file win). See backup.env.example for every variable.
#
# Bash 3.2 compatible (macOS runs the tests): no associative arrays, no
# mapfile, and empty arrays are expanded with ${a[@]+"${a[@]}"}.

set -euo pipefail

PROG=ttstats-backup
DEFAULT_ENV_FILE=/root/ttstats/deploy/backup/backup.env

log() { printf '%s %s: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PROG" "$*"; }

usage() {
    sed -n '2,/^# Configuration/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

DRY_RUN=0
TAG=""
CHECKIN_ARMED=0
WORK=""

fail() {
    # fail CODE MESSAGE...
    local code=$1
    shift
    log "ERROR: $*"
    exit "$code"
}

# ---------------------------------------------------------------- arguments
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --tag)
            [ $# -ge 2 ] || { echo "--tag needs a value" >&2; exit 2; }
            TAG=$2
            shift
            ;;
        --tag=*) TAG=${1#--tag=} ;;
        -h | --help) usage; exit 0 ;;
        *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

if [ -n "$TAG" ]; then
    case "$TAG" in
        *[!A-Za-z0-9._-]*) echo "--tag may only contain A-Z a-z 0-9 . _ -" >&2; exit 2 ;;
    esac
    [ ${#TAG} -le 64 ] || { echo "--tag is longer than 64 characters" >&2; exit 2; }
fi

# ------------------------------------------------------------------- config
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
BACKUP_VERIFY_RUNNER=${BACKUP_VERIFY_RUNNER:-container}   # container | host
BACKUP_REMOTE=${BACKUP_REMOTE:-}
BACKUP_AGE_RECIPIENTS=${BACKUP_AGE_RECIPIENTS:-}
BACKUP_AGE_RECIPIENTS_FILE=${BACKUP_AGE_RECIPIENTS_FILE:-}
BACKUP_LOCAL_DIR=${BACKUP_LOCAL_DIR:-/root/ttstats-backups}  # "none" disables
BACKUP_LOCAL_KEEP=${BACKUP_LOCAL_KEEP:-2}
BACKUP_KEEP_DAILY=${BACKUP_KEEP_DAILY:-7}
BACKUP_KEEP_WEEKLY=${BACKUP_KEEP_WEEKLY:-4}
BACKUP_KEEP_MONTHLY=${BACKUP_KEEP_MONTHLY:-6}
BACKUP_KEEP_ADHOC=${BACKUP_KEEP_ADHOC:-10}
BACKUP_PARTIAL_MAX_AGE=${BACKUP_PARTIAL_MAX_AGE:-24h}
BACKUP_TMPDIR=${BACKUP_TMPDIR:-${TMPDIR:-/tmp}}
SENTRY_CRON_URL_BACKUP=${SENTRY_CRON_URL_BACKUP:-}
if [ -n "${BACKUP_RCLONE_CONFIG:-}" ]; then
    export RCLONE_CONFIG=$BACKUP_RCLONE_CONFIG
fi

for n in "$BACKUP_LOCAL_KEEP" "$BACKUP_KEEP_DAILY" "$BACKUP_KEEP_WEEKLY" \
    "$BACKUP_KEEP_MONTHLY" "$BACKUP_KEEP_ADHOC"; do
    case "$n" in
        '' | *[!0-9]*) fail 2 "retention counts must be non-negative integers (got '$n')" ;;
    esac
done
case "$BACKUP_VERIFY_RUNNER" in
    container | host) ;;
    *) fail 2 "BACKUP_VERIFY_RUNNER must be 'container' or 'host'" ;;
esac

# The run's timestamp. BACKUP_NOW (YYYY-mm-ddTHH:MM, UTC) is injectable so the
# tests can simulate days; otherwise it is the current UTC time.
NOW=${BACKUP_NOW:-$(date -u +%Y-%m-%dT%H:%M)}
case "$NOW" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]) ;;
    *) fail 2 "BACKUP_NOW must look like 2026-11-15T02:45 (got '$NOW')" ;;
esac
Y=${NOW:0:4} M=${NOW:5:2} D=${NOW:8:2} HH=${NOW:11:2} MI=${NOW:14:2}

# Day of week, 0 = Sunday (Sakamoto). Pure arithmetic: BSD and GNU date
# disagree on how to parse a given date.
day_of_week() {
    local y=$((10#$1)) m=$((10#$2)) d=$((10#$3))
    local t=(0 3 2 5 0 3 5 1 4 6 2 4)
    [ "$m" -lt 3 ] && y=$((y - 1))
    echo $(((y + y / 4 - y / 100 + y / 400 + t[m - 1] + d) % 7))
}
DOW=$(day_of_week "$Y" "$M" "$D")

BASENAME="ttstats-${Y}${M}${D}-${HH}${MI}${TAG:+-$TAG}"
DUMP_NAME="$BASENAME.dump"
ENC_NAME="$DUMP_NAME.age"

# --------------------------------------------------------------- check-ins
checkin() {
    # Never fails the backup: a Sentry outage must not lose a night's dump.
    local status=$1 url=$SENTRY_CRON_URL_BACKUP sep='?'
    [ -n "$url" ] || return 0
    [ "$DRY_RUN" = 1 ] && return 0
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
    if [ "$rc" -ne 0 ] && [ "$CHECKIN_ARMED" = 1 ]; then
        checkin error
        log "backup FAILED (exit $rc)"
    fi
    [ -n "$WORK" ] && rm -rf "$WORK"
    exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ----------------------------------------------------------- name handling
# Every object we manage is named ttstats-YYYYmmdd-HHMM[-tag].dump.age; the
# sort key is the timestamp in the name (characters 9-21), never the mtime.
BACKUP_NAME_RE='^ttstats-[0-9]{8}-[0-9]{4}(-[A-Za-z0-9._-]+)?\.dump\.age$'
is_backup_name() { [[ $1 =~ $BACKUP_NAME_RE ]]; }

# Reads names on stdin, prints the backup names newest first.
newest_first() {
    local n
    while IFS= read -r n; do
        if is_backup_name "$n"; then
            printf '%s %s\n' "${n:8:13}" "$n"
        fi
    done | sort -r | cut -d' ' -f2-
}

# ------------------------------------------------------------------- start
if [ "$DRY_RUN" != 1 ]; then
    [ -n "$BACKUP_REMOTE" ] || fail 2 "BACKUP_REMOTE is not set"
    [ -n "$BACKUP_AGE_RECIPIENTS$BACKUP_AGE_RECIPIENTS_FILE" ] \
        || fail 2 "no age recipients: set BACKUP_AGE_RECIPIENTS or BACKUP_AGE_RECIPIENTS_FILE"
    CHECKIN_ARMED=1
fi
if [ -n "$BACKUP_AGE_RECIPIENTS_FILE" ] && [ ! -r "$BACKUP_AGE_RECIPIENTS_FILE" ]; then
    fail 2 "BACKUP_AGE_RECIPIENTS_FILE not readable: $BACKUP_AGE_RECIPIENTS_FILE"
fi
cd "$BACKUP_PROJECT_DIR" || fail 2 "BACKUP_PROJECT_DIR not found: $BACKUP_PROJECT_DIR"

mode=""
[ "$DRY_RUN" = 1 ] && mode=" (dry run)"
log "starting ${TAG:+tagged }backup $ENC_NAME$mode"
checkin in_progress

umask 077
WORK=$(mktemp -d "$BACKUP_TMPDIR/ttstats-backup.XXXXXX") || fail 1 "mktemp failed"
DUMP="$WORK/$DUMP_NAME"
ENC="$WORK/$ENC_NAME"

db_exec() {
    docker compose -f "$BACKUP_COMPOSE_FILE" exec -T "$BACKUP_DB_SERVICE" "$@"
}

# -------------------------------------------------------------------- dump
log "dumping database"
# shellcheck disable=SC2016  # $POSTGRES_* expand inside the container
db_exec sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-privileges' \
    >"$DUMP" || fail 3 "pg_dump failed"

# ------------------------------------------------------------------ verify
[ -s "$DUMP" ] || fail 4 "dump is empty"
if [ "$BACKUP_VERIFY_RUNNER" = host ]; then
    pg_restore --list "$DUMP" >"$WORK/toc.txt" 2>"$WORK/toc.err"
else
    db_exec pg_restore --list <"$DUMP" >"$WORK/toc.txt" 2>"$WORK/toc.err"
fi || { sed 's/^/  /' "$WORK/toc.err"; fail 4 "pg_restore --list rejected the dump"; }
entries=$(grep -vc '^;' "$WORK/toc.txt" || true)
size=$(wc -c <"$DUMP" | tr -d ' ')
log "dump verified: $size bytes, $entries TOC entries"

# ----------------------------------------------------------------- encrypt
recipient_args=()
if [ -n "$BACKUP_AGE_RECIPIENTS" ]; then
    for r in $(printf '%s' "$BACKUP_AGE_RECIPIENTS" | tr ',' ' '); do
        recipient_args+=(-r "$r")
    done
fi
[ -n "$BACKUP_AGE_RECIPIENTS_FILE" ] && recipient_args+=(-R "$BACKUP_AGE_RECIPIENTS_FILE")
if [ ${#recipient_args[@]} -eq 0 ]; then
    # Dry run only (a real run failed earlier): throwaway key, never kept.
    age-keygen -o "$WORK/throwaway.key" 2>/dev/null || fail 5 "age-keygen failed"
    recipient_args=(-r "$(age-keygen -y "$WORK/throwaway.key")") || fail 5 "age-keygen -y failed"
    log "dry run: encrypting to a throwaway key"
fi
age ${recipient_args[@]+"${recipient_args[@]}"} -o "$ENC" "$DUMP" || fail 5 "age encryption failed"
[ -s "$ENC" ] || fail 5 "encrypted file is empty"
rm -f "$DUMP"   # the plaintext never leaves $WORK, and leaves it as soon as possible
log "encrypted: $ENC_NAME ($(wc -c <"$ENC" | tr -d ' ') bytes)"

if [ "$DRY_RUN" = 1 ]; then
    log "dry run: skipping local copy, upload, retention and check-ins"
    log "dry run OK"
    exit 0
fi

# -------------------------------------------------------------- local copy
if [ "$BACKUP_LOCAL_DIR" != none ]; then
    if mkdir -p "$BACKUP_LOCAL_DIR" && cp "$ENC" "$BACKUP_LOCAL_DIR/$ENC_NAME"; then
        for f in "$BACKUP_LOCAL_DIR"/ttstats-*.dump.age; do
            if [ -e "$f" ]; then basename "$f"; fi
        done | newest_first | tail -n +$((BACKUP_LOCAL_KEEP + 1)) \
            | while IFS= read -r old; do rm -f "$BACKUP_LOCAL_DIR/$old"; done
        log "local copy kept in $BACKUP_LOCAL_DIR (newest $BACKUP_LOCAL_KEEP)"
    else
        log "WARNING: could not write the local copy to $BACKUP_LOCAL_DIR (continuing)"
    fi
fi

# ------------------------------------------------------------------ upload
# Upload under .partial, then rename: an interrupted upload never carries a
# name the retention or restore code would treat as a backup.
put() {
    # put SOURCE DEST_PATH (SOURCE is local, or a remote path for a copy)
    rclone copyto "$1" "$2.partial" && rclone moveto "$2.partial" "$2"
}

REMOTE=${BACKUP_REMOTE%/}
if [ -n "$TAG" ]; then
    PRIMARY="$REMOTE/adhoc/$ENC_NAME"
else
    PRIMARY="$REMOTE/daily/$ENC_NAME"
fi
log "uploading to $PRIMARY"
put "$ENC" "$PRIMARY" || fail 6 "upload of $ENC_NAME failed"
uploaded="$PRIMARY"
if [ -z "$TAG" ]; then
    if [ "$DOW" = 0 ]; then
        put "$PRIMARY" "$REMOTE/weekly/$ENC_NAME" || fail 6 "weekly copy failed"
        uploaded="$uploaded weekly"
    fi
    if [ "$D" = 01 ]; then
        put "$PRIMARY" "$REMOTE/monthly/$ENC_NAME" || fail 6 "monthly copy failed"
        uploaded="$uploaded monthly"
    fi
fi
log "uploaded: $uploaded"

# --------------------------------------------------------------- retention
# Only reached when every upload above succeeded.
prune() {
    # prune PREFIX KEEP: keep the newest KEEP backups (by name) under PREFIX.
    local prefix=$1 keep=$2 listing old rc=0
    # rclone exits 3 for "directory not found": a prefix never written yet
    # (weekly/ before the first Sunday) is empty, not an error.
    listing=$(rclone lsf --files-only "$REMOTE/$prefix/" 2>"$WORK/lsf.err") || rc=$?
    if [ "$rc" = 3 ]; then
        return 0
    elif [ "$rc" != 0 ]; then
        cat "$WORK/lsf.err"
        return 1
    fi
    printf '%s\n' "$listing" | newest_first | tail -n +$((keep + 1)) >"$WORK/prune.$prefix"
    while IFS= read -r old; do
        [ "$old" = "$ENC_NAME" ] && continue   # never the backup we just made
        rclone deletefile "$REMOTE/$prefix/$old" || return 1
        log "retention: deleted $prefix/$old"
    done <"$WORK/prune.$prefix"
}

retention_ok=1
if [ -z "$TAG" ]; then
    prune daily "$BACKUP_KEEP_DAILY" || retention_ok=0
    prune weekly "$BACKUP_KEEP_WEEKLY" || retention_ok=0
    prune monthly "$BACKUP_KEEP_MONTHLY" || retention_ok=0
fi
prune adhoc "$BACKUP_KEEP_ADHOC" || retention_ok=0
# Stray uploads interrupted before their rename. The age test uses the
# object's mtime: a .partial's name may belong to a run still in flight.
rclone delete "$REMOTE" --include '*.partial' --min-age "$BACKUP_PARTIAL_MAX_AGE" || retention_ok=0
[ "$retention_ok" = 1 ] || fail 7 "retention failed (the new backup is uploaded)"

checkin ok
log "backup OK: $ENC_NAME"
