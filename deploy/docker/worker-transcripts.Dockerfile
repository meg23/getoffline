FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DJANGO_SETTINGS_MODULE=app.settings

RUN apk add --no-cache ca-certificates ffmpeg libgomp libstdc++

WORKDIR /app
COPY deploy/requirements/worker-transcripts.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt

COPY src ./src

CMD ["python", "-m", "workers"]
