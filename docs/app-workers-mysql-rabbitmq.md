# Django app + split RabbitMQ workers + shared Django ORM models

This split deployment uses three simple Python packages:

- `src/app`: the Django frontend. It renders pages, reads from MySQL, and queues jobs.
- `src/models`: shared Django ORM models and tiny job helpers used by both app and workers.
- `src/workers`: queue-specific Python workers that consume RabbitMQ messages and update MySQL.
- `ScheduledJob` rows in MySQL define recurring work; the scheduler process enqueues due jobs.

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
- `ScheduledJob`: database-configured recurring jobs with frequency, payload, next run, and idempotency template.

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
- `retention_cleanup` for automatic old-content deletion

## Running workers

Episode discovery and downloads intentionally run as separate single-concurrency
workers so the app checks sources and downloads from YouTube/media hosts one at
a time:

```bash
make run-worker-updates
make run-worker-downloader
```

Transcript and summary work can be split and run concurrently by starting
multiple worker processes for the same queue, or by setting `PREFETCH` for each
process:

```bash
PREFETCH=1 make run-worker-ffmpeg
PREFETCH=4 make run-worker-transcripts
PREFETCH=4 make run-worker-summaries
```

The FFmpeg worker is required for new downloads to move past the downloaded
state because the downloader now publishes `transcode_media` jobs before
transcript generation. When checking logs after a download, expect to see the
downloader publish `job_type=transcode_media`, then a separate `worker-ffmpeg`
container or `make run-worker-ffmpeg` process log `Worker starting
worker_type=ffmpeg` and `FFmpeg conversion starting`.

The normal media workflow is: updates discover items, downloads fetch one item,
the downloader skips FFmpeg when the file already matches the profile's target
format or queues `transcode_media` when conversion is needed, transcripts run on
the final media file, summaries run after transcripts, and the summary stage
removes any pre-transcode original media once downstream work has completed.
When conversion is needed, the downloader defers creating the library `Download`
row until the FFmpeg worker has produced the final MP4/MP3 output, so partially
processed video files do not appear in the database-backed library.
The default video conversion target is H.264/AAC in MP4 with an ultrafast x264
preset because it is much faster than HEVC and broadly compatible with Jellyfin
clients.

By default workers consume only messages that are already in RabbitMQ or are
published while the worker is running. If RabbitMQ was reset and you need to
recover queued database rows, restart the relevant worker once with
`GETOFFLINE_REQUEUE_EXISTING_JOBS=1`. Leave this off for normal operation so a
large backlog of older queued conversions does not run ahead of newly downloaded
media.

Recurring work is created by the scheduler from `scheduled_jobs` rows. Start it with:

```bash
make run-scheduler
```

Use `python -m django run_scheduler --install-defaults` to insert the default update, summary, sync, and retention schedules. Edit the `scheduled_jobs` table to change `enabled`, `interval_seconds`, `payload`, or `next_run_at`.

Sync work remains isolated on its own queue:

```bash
make run-worker-sync
```

## Queue mapping

- `getoffline.jobs.updates`: `update_downloads`, `check_for_episodes`
- `getoffline.jobs.downloads`: `download_single`, `download_episode`
- `getoffline.jobs.ffmpeg`: `transcode_media`
- `getoffline.jobs.transcripts`: `generate_transcript`
- `getoffline.jobs.summaries`: `summarize_missing`, `generate_summary`
- `getoffline.jobs.sync_media`: `sync_media`
- `getoffline.jobs.cleanup`: `retention_cleanup`

Each RabbitMQ message contains only the job id, job type, profile id, and attempt.
The job payload and status live in MySQL.

## Docker Compose with persistent MySQL

The repository includes `docker-compose.yml` for running the frontend with bundled nginx, RabbitMQ, workers, and a persistent MySQL database. It builds separate Alpine images so the frontend/migration image installs only web/database/queue dependencies plus nginx, while the worker image carries the heavier media-processing dependencies only where they are needed:

- `frontend` and `migrate` build from `deploy/docker/frontend.Dockerfile`, an Alpine image with Django, Gunicorn, and nginx for web/database/queue dependencies.
- updates/downloader workers build from `deploy/docker/worker-download.Dockerfile`, an Alpine image with yt-dlp/feed parsing plus ffmpeg and deno only where download/discovery work needs them.
- transcript, summary, and sync workers build from `deploy/docker/worker-base.Dockerfile`, a smaller Alpine worker image with only Django/database/queue dependencies until heavier processing packages are actually needed.
- `frontend` publishes host port `8080`, serves static files with bundled nginx, and proxies dynamic requests to the Django app running under Gunicorn WSGI in the same container.
  It preserves the original `Host` header, including the port, so Django CSRF origin checks match browser requests.
- `mysql` runs MySQL 8.4, initializes the app database/user from `GETOFFLINE_DB_*`, and persists database files in the `mysql-data` named volume.
- `rabbitmq` runs the broker and exposes the management UI on host port `15672`; broker state persists in `rabbitmq-data`.
- `worker-updates` discovers new episodes and publishes download jobs.
- `worker-downloader` consumes download jobs one at a time.
- `worker-ffmpeg` consumes conversion jobs and defaults to three Compose
  replicas so up to three conversions can run in parallel. Each replica uses a
  prefetch of one by default so one long encode does not reserve extra jobs.
- `worker-transcripts`, `worker-summaries`, `worker-sync`, and `worker-cleanup` run the parallel/background processing queues.
- `scheduler` polls the database for due `scheduled_jobs` rows and publishes durable RabbitMQ jobs.
- `migrate` is a one-shot service that runs automatically before the frontend and workers start, applying Django schema updates to the configured MySQL database.

Example startup:

```bash
export GETOFFLINE_DJANGO_SECRET_KEY='replace-me'
export GETOFFLINE_DB_NAME='getoffline'
export GETOFFLINE_DB_USER='getoffline'
export GETOFFLINE_DB_PASSWORD='replace-me'
export GETOFFLINE_DB_ROOT_PASSWORD='replace-root-password'
export GETOFFLINE_DOWNLOADS_DIR='/srv/getoffline/downloads'
# Optional when exposing a different hostname or HTTPS URL:
export GETOFFLINE_CSRF_TRUSTED_ORIGINS='http://localhost:8080'

docker compose up --build -d
```

On startup, `frontend` and every worker wait for the one-shot `migrate` service to finish successfully, so tables such as `downloads` are created before the web UI serves requests. The default Compose database host is the `mysql` service. To use an external MySQL server instead, set `GETOFFLINE_DB_HOST` and keep the service credentials aligned with that server. Scale only the workers that are safe to run in parallel. Keep `worker-downloader` at one replica so YouTube downloads remain serialized, but transcript and summary workers can be scaled independently:

```bash
docker compose up -d --scale worker-ffmpeg=3 --scale worker-transcripts=4 --scale worker-summaries=4
```
