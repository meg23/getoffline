# syntax=docker/dockerfile:1.4
FROM python:3.12-slim AS wheels

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY deploy/requirements/worker-summaries.txt /tmp/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake ninja-build \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --upgrade pip \
    && CMAKE_ARGS="-DGGML_NATIVE=OFF" FORCE_CMAKE=1 python -m pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt

FROM python:3.12-slim AS model-cache

ARG SUMMARY_LLAMA_CPP_REPO_ID=Qwen/Qwen2.5-0.5B-Instruct-GGUF
ARG SUMMARY_LLAMA_CPP_FILENAME=qwen2.5-0.5b-instruct-q4_k_m.gguf
ENV GETOFFLINE_MODEL_CACHE_DIR=/app/model-cache \
    HF_HOME=/app/model-cache \
    HUGGINGFACE_HUB_CACHE=/app/model-cache/hub \
    XDG_CACHE_HOME=/app/model-cache/xdg \
    GETOFFLINE_SUMMARY_LLAMA_CPP_REPO_ID=${SUMMARY_LLAMA_CPP_REPO_ID} \
    GETOFFLINE_SUMMARY_LLAMA_CPP_FILENAME=${SUMMARY_LLAMA_CPP_FILENAME}

COPY deploy/requirements/worker-summaries.txt /tmp/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    /opt/venv/bin/python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt
RUN /opt/venv/bin/python - <<'PY'
import os
from huggingface_hub import hf_hub_download
hf_hub_download(
    repo_id=os.environ["GETOFFLINE_SUMMARY_LLAMA_CPP_REPO_ID"],
    filename=os.environ["GETOFFLINE_SUMMARY_LLAMA_CPP_FILENAME"],
)
PY

FROM python:3.12-slim

ARG SUMMARY_LLAMA_CPP_REPO_ID=Qwen/Qwen2.5-0.5B-Instruct-GGUF
ARG SUMMARY_LLAMA_CPP_FILENAME=qwen2.5-0.5b-instruct-q4_k_m.gguf
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DJANGO_SETTINGS_MODULE=app.settings \
    GETOFFLINE_MODEL_CACHE_DIR=/app/model-cache \
    HF_HOME=/app/model-cache \
    HUGGINGFACE_HUB_CACHE=/app/model-cache/hub \
    XDG_CACHE_HOME=/app/model-cache/xdg \
    GETOFFLINE_SUMMARY_BACKEND=internal \
    GETOFFLINE_SUMMARY_LLAMA_CPP_REPO_ID=${SUMMARY_LLAMA_CPP_REPO_ID} \
    GETOFFLINE_SUMMARY_LLAMA_CPP_FILENAME=${SUMMARY_LLAMA_CPP_FILENAME} \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv
WORKDIR /app
COPY deploy/requirements/worker-summaries.txt /tmp/requirements.txt
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && rm -rf /tmp/requirements.txt /root/.cache /opt/venv/share
COPY --from=model-cache /app/model-cache /app/model-cache
COPY src ./src
RUN python -m compileall -q /app/src

CMD ["python", "-m", "workers"]
