# Django app + split RabbitMQ workers + shared Django ORM models

This split deployment uses three simple Python packages:

- `src/app`: the Django frontend. It renders pages, reads from MySQL, and queues jobs.
- `src/models`: shared Django ORM models and tiny job helpers used by both app and workers.
- `src/workers`: queue-specific Python workers that consume RabbitMQ messages and update MySQL.

The browser never connects to MySQL or RabbitMQ directly. The Django app and each
worker process connect to the same MySQL database using Django's ORM.

## MySQL settings

The split Django app uses `PyMySQL` as Django's MySQL driver shim, so it does not require the native `mysqlclient` package or local `pkg-config`/MariaDB client headers.

Both the app and workers use `app.settings`, so they share these variables:

```bash
export GETOFFLINE_DB_NAME=getoffline
export GETOFFLINE_DB_USER=getoffline
export GETOFFLINE_DB_PASSWORD=password
export GETOFFLINE_DB_HOST=127.0.0.1
export GETOFFLINE_DB_PORT=3306
```

## RabbitMQ settings

```bash
export GETOFFLINE_RABBITMQ_URL='amqp://guest:guest@127.0.0.1:5672/%2F'
export GETOFFLINE_RABBITMQ_EXCHANGE=getoffline
```

## Database tables

The shared models live in `src/models/models.py` and define:

- `Download`: the frontend library rows workers update.
- `Job`: the durable job state (`queued`, `running`, `succeeded`, `failed`).

Create tables with Django's normal migration/sync workflow for this split app:

```bash
PYTHONPATH=src DJANGO_SETTINGS_MODULE=app.settings python -m django migrate --run-syncdb
```

## Running the app

```bash
PYTHONPATH=src python -m app
```

The frontend lists recent downloads and jobs and can queue:

- `update_downloads`
- `download_single`
- `sync_media`
- `summarize_missing`

## Running workers

Downloads intentionally run as a single worker so the app does not download too
quickly from YouTube:

```bash
PYTHONPATH=src python -m workers downloads
```

Other work can be split and run concurrently by starting multiple worker
processes for the same queue:

```bash
PYTHONPATH=src python -m workers sync
PYTHONPATH=src python -m workers sync
PYTHONPATH=src python -m workers summaries
PYTHONPATH=src python -m workers summaries
```

## Queue mapping

- `getoffline.downloads`: `update_downloads`, `download_single`
- `getoffline.sync_media`: `sync_media`
- `getoffline.summarize_missing`: `summarize_missing`

Each RabbitMQ message contains only the job id, job type, profile id, and attempt.
The job payload and status live in MySQL.
