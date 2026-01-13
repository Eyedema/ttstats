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
python manage.py makemigrations --noinput

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
