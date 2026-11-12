#!/usr/bin/env bash
# Deploy one web image to this host (KAN-110). Run by CI over SSH after
# `git pull`, or by hand on the box:
#
#   cd /root/ttstats && ./deploy/deploy.sh ghcr.io/eyedema/ttstats-web:<sha> <sha>
#
# GHCR_USER/GHCR_TOKEN in the environment log in to the registry first (CI
# passes the run's own GITHUB_TOKEN); by hand, a prior `docker login` does.
#
# Every web restart pins WEB_IMAGE and SENTRY_RELEASE: compose.prod.yml falls
# back to the stale :latest otherwise (.claude/rules/ops.md).
#
# Env overrides (tests, by hand): COMPOSE_FILE, DEPLOY_STATE_DIR.
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
dcp pull web

log "up"
dcp up -d

log "migrate"
dcp exec -T web python manage.py migrate

log "collectstatic"
dcp exec -T web python manage.py collectstatic --noinput

echo "$IMAGE" > "$LAST_GOOD"

log "prune superseded images"
docker image prune -f >/dev/null

log "done: $IMAGE"
