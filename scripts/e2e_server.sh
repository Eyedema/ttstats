#!/usr/bin/env bash
# Boot the app for the Playwright suite against a throwaway SQLite file.
#
# Two things this deliberately guarantees:
#   1. The CSS is rebuilt first. The e2e suite exists to catch problems in the
#      compiled stylesheet (a purged class, a missing rule); running it against
#      a stale build would defeat the point.
#   2. DATABASE_URL is cleared, so an exported Supabase URL can never point the
#      suite at the clone of production.
set -euo pipefail

PORT="${1:-8125}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export DJANGO_SETTINGS_MODULE=ttstats.settings.dev
unset DATABASE_URL || true

E2E_DB="$REPO_ROOT/ttstats/e2e.sqlite3"
export TTSTATS_SQLITE_NAME="$E2E_DB"

npm run build:css >/dev/null

cd "$REPO_ROOT/ttstats"
PY="$REPO_ROOT/.venv/bin/python"

rm -f "$E2E_DB"
"$PY" manage.py migrate --noinput >/dev/null
"$PY" manage.py seed_e2e

exec "$PY" manage.py runserver "localhost:$PORT" --noreload
