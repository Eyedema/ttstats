#!/usr/bin/env bash
# Deploy one web image to this host (KAN-110). Run by CI over SSH after
# `git pull`, or by hand on the box:
#
#   cd /root/ttstats && ./deploy/deploy.sh ghcr.io/eyedema/ttstats-web:<sha> <sha>
#
# GHCR_USER/GHCR_TOKEN in the environment log in to the registry first (CI
# passes the run's own GITHUB_TOKEN); by hand, a prior `docker login` does.
#
#   1. read the running web image: the rollback target
#   2. pull the new image
#   3. pre-swap tasks, ONCE, with the new image, while the old container
#      still serves: makemigrations --check, migrate, collectstatic (KAN-114).
#      Migrations must therefore be expand/contract: the previous release
#      keeps running against the new schema until the swap, and after a
#      rollback (.claude/rules/ops.md).
#   4. swap, and wait for the container's healthcheck (/healthz, KAN-109)
#   5. smoke test through the public URL: /healthz reports this release, then
#      a few pages answer 200 (KAN-116)
#   6. on a failed 4 or 5: put PREVIOUS_IMAGE back, wait for it to be healthy,
#      report to Sentry, exit 1 so the workflow goes red. Migrations are not
#      rolled back.
#
# Every web restart pins WEB_IMAGE and SENTRY_RELEASE: compose.prod.yml falls
# back to the stale :latest otherwise.
#
# Exit codes: 0 deployed; 1 failed and rolled back (previous image serving);
#   2 usage; 3 failed before the swap (nothing changed, old image serving);
#   4 failed and the rollback failed too, or there was nothing to roll back to.
#
# Env overrides (tests, by hand): COMPOSE_FILE, DEPLOY_STATE_DIR, PUBLIC_URL,
# SMOKE_TIMEOUT (s, default 90), SMOKE_INTERVAL (s, default 2),
# WAIT_TIMEOUT (s, default 180, the healthcheck wait).
set -euo pipefail

usage() {
  echo "usage: $0 <image> <sha>" >&2
  exit 2
}

[ "$#" -eq 2 ] || usage
IMAGE="$1"
SHA="$2"
[ -n "$IMAGE" ] && [ -n "$SHA" ] || usage

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
COMPOSE_FILE="${COMPOSE_FILE:-compose.prod.yml}"
STATE_DIR="${DEPLOY_STATE_DIR:-$REPO_ROOT/deploy}"
LAST_GOOD="$STATE_DIR/.last-good-image"
PUBLIC_URL="${PUBLIC_URL:-https://pingpong.ubaldopuocci.org}"
SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-90}"
SMOKE_INTERVAL="${SMOKE_INTERVAL:-2}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"
# Pages a broken release cannot serve: two templates with forms (CSRF,
# static manifest), the PWA files rendered without a request, the offline page.
SMOKE_PATHS="/accounts/login/ /pingpong/login/ /manifest.webmanifest /sw.js /offline/"

START=$(date +%s)
log() { printf '[deploy +%3ss] %s\n' "$(( $(date +%s) - START ))" "$*"; }
dcp() { docker compose -f "$COMPOSE_FILE" "$@"; }

# The image web is running now, before anything changes: the rollback target.
running_web_image() {
  local id
  id="$(dcp ps -q web 2>/dev/null || true)"
  [ -n "$id" ] || return 0
  docker inspect --format '{{.Config.Image}}' "$id" 2>/dev/null || true
}

release_of() {
  # ghcr.io/eyedema/ttstats-web:<sha> -> <sha>; CI tags every image by SHA.
  echo "${1##*:}"
}

# Bring web up on an image and wait for its healthcheck. Pinned, always.
swap_to() {
  local image="$1" release="$2"
  WEB_IMAGE="$image" SENTRY_RELEASE="$release" \
    dcp up -d --wait --wait-timeout "$WAIT_TIMEOUT" web
}

# Through the real proxy: /healthz must report the expected release, then
# every smoke page must answer 200.
smoke() {
  local release="$1" deadline body path code
  deadline=$(( $(date +%s) + SMOKE_TIMEOUT ))
  while :; do
    body="$(curl -fsS --max-time 5 -H 'Cache-Control: no-cache' "$PUBLIC_URL/healthz" 2>/dev/null || true)"
    if printf '%s' "$body" | grep -q "\"release\": \"$release\""; then
      break
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      log "smoke: /healthz never reported release $release (last: ${body:-no answer})"
      return 1
    fi
    sleep "$SMOKE_INTERVAL"
  done
  log "smoke: /healthz ok ($body)"
  for path in $SMOKE_PATHS; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$PUBLIC_URL$path" 2>/dev/null || true)"
    if [ "$code" != "200" ]; then
      log "smoke: $path answered ${code:-nothing}"
      return 1
    fi
  done
  log "smoke: pages ok"
}

# Best effort, never fails the script: the workflow going red is the
# guaranteed signal; this puts the two SHAs in the same inbox as app errors.
# Runs inside the container that is serving, which has the SDK and the DSN.
notify_sentry() {
  local message="$1"
  DEPLOY_MESSAGE="$message" dcp exec -T -e DEPLOY_MESSAGE web python -c '
import os
dsn = os.environ.get("SENTRY_DSN", "").strip()
if dsn:
    import sentry_sdk
    sentry_sdk.init(dsn=dsn, release=os.environ.get("SENTRY_RELEASE") or None)
    sentry_sdk.capture_message(os.environ["DEPLOY_MESSAGE"], level="error")
    sentry_sdk.flush(5)
' >/dev/null 2>&1 || log "could not report to Sentry (ignored)"
}

rollback() {
  local reason="$1"
  if [ -z "$PREVIOUS_IMAGE" ]; then
    log "FAILED ($reason) and there is no previous image to roll back to"
    exit 4
  fi
  local prev_release
  prev_release="$(release_of "$PREVIOUS_IMAGE")"
  log "FAILED ($reason): rolling back to $PREVIOUS_IMAGE"
  if swap_to "$PREVIOUS_IMAGE" "$prev_release" && smoke "$prev_release"; then
    notify_sentry "Deploy of $SHA failed ($reason); rolled back to $prev_release"
    log "rolled back: $PREVIOUS_IMAGE is serving. Migrations were NOT reverted."
    exit 1
  fi
  notify_sentry "Deploy of $SHA failed ($reason) and the rollback to $prev_release failed too"
  log "ROLLBACK FAILED: check 'docker compose -f $COMPOSE_FILE ps' now"
  exit 4
}

PREVIOUS_IMAGE="$(running_web_image)"
if [ -z "$PREVIOUS_IMAGE" ] && [ -f "$LAST_GOOD" ]; then
  PREVIOUS_IMAGE="$(cat "$LAST_GOOD")"
fi
log "deploying $IMAGE (release $SHA); previous: ${PREVIOUS_IMAGE:-none}"

if [ -n "${GHCR_TOKEN:-}" ]; then
  log "registry login"
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "${GHCR_USER:-}" --password-stdin >/dev/null
  trap 'docker logout ghcr.io >/dev/null 2>&1 || true' EXIT
fi

export WEB_IMAGE="$IMAGE" SENTRY_RELEASE="$SHA"

log "pull"
dcp pull web || { log "pull failed; nothing changed"; exit 3; }

log "pre-swap: makemigrations --check, migrate, collectstatic (old container still serving)"
if ! dcp run --rm --no-deps -T --entrypoint sh web -c \
  'python manage.py makemigrations --check --dry-run && python manage.py migrate --noinput && python manage.py collectstatic --noinput -v 0'; then
  log "pre-swap tasks failed; the old container is still serving"
  exit 3
fi

log "swap: web -> $IMAGE, waiting for healthy"
# db and redis too, as before: a config change to either is applied here.
# Neither has a healthcheck, so --wait only waits for them to be running.
if ! dcp up -d --wait --wait-timeout "$WAIT_TIMEOUT"; then
  rollback "new container never became healthy"
fi

log "smoke test via $PUBLIC_URL"
if ! smoke "$SHA"; then
  rollback "smoke test failed"
fi

echo "$IMAGE" > "$LAST_GOOD"

log "prune superseded images"
docker image prune -f >/dev/null || true

log "done: $IMAGE"
