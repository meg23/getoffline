FROM python:3.12-alpine AS wheels

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY deploy/requirements/worker-transcripts.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt

FROM python:3.12-alpine AS model-cache

ARG WHISPER_MODEL=base
ENV WHISPER_MODEL=${WHISPER_MODEL} \
    GETOFFLINE_MODEL_CACHE_DIR=/app/model-cache \
    HF_HOME=/app/model-cache \
    HUGGINGFACE_HUB_CACHE=/app/model-cache/hub \
    XDG_CACHE_HOME=/app/model-cache/xdg

COPY --from=wheels /wheels /wheels
COPY deploy/requirements/worker-transcripts.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && /opt/venv/bin/python - <<'PY'
import os
from faster_whisper.utils import download_model
model = os.environ.get("WHISPER_MODEL", "base")
download_model(model, output_dir=os.environ["GETOFFLINE_MODEL_CACHE_DIR"])
PY

FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DJANGO_SETTINGS_MODULE=app.settings \
    GETOFFLINE_MODEL_CACHE_DIR=/app/model-cache \
    HF_HOME=/app/model-cache \
    HUGGINGFACE_HUB_CACHE=/app/model-cache/hub \
    XDG_CACHE_HOME=/app/model-cache/xdg \
    PATH=/opt/venv/bin:$PATH

RUN apk add --no-cache ca-certificates ffmpeg libgomp \
    && python -m venv /opt/venv
WORKDIR /app
COPY --from=wheels /wheels /wheels
COPY deploy/requirements/worker-transcripts.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && rm -rf /wheels /tmp/requirements.txt /root/.cache /opt/venv/share
COPY --from=model-cache /app/model-cache /app/model-cache
COPY src ./src
RUN python -m compileall -q /app/src

CMD ["python", "-m", "workers"]
