# syntax=docker/dockerfile:1.4
FROM python:3.12-alpine AS wheels

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY deploy/requirements/worker-download.txt /tmp/requirements.txt
RUN apk add --no-cache build-base
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt

FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DJANGO_SETTINGS_MODULE=frontend.settings \
    PATH=/opt/venv/bin:/usr/local/bin:$PATH

RUN apk add --no-cache ca-certificates ffmpeg quickjs \
    && python -m venv /opt/venv
WORKDIR /app
COPY deploy/requirements/worker-download.txt /tmp/requirements.txt
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && rm -rf /tmp/requirements.txt /root/.cache /opt/venv/share

COPY src ./src
RUN python -m compileall -q /app/src

CMD ["python", "-m", "workers"]
