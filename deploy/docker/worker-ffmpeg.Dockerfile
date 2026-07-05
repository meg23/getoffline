# syntax=docker/dockerfile:1.4
FROM python:3.12-alpine AS wheels

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY deploy/requirements/worker-base.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt

FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DJANGO_SETTINGS_MODULE=app.settings \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app
RUN apk add --no-cache ca-certificates ffmpeg \
    && python -m venv /opt/venv
COPY deploy/requirements/worker-base.txt /tmp/requirements.txt
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && rm -rf /tmp/requirements.txt /root/.cache /opt/venv/share

COPY src ./src
RUN python -m compileall -q /app/src

CMD ["python", "-m", "workers"]
