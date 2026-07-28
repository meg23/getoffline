#!/bin/sh
set -eu

python -m django migrate --run-syncdb
python -m django sync_model_schema

PYTHONPATH=/app/src exec gunicorn frontend.wsgi:application \
  --bind "${GETOFFLINE_API_GUNICORN_BIND:-0.0.0.0:8000}" \
  --workers "${GETOFFLINE_API_GUNICORN_WORKERS:-3}" \
  --timeout "${GETOFFLINE_GUNICORN_TIMEOUT:-300}"

