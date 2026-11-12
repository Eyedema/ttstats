#!/bin/sh
set -e

echo "Waiting for postgres..."
while ! nc -z $DB_HOST ${DB_PORT:-5432}; do
  sleep 0.5
done
echo "PostgreSQL started"

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Making migrations..."
python manage.py makemigrations --check --dry-run || {
    echo "⚠️  WARNING: Uncommitted migrations detected!"
    echo "Migrations should be created in development and committed to git."
    exit 1
}

echo "Running migrations..."
python manage.py migrate --noinput

# Threaded workers (KAN-168): a held /live/state/?wait=1 request takes one
# thread, not one of three processes. Thread-locals stay per request: one
# request per thread at a time, cleared on the way in and out
# (CurrentUserMiddleware). Each thread may keep one persistent Postgres
# connection (CONN_MAX_AGE), so workers x threads is the ceiling -- see the
# comment in settings/prod.py before raising either. --timeout is the
# worker heartbeat and must stay above LIVE_LONGPOLL_WAIT (20 s).
# --worker-connections defaults to the thread count: gthread otherwise accepts
# up to 1000 connections and queues them behind busy threads, while equal to
# it a saturated worker stops accepting and the others take the connection.
# The price is no keep-alive (gunicorn logs "No keepalived connections can be
# handled" at boot; expected), cheap on the docker network to Traefik. Held
# long-polls are capped below the thread count by LIVE_LONGPOLL_MAX_HELD.
echo "Starting Gunicorn..."
exec gunicorn ttstats.wsgi:application \
  --bind 0.0.0.0:8000 \
  --worker-class gthread \
  --workers "${GUNICORN_WORKERS:-3}" \
  --threads "${GUNICORN_THREADS:-8}" \
  --worker-connections "${GUNICORN_WORKER_CONNECTIONS:-${GUNICORN_THREADS:-8}}" \
  --forwarded-allow-ips '*' \
  --timeout 60 \
  --access-logfile - \
  --error-logfile -
