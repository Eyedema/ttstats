#!/usr/bin/env bash
# Boot the app for the Playwright suite against a throwaway SQLite file.
#
# Two things this deliberately guarantees:
#   1. The CSS is rebuilt first. The e2e suite exists to catch problems in the
#      compiled stylesheet (a purged class, a missing rule); running it against
#      a stale build would defeat the point.
#   2. DATABASE_URL is cleared, so an exported Supabase URL can never point the
#      suite at the clone of production.
#   3. Static files are served the way prod serves them (settings/e2e.py):
#      collectstatic through the hashing + compressing (Brotli and gzip)
#      manifest storage, served by WhiteNoise. The byte budgets and
#      `npm run perf` measure what a phone actually downloads, and a template
#      referencing a file missing from the manifest 500s here exactly as it
#      would in production.
#
# Env: E2E_DB / E2E_STATIC_ROOT let a second server (npm run perf, port 8126)
# run beside the suite without sharing its database or STATIC_ROOT.
set -euo pipefail

PORT="${1:-8125}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export DJANGO_SETTINGS_MODULE=ttstats.settings.e2e
unset DATABASE_URL || true

# Throwaway VAPID keypair, committed on purpose.
#
# Without keys the app reports push as "not configured on this server" and
# never renders the enable button, so every browser test would exercise the
# one state production is guaranteed NOT to be in. These make the e2e server
# look like a configured deployment. Nothing is ever sent: the tests do not
# subscribe to a real push service, and this key pair is public in the repo,
# so it must never be used anywhere real.
export VAPID_PUBLIC_KEY="BFGG9T9Q89g2G_O5lR-iaNElVj-dJH1umtVTCoiI0a97c4PqSfebOzRB7mE02K2xk7GioqPhcx1aur7lb5r6p5I"
export VAPID_PRIVATE_KEY="yvVjNtitD1wJSoyx7XSA3e7AiCJEKQQ5axvRBIP6ytI"
export VAPID_ADMIN_EMAIL="mailto:e2e@example.invalid"

E2E_DB="${E2E_DB:-$REPO_ROOT/ttstats/e2e.sqlite3}"
export TTSTATS_STATIC_ROOT="${E2E_STATIC_ROOT:-$REPO_ROOT/ttstats/staticfiles_e2e}"
export TTSTATS_SQLITE_NAME="$E2E_DB"

# `build`, not `build:css`: the vendored htmx/Alpine bundles are gitignored
# too. On a clean checkout (i.e. CI) skipping the vendor step means Alpine
# 404s, and the drawer specs would then pass for entirely the wrong reason --
# a drawer that fails closed because the framework never loaded is exactly the
# scenario the fail-closed specs deliberately create.
npm run build >/dev/null

cd "$REPO_ROOT/ttstats"

# Locally this is the project venv; in CI the interpreter is already on PATH
# with the requirements installed, and there is no .venv to find.
PY="${E2E_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
fi

# --clear: a stale hashed copy of a file that has since changed must not linger.
"$PY" manage.py collectstatic --noinput --clear >/dev/null

rm -f "$E2E_DB"
"$PY" manage.py migrate --noinput >/dev/null
"$PY" manage.py seed_e2e

# Prod's worker class (KAN-168): gthread, so a held /live/state/?wait=1
# request ties up a thread exactly as it does in prod, and the thread-local
# tenancy state is exercised under thread reuse. ONE worker, not prod's 3:
# the e2e cache is LocMemCache, per process, and with several workers the
# long-poll would only see another worker's op through its 2 s DB fallback
# (and SQLite would take writes from several processes). Static is served by
# WhiteNoise in the app, as in prod; gunicorn has no static handler.
exec "$PY" -m gunicorn ttstats.wsgi:application \
  --bind "localhost:$PORT" \
  --worker-class gthread \
  --workers 1 \
  --threads "${E2E_GUNICORN_THREADS:-8}" \
  --timeout 60 \
  --error-logfile -
