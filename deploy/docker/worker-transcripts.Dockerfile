FROM python:3.12-alpine AS wheels

ARG FASTER_WHISPER_VERSION=1.1.1
ARG CTRANSLATE2_VERSION=4.6.0
ARG TARGETPLATFORM
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY deploy/requirements/worker-transcripts.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt \
    && python -m pip download --no-cache-dir --no-deps --dest /wheels faster-whisper==${FASTER_WHISPER_VERSION} \
    && case "${TARGETPLATFORM:-linux/amd64}" in \
        linux/amd64) CTRANSLATE2_PLATFORM=manylinux2014_x86_64 ;; \
        linux/arm64) CTRANSLATE2_PLATFORM=manylinux2014_aarch64 ;; \
        *) echo "Unsupported Alpine transcript platform for prebuilt ctranslate2: ${TARGETPLATFORM}" >&2; exit 1 ;; \
       esac \
    && python -m pip download --no-cache-dir --no-deps --only-binary=:all: --dest /wheels \
        --platform ${CTRANSLATE2_PLATFORM} --implementation cp --python-version 312 --abi cp312 \
        ctranslate2==${CTRANSLATE2_VERSION}

FROM python:3.12-alpine AS model-cache

ARG WHISPER_MODEL=base
ENV WHISPER_MODEL=${WHISPER_MODEL} \
    GETOFFLINE_MODEL_CACHE_DIR=/app/model-cache \
    HF_HOME=/app/model-cache \
    HUGGINGFACE_HUB_CACHE=/app/model-cache/hub \
    XDG_CACHE_HOME=/app/model-cache/xdg

COPY --from=wheels /wheels /wheels
COPY deploy/requirements/worker-transcripts.txt /tmp/requirements.txt
RUN apk add --no-cache libstdc++ gcompat \
    && python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && /opt/venv/bin/python -m pip install --no-cache-dir --no-deps /wheels/faster_whisper-*.whl \
    && /opt/venv/bin/python - <<'PY'
import site
import zipfile
from pathlib import Path
site_packages = Path(site.getsitepackages()[0])
wheels = sorted(Path('/wheels').glob('ctranslate2-*.whl'))
if not wheels:
    raise SystemExit('missing ctranslate2 wheel')
with zipfile.ZipFile(wheels[0]) as wheel:
    wheel.extractall(site_packages)
PY
RUN /opt/venv/bin/python - <<'PY'
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

RUN apk add --no-cache ca-certificates ffmpeg libgomp libstdc++ gcompat \
    && python -m venv /opt/venv
WORKDIR /app
COPY --from=wheels /wheels /wheels
COPY deploy/requirements/worker-transcripts.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && python -m pip install --no-cache-dir --no-deps /wheels/faster_whisper-*.whl \
    && python - <<'PY'
import site
import zipfile
from pathlib import Path
site_packages = Path(site.getsitepackages()[0])
wheels = sorted(Path('/wheels').glob('ctranslate2-*.whl'))
if not wheels:
    raise SystemExit('missing ctranslate2 wheel')
with zipfile.ZipFile(wheels[0]) as wheel:
    wheel.extractall(site_packages)
PY
RUN rm -rf /wheels /tmp/requirements.txt /root/.cache /opt/venv/share
COPY --from=model-cache /app/model-cache /app/model-cache
COPY src ./src
RUN python -m compileall -q /app/src

CMD ["python", "-m", "workers"]
