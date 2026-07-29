# GetOffline engineering guide

This file is the repository-level operating context for coding agents and
contributors. Read it before changing application code, worker behavior,
Docker configuration, or tests.

## What the application does

GetOffline is a self-hosted media library for downloading YouTube videos and
podcast episodes for offline playback. It provides:

- A browser UI for library browsing, search, played/unplayed state, favorites,
  playback progress, settings, source management, job history, and manual
  actions.
- YouTube channel/playlist/video discovery and podcast RSS discovery.
- Durable background jobs for source updates, media downloads, FFmpeg
  conversion, transcript generation, and retention cleanup.
- Audio/video playback and subtitle streaming from the configured downloads
  directory.
- Whisper-based transcripts and optional explicit-content filtering.
- Per-profile libraries and settings, including source configuration and media
  output preferences.
- A typed Python SDK and a terminal/curses client for the API.

The application is designed to run as a Docker Compose stack. The browser
talks to the frontend service; the frontend proxies dynamic requests to the
API. The API and workers own database access. RabbitMQ carries work
notifications, while MySQL is the source of truth for job state and media
metadata.

## Repository layout

| Path | Responsibility |
| --- | --- |
| `src/frontend/` | Django frontend, templates, static assets, browser routes, and proxy behavior. The normal frontend role is stateless and does not require database-backed authentication. |
| `src/api/` | JSON API routes, authentication/session endpoints, playback/streaming endpoints, and service-layer actions. The API role owns database-backed authentication and application state. |
| `src/models/` | Shared Django app, ORM models, job helpers, scheduler command, and domain enums. Both the API and workers use these modules. |
| `src/workers/` | RabbitMQ consumers, job handlers, source discovery, yt-dlp integration, FFmpeg orchestration, transcription, filtering, cleanup, and distributed CPU-slot coordination. |
| `src/shared/` | Shared schemas and small cross-service data definitions. |
| `src/packages/getoffline_sdk/` | Typed Python SDK with HTTP and in-process Django transports. |
| `src/cli/` | Optional curses client that uses the SDK to browse and play the library. |
| `crons/` | Host-cron-compatible media synchronization utility. It copies validated media atomically to another directory. |
| `deploy/docker/` | Runtime-specific Dockerfiles and API/frontend entrypoints. |
| `deploy/nginx/` | Bundled nginx configuration for static files, frontend proxying, and API proxying. |
| `docker-compose.yml` | Local/production-style multi-service stack with MySQL, RabbitMQ, web services, workers, and scheduler. |
| `tests/` | Unit tests, service/API tests, helper tests, CLI tests, and Docker Compose integration tests. |
| `docs/` | Deployment and architecture notes. |
| `Makefile` | Canonical local development, quality, test, migration, and process commands. |

**Generated files and directories** (never commit these; ensure `.gitignore` coverage):
- Python bytecode: `__pycache__/`, `*.pyc`, `*.pyo`, `*.pyd` (version-specific, regenerated at runtime)
- Build artifacts: `target/`, `build/`, `dist/`, `*.egg-info/`
- Test/coverage output: `.coverage`, `htmlcov/`, `.pytest_cache/`
- Downloaded assets: `downloads/` directory and model cache
- Environment/local overrides: `.env`, `.env.local`

Do not modify or delete unrelated user-created files in a dirty worktree.

## Runtime architecture

### Django roles and request flow

`src/frontend/settings.py` changes behavior based on
`GETOFFLINE_DJANGO_ROLE`:

- The default `frontend` role is a stateless Django frontend. It renders the
  browser pages and proxies state-changing/data requests to the API.
- The `api` role enables MySQL, Django auth/session apps, the shared models,
  and API routes.
- The `worker` role also enables the shared Django models and MySQL access so
  workers can claim and update jobs.
- Tests set `GETOFFLINE_TEST_IN_MEMORY_DB=1` and use SQLite in memory.

In Compose, the `frontend` container runs nginx plus Gunicorn. Nginx serves
collected static files, proxies browser routes to the local frontend Gunicorn,
and proxies `/api/` to the API container. It preserves the browser `Host`,
forwarded host, scheme, and client IP headers because Django CSRF and host
validation depend on them. The `api` container runs Gunicorn directly and uses
`deploy/docker/api-entrypoint.sh` to run migrations, `sync_model_schema`, and
then Gunicorn for `frontend.wsgi:application`.

The browser should normally use the frontend origin, not connect directly to
MySQL, RabbitMQ, or the API's published development port.

### Persistence and models

The shared models are in `src/models/models.py`:

- `SourceConfig` stores YouTube/podcast sources and per-source discovery flags.
- `Download` stores library metadata, media paths, status, playback state, and
  subtitle paths.
- `Job` stores durable queued/running/succeeded/failed work and its payload.
- `ScheduledJob` stores recurring work, intervals, payloads, and idempotency
  templates.
- `TranscriptSegment` stores searchable transcript segments.
- `ProfileConfigValue`, `ProfileDownloadSettings`, and `DownloadSettings` store
  profile/global configuration and cookie data.
- `AppConfigValue` stores global configuration and distributed lock records.
- `CpuSlotRequest` stores leases used to coordinate expensive FFmpeg and
  transcription work across worker processes.

There are no ordinary per-change migration files in this repository. Database
setup intentionally uses Django's migration framework plus
`sync_model_schema`; update both the model and schema-sync behavior when
adding or changing persisted fields. Preserve existing tables and data.

Every database-backed operation must respect `profile_id` unless it is
explicitly global. Do not leak one profile's sources, downloads, settings,
transcripts, or jobs into another profile.

### RabbitMQ and job lifecycle

Queue names and routing rules live in `src/frontend/routing.py` and the enums
in `src/models/domain.py`:

- `getoffline.jobs.updates`: `check_for_episodes` and `update_downloads`.
- `getoffline.jobs.downloads.youtube`: YouTube and manual URL downloads.
- `getoffline.jobs.downloads.podcast`: podcast episode downloads.
- `getoffline.jobs.ffmpeg`: `transcode_media`.
- `getoffline.jobs.transcripts`: `generate_transcript`.
- `getoffline.jobs.cleanup`: `retention_cleanup`.

Published messages are intentionally small. The job ID, type, profile, and
attempt identify work; the authoritative payload and status remain in MySQL.
`frontend.queue.publish_job` selects the queue and applies priority settings.

`workers.runner` validates the worker type, consumes the matching queue,
atomically claims a queued `Job`, invokes the handler, marks it succeeded or
failed, and acknowledges or negatively acknowledges the RabbitMQ delivery.
Workers must be safe to retry: use idempotency keys, check existing output,
and avoid creating duplicate library rows or duplicate child jobs.

The update flow discovers source items and creates download jobs. Downloaders
use yt-dlp or feedparser, write media and metadata, and either keep an already
compatible file or enqueue the FFmpeg conversion stage. FFmpeg chooses the
profile's target format/codec and cleans up superseded source artifacts only
after the target is valid. Transcript workers use faster-whisper, split long
files into bounded chunks when configured, write subtitle/transcript data, and
can run explicit-content screening. Cleanup marks missing files and applies
retention rules while preserving favorites.

Expensive work is coordinated through the shared CPU-slot scheduler. Do not
remove lease/heartbeat behavior or introduce unbounded parallel FFmpeg or
Whisper work without changing the scheduler and tests together.

### Scheduler and cron behavior

`python -m django run_scheduler --loop --install-defaults` polls due
`ScheduledJob` rows, creates idempotent `Job` rows, and publishes them. The
scheduler is separate from queue consumers. Use `run_scheduler` and
`scheduled_jobs` configuration for recurring application work; do not add a
second ad hoc scheduler in a worker.

`crons/sync_media_downloads.py` is a separate filesystem sync utility. It
validates media with `ffprobe`, copies to a temporary destination, and uses an
atomic rename so consumers never see a partial file. Keep it safe for repeated
execution and preserve its dry-run, force, verbosity, ownership, and
`user[:group]`/`uid[:gid]` options.

## Configuration and local execution

### Recommended Docker workflow

The quickest complete development environment is:

```bash
export GETOFFLINE_DJANGO_SECRET_KEY='use-a-long-random-development-secret'
docker compose up --build -d
docker compose ps
docker compose logs -f api frontend worker-updates
```

The application is available at `http://127.0.0.1:8080`. Create a first user
from the API container:

```bash
docker compose exec api python -m django create_user alice --password 'change-this-password'
```

Downloaded media defaults to `./downloads`; set
`GETOFFLINE_DOWNLOADS_DIR=/absolute/path/to/media` to use another host
directory. MySQL and RabbitMQ state use the `mysql-data` and `rabbitmq-data`
named volumes. Stop the stack with `docker compose down`; do not use `down -v`
unless deleting database/broker state is intentional.

Compose services are:

- `frontend`: nginx/Gunicorn browser entrypoint.
- `api`: migrations, schema sync, API Gunicorn, database and RabbitMQ access.
- `mysql`: MySQL 8.4.
- `rabbitmq`: RabbitMQ broker.
- `worker-updates`: serialized source discovery/update jobs.
- `worker-downloader-youtube`: YouTube/manual URL downloads.
- `worker-downloader-podcast`: podcast downloads.
- `worker-ffmpeg`: media conversion.
- `worker-transcripts`: Whisper/transcript/filtering work.
- `worker-cleanup`: retention and cleanup work.
- `scheduler`: recurring-job publisher.

The main environment variables are:

- `GETOFFLINE_DJANGO_SECRET_KEY`: required by Compose; never use a shared
  production/default secret.
- `GETOFFLINE_DJANGO_ROLE`: `frontend`, `api`, or `worker`.
- `GETOFFLINE_DB_HOST`, `GETOFFLINE_DB_PORT`, `GETOFFLINE_DB_NAME`,
  `GETOFFLINE_DB_USER`, `GETOFFLINE_DB_PASSWORD`, and
  `GETOFFLINE_DB_ROOT_PASSWORD`.
- `GETOFFLINE_RABBITMQ_URL` and `GETOFFLINE_RABBITMQ_EXCHANGE`.
- `GETOFFLINE_DOWNLOADS_DIR` and profile `output_root` settings.
- `GETOFFLINE_DJANGO_ALLOWED_HOSTS`, `GETOFFLINE_DJANGO_STRICT_ALLOWED_HOSTS`,
  and `GETOFFLINE_CSRF_TRUSTED_ORIGINS`.
- `GETOFFLINE_STATIC_ROOT`, `GETOFFLINE_STATIC_MANIFEST`, and Gunicorn bind,
  worker, and timeout settings.
- `GETOFFLINE_CPU_SCHEDULER_SLOTS`, lease/heartbeat/poll settings, and
  `GETOFFLINE_WORKER_MAX_MESSAGES`.
- `GETOFFLINE_MODEL_CACHE_DIR`, `GETOFFLINE_TRANSCRIPTION_CHUNK_SECONDS`, and
  `GETOFFLINE_TRANSCRIPTION_CHUNK_THRESHOLD_SECONDS`.
- `GETOFFLINE_YTDLP_MODULE` for the deterministic integration-test double and
  `GETOFFLINE_REQUEUE_EXISTING_JOBS=1` for deliberate queued-job recovery.

Read `src/frontend/settings.py`, `src/workers/handlers.py`,
`src/workers/transcription.py`, and `docker-compose.yml` before adding a new
environment variable. Keep defaults safe for local use and document new
deployment-facing variables in `README.md` or the relevant architecture doc.

### Local Python workflow

The Makefile creates `.venv` and installs `src/requirements.txt` plus the CI
tools. Typical commands from the repository root are:

```bash
make venv
make run-app-debug
make run-worker-updates
make run-worker-downloader-youtube
PREFETCH=4 make run-worker-transcripts
make run-worker-cleanup
make run-scheduler
```

The local app command is useful for frontend development, but database-backed
features require the API/worker role and MySQL/RabbitMQ configuration. Docker
Compose is the preferred way to exercise the complete system.

The SDK can be built from `src/packages`:

```bash
cd src/packages
python -m pip wheel . -w dist
```

The CLI can be run from the repository root with
`python3 src/cli/app.py --login --base-url http://127.0.0.1:8080`; credentials
are then stored for normal `python3 src/cli/app.py` use. Do not make the CLI
depend on Django internals; use the SDK transport/client boundary.

## Code quality and design expectations

The project favors small, explicit, testable Python modules over framework
magic or duplicated implementations.

- Keep API controllers thin. Put database, filesystem, queue, and business
  logic in `src/api/services` or the appropriate worker/service module.
- Keep frontend and API responsibilities separate. The frontend should proxy
  requests and render UI; it should not acquire database credentials or
  duplicate API business logic.
- Keep worker handlers deterministic around their database transitions. Record
  meaningful job IDs, profile IDs, stages, and paths in logs without logging
  passwords, cookie contents, or other secrets.
- Preserve queue names, job types, payload compatibility, and idempotency
  semantics unless a change includes all publishers, consumers, schema work,
  and tests.
- Use safe path resolution and profile roots for every media/subtitle path.
  Never trust a user-supplied path or allow it to escape the configured output
  root.
- Use atomic file writes/renames for media or sync outputs. Do not expose a
  partially downloaded or partially transcoded file as a completed library
  item.
- Prefer typed function signatures, `pathlib.Path`, explicit enums, narrow
  exception handling, and clear return values. Avoid `Any` and broad ignores;
  if a boundary genuinely needs them, keep the suppression local and explain
  why.
- Respect the current Python/tooling split: mypy targets Python 3.12, worker
  images target Python 3.12, and the frontend image uses the version declared
  in its Dockerfile. Do not introduce syntax unsupported by the declared
  runtime targets.
- Keep security defaults in place: CSRF protection for browser sessions,
  secure host/origin configuration, `X-Frame-Options`, content-type sniffing
  protection, and authenticated state-changing endpoints.
- **Do not commit Python bytecode** (`__pycache__/`, `*.pyc`, `*.pyo`, `*.pyd`).
  These are generated at runtime in containers and on local test machines.
  Ensure they are in `.gitignore` and use `git rm --cached` if any are
  accidentally committed. Bytecode files are version-specific and should never
  be shared across Python versions or environments.
- Do not commit secrets, generated downloads, model caches, credentials,
  coverage artifacts, or local Docker state.

The repository uses Ruff for linting, mypy strict mode for typed checks, McCabe
with a maximum complexity of 60, Bandit for security scanning, Vulture at 100%
confidence, unittest coverage, and Wapiti authenticated/public scans. Fix the
underlying code when practical; do not broadly disable a tool just to make a
check green.

## Testing and verification

### Fast local checks

Run the smallest relevant check while iterating, then run the full applicable
suite before handing off:

```bash
make test-compile       # Python bytecode compilation
make test-ruff          # Ruff on src, tests, and crons
make test-mypy          # mypy on src, tests, and crons
make test-mccabe        # fails above complexity 60
make test-bandit        # Bandit on source code
make test-vulture       # dead code at confidence 100
make test-coverage      # unittest discovery plus coverage report
```

The unit-test command used by the Makefile and CI is:

```bash
PYTHONPATH=src \
GETOFFLINE_DB_ENGINE=sqlite \
GETOFFLINE_DB_NAME=':memory:' \
GETOFFLINE_MODEL_CACHE_DIR="$PWD/.test-model-cache" \
python -m unittest discover -s tests -p 'test_*.py' -v
```

Tests should not require a running MySQL, RabbitMQ, Docker stack, YouTube
account, or local Whisper download. Keep external calls behind seams and use
the existing fakes/mocks. The test settings select in-memory SQLite before
Django setup; test classes should clean up temporary files and mutable global
state.

Although some tests use Django helpers and a `conftest.py` exists for setup,
the canonical runner is `unittest discover`, not pytest. Add tests as
`unittest.TestCase`-compatible modules matching `test_*.py` so CI discovers
them.

### Full checks

`make test` runs compilation, Ruff, McCabe, mypy, Bandit, Vulture, coverage,
and the authenticated Wapiti scan. It requires Docker for Wapiti and starts
the frontend/API services. It does not replace the end-to-end Compose test.

```bash
make test
```

Wapiti reports are written under `target/wapiti`. The authenticated scan
creates temporary credentials in the API container and removes them on exit.
If a scan is interrupted, verify that the temporary user is removed before
re-running it.

### Docker Compose integration tests

Integration tests start a real Compose stack with MySQL, RabbitMQ, API,
frontend, workers, scheduler, and cleanup services. They use temporary host
downloads and dynamically reserved ports. Docker Compose v2 or the legacy
`docker-compose` command is required.

```bash
make integration-test
```

The combined integration test runs the podcast discovery/download scenario
and the YouTube-shaped scenario in one stack. The YouTube test sets
`GETOFFLINE_YTDLP_MODULE=workers.fake_ytdlp`, so it does not contact YouTube;
the podcast scenario uses its configured RSS/media source and therefore needs
network access. Individual entry points are also available:

```bash
make integration-test-youtube
make integration-test-podcast
```

Useful integration controls:

- `GETOFFLINE_INTEGRATION_TIMEOUT` changes the polling deadline.
- `GETOFFLINE_INTEGRATION_KEEP_STACK=1` leaves the Compose project running for
  debugging. Bring it down manually afterward with the project name shown in
  the test output.
- Integration output includes Compose commands and streamed service logs;
  inspect API/worker logs before changing retry or timeout behavior.

The integration tests verify more than HTTP responses: database job state,
queue routing, source discovery, media output, transcript/subtitle artifacts,
explicit-content handling, and host/container download-path mapping. Changes
to Docker service names, healthchecks, entrypoints, queue names, output paths,
or worker commands must update the helper assertions and end-to-end tests.

## CI expectations

GitHub Actions currently runs separate jobs for compilation, Ruff, McCabe,
mypy, Vulture, Bandit, unit-test coverage, and then a Compose integration job
that depends on the quality jobs. Match the actual workflow in
`.github/workflows/ci.yml` when diagnosing CI; do not assume a Makefile target
is what CI executes.

For a CI failure:

1. Reproduce the exact command from the workflow locally.
2. Identify whether the failure is source, test, Docker, environment, or
   network related.
3. Make the smallest behavior-preserving fix.
4. Add or update a focused unit/integration test for behavior changes.
5. Run the exact failed check and the relevant broader checks.

Do not hide failures with `|| true`, disable a whole lint rule, skip a worker,
or weaken healthchecks unless that is the explicitly intended design change.

## Common troubleshooting

- **API is unhealthy:** inspect `docker compose logs api mysql rabbitmq`.
  Confirm MySQL credentials, wait for healthchecks, and run `make migrate-db`
  only against the intended database.
- **Browser CSRF/403 errors:** verify the public browser origin is in
  `GETOFFLINE_CSRF_TRUSTED_ORIGINS`, the proxy preserves `Host`, and secure
  cookie settings match HTTP versus HTTPS.
- **Jobs remain queued:** inspect RabbitMQ and worker logs, confirm the worker
  type consumes the queue selected by `frontend.routing.queue_name`, and check
  that the job profile/source payload is valid. Use
  `GETOFFLINE_REQUEUE_EXISTING_JOBS=1` only for deliberate recovery after a
  broker reset.
- **Downloads are missing:** inspect the API/worker volume mapping and the
  profile `output_root`; remember that container paths map to the host through
  `GETOFFLINE_DOWNLOADS_DIR`.
- **Transcripts are slow or fail at startup:** check the transcript image's
  model cache and available memory/CPU. Use the configured chunk threshold and
  chunk duration rather than loading arbitrarily large files into one job.
- **Integration cleanup fails:** the test attempts to make Docker-created
  download files writable before removing its temporary stack. Check the
  Compose project name and use `docker compose down -v --remove-orphans` only
  for the temporary integration project, never the user's persistent stack.

When documenting a fix, include the command run, the relevant service or job
type, and whether verification used the in-memory unit environment or the real
Docker integration environment.
