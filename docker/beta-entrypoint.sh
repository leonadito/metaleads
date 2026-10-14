#!/usr/bin/env bash
# Beta container: web (gunicorn) + task worker (django-tasks db_worker) in the
# same container, as planned (no extra container, no Redis). If either dies
# the container exits and the restart policy brings both back.
set -euo pipefail

python manage.py collectstatic --noinput -v 0
python manage.py migrate --noinput
python manage.py crm_schedule

gunicorn leadmanager.wsgi:application --bind 0.0.0.0:8000 --preload \
    --worker-class gthread --workers 1 --threads 4 --timeout 120 \
    --max-requests 1000 --max-requests-jitter 100 --access-logfile - &

python manage.py db_worker --interval 2 &

trap 'kill -TERM $(jobs -p) 2>/dev/null; wait' TERM INT
wait -n
status=$?
kill -TERM $(jobs -p) 2>/dev/null || true
wait || true
exit "$status"
