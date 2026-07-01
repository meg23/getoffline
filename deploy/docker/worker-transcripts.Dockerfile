# syntax=docker/dockerfile:1.4
FROM python:3.12-slim AS wheels

ARG FASTER_WHISPER_VERSION=1.1.0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY deploy/requirements/worker-transcripts.txt /tmp/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --upgrade pip \
    && for attempt in 1 2 3; do \
        rm -rf /wheels /tmp/wheel-verify \
        && mkdir -p /wheels \
        && python -m pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt \
        && python -m pip wheel --no-cache-dir --wheel-dir /wheels faster-whisper==${FASTER_WHISPER_VERSION} \
        && python -m pip install --no-cache-dir --target /tmp/wheel-verify --no-index --find-links=/wheels -r /tmp/requirements.txt \
        && python -m pip install --no-cache-dir --target /tmp/wheel-verify --no-index --find-links=/wheels faster-whisper==${FASTER_WHISPER_VERSION} \
        && break; \
        if [ "$attempt" = "3" ]; then exit 1; fi; \
        sleep 5; \
    done \
    && rm -rf /tmp/wheel-verify

FROM python:3.12-slim AS model-cache

ARG WHISPER_MODEL=base
ARG FASTER_WHISPER_VERSION=1.1.0
ENV WHISPER_MODEL=${WHISPER_MODEL} \
    GETOFFLINE_MODEL_CACHE_DIR=/app/model-cache \
    HF_HOME=/app/model-cache \
    HUGGINGFACE_HUB_CACHE=/app/model-cache/hub \
    XDG_CACHE_HOME=/app/model-cache/xdg

COPY deploy/requirements/worker-transcripts.txt /tmp/requirements.txt
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && /opt/venv/bin/python -m pip install --no-cache-dir --no-index --find-links=/wheels faster-whisper==${FASTER_WHISPER_VERSION}
RUN /opt/venv/bin/python - <<'PY'
import os
from faster_whisper.utils import download_model
model = os.environ.get("WHISPER_MODEL", "base")
download_model(model, output_dir=os.environ["GETOFFLINE_MODEL_CACHE_DIR"])
PY

FROM python:3.12-slim

ARG FASTER_WHISPER_VERSION=1.1.0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DJANGO_SETTINGS_MODULE=app.settings \
    GETOFFLINE_MODEL_CACHE_DIR=/app/model-cache \
    HF_HOME=/app/model-cache \
    HUGGINGFACE_HUB_CACHE=/app/model-cache/hub \
    XDG_CACHE_HOME=/app/model-cache/xdg \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv
WORKDIR /app
COPY deploy/requirements/worker-transcripts.txt /tmp/requirements.txt
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && python -m pip install --no-cache-dir --no-index --find-links=/wheels faster-whisper==${FASTER_WHISPER_VERSION}
RUN rm -rf /tmp/requirements.txt /root/.cache /opt/venv/share
COPY --from=model-cache /app/model-cache /app/model-cache
COPY src ./src
RUN python -m compileall -q /app/src

CMD ["python", "-m", "workers"]
