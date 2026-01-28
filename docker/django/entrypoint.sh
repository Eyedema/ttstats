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

echo "Starting Gunicorn..."
exec gunicorn ttstats.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers 3 \
  --forwarded-allow-ips '*' \
  --timeout 60 \
  --access-logfile - \
  --error-logfile -
