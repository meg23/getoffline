#!/bin/sh
set -eu

python -m django collectstatic --noinput

gunicorn app.wsgi:application \
  --bind "${GETOFFLINE_GUNICORN_BIND:-127.0.0.1:8000}" \
  --workers "${GETOFFLINE_GUNICORN_WORKERS:-3}" \
  --timeout "${GETOFFLINE_GUNICORN_TIMEOUT:-300}" &

gunicorn_pid="$!"

term_handler() {
  kill -TERM "$gunicorn_pid" 2>/dev/null || true
  nginx -s quit 2>/dev/null || true
  wait "$gunicorn_pid" 2>/dev/null || true
}
trap term_handler INT TERM

nginx -g 'daemon off;' &
nginx_pid="$!"

wait -n "$gunicorn_pid" "$nginx_pid"
status="$?"

kill -TERM "$gunicorn_pid" "$nginx_pid" 2>/dev/null || true
wait "$gunicorn_pid" "$nginx_pid" 2>/dev/null || true
exit "$status"
