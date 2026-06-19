FROM python:3.14-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DJANGO_SETTINGS_MODULE=app.settings \
    DENO_INSTALL=/usr/local

RUN apk add --no-cache ca-certificates ffmpeg \
    && apk add --no-cache --virtual .deno-fetch curl unzip \
    && curl -fsSL https://deno.land/install.sh | sh \
    && apk del .deno-fetch

WORKDIR /app
COPY deploy/requirements/worker-download.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt

COPY src ./src

CMD ["python", "-m", "workers"]
