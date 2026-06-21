FROM python:3.14-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DJANGO_SETTINGS_MODULE=app.settings \
    GETOFFLINE_GUNICORN_BIND=127.0.0.1:8000

WORKDIR /app
COPY deploy/requirements/frontend.txt /tmp/requirements.txt
RUN apk add --no-cache nginx \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && mkdir -p /run/nginx /var/lib/nginx/tmp/client_body /app/staticfiles

COPY deploy/nginx/default.conf /etc/nginx/http.d/default.conf
COPY deploy/docker/frontend-entrypoint.sh /usr/local/bin/frontend-entrypoint.sh
COPY src ./src
RUN python -m django collectstatic --noinput \
    && chmod +x /usr/local/bin/frontend-entrypoint.sh

EXPOSE 80
CMD ["frontend-entrypoint.sh"]
