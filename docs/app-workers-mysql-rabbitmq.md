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
make migrate-db
```

This target runs Django migrations and then `sync_model_schema`, which adds missing shared-model columns to existing GetOffline tables. If MySQL reports an error such as `Unknown column downloads.source_url`, run this target before starting the app again.

## Running the app

```bash
PYTHONPATH=src python -m app
```

Run the app in Django debug mode with:

```bash
make run-app-debug
```

The debug target starts `python -m app` with `GETOFFLINE_DJANGO_DEBUG=1`.

The frontend now covers the core legacy `webapp.py` screens and actions in Django:

- library listing with played/favorite filters
- player page with media/subtitle endpoints and playback position saves
- settings/config/source management
- job history
- download actions for played/unplayed, favorite/unfavorite, and delete-file

It can queue:

- `update_downloads` / `check_for_episodes` to discover new work
- `download_single` / `download_episode` to download one item at a time
- `generate_transcript` for parallel transcript generation
- `summarize_missing` / `generate_summary` for parallel summary generation
- `sync_media`

## Running workers

Episode discovery and downloads intentionally run as separate single-concurrency
workers so the app checks sources and downloads from YouTube/media hosts one at
a time:

```bash
make run-worker-episode-checker
make run-worker-downloader
```

Transcript and summary work can be split and run concurrently by starting
multiple worker processes for the same queue, or by setting `PREFETCH` for each
process:

```bash
PREFETCH=4 make run-worker-transcripts
PREFETCH=4 make run-worker-summaries
```

Sync work remains isolated on its own queue:

```bash
make run-worker-sync
```

## Queue mapping

- `getoffline.episode_checks`: `update_downloads`, `check_for_episodes`
- `getoffline.downloads`: `download_single`, `download_episode`
- `getoffline.transcripts`: `generate_transcript`
- `getoffline.summaries`: `summarize_missing`, `generate_summary`
- `getoffline.sync_media`: `sync_media`

Each RabbitMQ message contains only the job id, job type, profile id, and attempt.
The job payload and status live in MySQL.
