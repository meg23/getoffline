# syntax=docker/dockerfile:1.4
FROM python:3.14-alpine AS wheels

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY deploy/requirements/frontend.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt

FROM python:3.14-alpine AS static

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DJANGO_SETTINGS_MODULE=frontend.settings \
    GETOFFLINE_STATIC_MANIFEST=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app
RUN python -m venv /opt/venv
COPY deploy/requirements/frontend.txt /tmp/requirements.txt
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && rm -rf /tmp/requirements.txt /root/.cache /opt/venv/share
COPY src ./src
RUN python -m django collectstatic --noinput

FROM python:3.14-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DJANGO_SETTINGS_MODULE=frontend.settings \
    GETOFFLINE_STATIC_MANIFEST=1 \
    GETOFFLINE_GUNICORN_BIND=127.0.0.1:8000 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app
RUN apk add --no-cache curl nginx \
    && python -m venv /opt/venv \
    && mkdir -p /run/nginx /var/lib/nginx/tmp/client_body /app/staticfiles
COPY deploy/requirements/frontend.txt /tmp/requirements.txt
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && rm -rf /tmp/requirements.txt /root/.cache /opt/venv/share

COPY deploy/nginx/default.conf /etc/nginx/http.d/default.conf
COPY deploy/docker/frontend-entrypoint.sh /usr/local/bin/frontend-entrypoint.sh
COPY deploy/docker/api-entrypoint.sh /usr/local/bin/api-entrypoint.sh
COPY src ./src
COPY --from=static /app/staticfiles ./staticfiles
RUN GETOFFLINE_DJANGO_ROLE=api python -c "import frontend; import api" \
    && python -m compileall -q /app/src \
    && chmod +x /usr/local/bin/frontend-entrypoint.sh /usr/local/bin/api-entrypoint.sh

EXPOSE 80
CMD ["frontend-entrypoint.sh"]
