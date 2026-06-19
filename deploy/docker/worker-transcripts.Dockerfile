# faster-whisper depends on ctranslate2, which publishes glibc/manylinux wheels but
# not Alpine/musl wheels. Keep only the transcript image on slim Debian so the
# heavy transcription dependency is isolated from the rest of the worker fleet.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DJANGO_SETTINGS_MODULE=app.settings \
    GETOFFLINE_MODEL_CACHE_DIR=/app/model-cache \
    HF_HOME=/app/model-cache \
    HUGGINGFACE_HUB_CACHE=/app/model-cache/hub \
    XDG_CACHE_HOME=/app/model-cache/xdg

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY deploy/requirements/worker-transcripts.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt

COPY src ./src

CMD ["python", "-m", "workers"]
