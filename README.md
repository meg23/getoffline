# GetOffline

[![Vibecoded](https://img.shields.io/badge/vibecoded-yes-ff69b4?style=flat-square)](https://github.com/meg23/getoffline)

GetOffline is a self-hosted media library for saving YouTube videos and podcast episodes for offline playback. It combines a browser-based library with background download workers, FFmpeg conversion, transcript generation, and per-user settings.

## Features

- YouTube channels, playlists, individual videos, and podcast RSS feeds
- Browser playback with search, filters, favorites, and playback progress
- Automatic audio conversion, subtitles, and Whisper transcripts
- Scheduled source updates and manual downloads from the dashboard
- Optional explicit-content filtering for downloaded and uploaded media
- Username/password authentication with separate libraries and settings

## Start with Docker Compose

The registry-backed deployment is defined separately under `stacks/`, so the
normal root Compose file remains the local-build workflow. It starts the
frontend, API, database, RabbitMQ, an in-cluster Docker Registry, the
scheduler, and worker services. Set a secret key, publish the application
images to the registry, then start the stack:

```bash
export GETOFFLINE_DJANGO_SECRET_KEY="replace-with-a-long-random-secret"
make registry-release
make compose-up
```

The registry is available to the host Docker daemon at `localhost:5000` and
stores its data in the `registry-data` volume. The application services pull
their images from it using `GETOFFLINE_IMAGE_REGISTRY` and
`GETOFFLINE_IMAGE_TAG`:

```bash
export GETOFFLINE_IMAGE_REGISTRY="localhost:5000"
export GETOFFLINE_IMAGE_TAG="2026-07-31"
make registry-release
make compose-up
```

For a private network or remote Docker host, set `GETOFFLINE_IMAGE_REGISTRY`
to the registry address reachable by that host's Docker daemon. The registry
must be configured for TLS/authentication before exposing it beyond a trusted
local network; the bundled registry is intentionally a simple internal
registry for the Compose cluster.

The stack explicitly reuses the original `getoffline_mysql-data` and
`getoffline_rabbitmq-data` volumes. If the original Compose project used a
custom project name, set `GETOFFLINE_MYSQL_VOLUME_NAME` and
`GETOFFLINE_RABBITMQ_VOLUME_NAME` to those existing Docker volume names.

Workers keep using a mounted file when one is available. If a processing
worker cannot see that path, configure the API and worker containers with the
same strong secret and an API URL reachable by the worker:

```bash
export GETOFFLINE_WORKER_API_TOKEN="replace-with-a-long-random-worker-token"
export GETOFFLINE_WORKER_API_URL="http://api:8000/api"
```

The worker then fetches the profile-scoped media artifact from the internal
authenticated API endpoint and stores it in its local cache at
`/tmp/getoffline-worker-media` (configurable with
`GETOFFLINE_WORKER_MEDIA_CACHE_DIR`). The download is written to a temporary
file and atomically renamed after its size is verified. Keep this endpoint on a
trusted network and do not expose the worker token to browsers.

For local development and integration tests, use the root Compose file as
before:

```bash
docker compose up --build -d
```

The API automatically applies database migrations and serves the application backend while the frontend serves the app at [http://127.0.0.1:8080](http://127.0.0.1:8080). Create the first user from the API container:

```bash
docker compose exec api python -m django create_user alice --password 'change-this-password'
```

Then sign in through the browser and add YouTube or podcast sources from **Settings**. Use **Update Downloads** to check sources immediately; scheduled updates run using the configured interval.

## Docker Swarm deployment

For a true two-node deployment with shared service DNS and cross-host
replicas, use [`stacks/docker-stack.yml`](/Users/maxgelman/git/getoffline/stacks/docker-stack.yml)
instead of the split Compose files above. The stack places the API, frontend,
database, RabbitMQ, downloaders, scheduler, cleanup, and one copy of each
heavy worker on the manager. Additional FFmpeg and transcript replicas run on
the Windows node.

Initialize Swarm on Debian and join the Windows Docker node using the command
printed by `docker swarm join-token worker`:

```bash
docker swarm init --advertise-addr DEBIAN_IP
docker swarm join-token worker
docker node update --label-add role=manager-processing debian
docker node update --label-add role=windows-processing WINDOWS_NODE_NAME
```

Set the stack variables in a protected env file on Debian:

```dotenv
GETOFFLINE_IMAGE_REGISTRY=192.168.1.10:5000
GETOFFLINE_IMAGE_TAG=latest
GETOFFLINE_DJANGO_SECRET_KEY=long-random-secret
GETOFFLINE_WORKER_API_TOKEN=long-random-worker-token
GETOFFLINE_DB_PASSWORD=strong-db-password
GETOFFLINE_DB_ROOT_PASSWORD=strong-root-password
GETOFFLINE_RABBITMQ_USER=getoffline-worker
GETOFFLINE_RABBITMQ_PASSWORD=strong-rabbit-password
GETOFFLINE_MANAGER_DOWNLOADS_DIR=/srv/getoffline/downloads
GETOFFLINE_WINDOWS_FFMPEG_REPLICAS=3
GETOFFLINE_WINDOWS_TRANSCRIPT_REPLICAS=3
```

Build and publish the images, then deploy the single stack from Debian:

```bash
GETOFFLINE_IMAGE_REGISTRY=192.168.1.10:5000 \
GETOFFLINE_IMAGE_TAG=latest make registry-release
docker stack deploy --compose-file stacks/docker-stack.yml getoffline
docker stack services getoffline
```

Both Docker daemons must be able to pull from the registry. If it is an HTTP
registry, configure it as an insecure registry on both nodes; TLS is preferred.
Windows workers do not mount the manager's downloads directory. They fetch
missing inputs through the authenticated API, process them in their local
cache, and upload converted media and subtitles back to the manager API.

The Swarm deployment uses separate named volumes by default:
`getoffline_swarm_mysql-data`, `getoffline_swarm_rabbitmq-data`, and
`getoffline_swarm_registry-data`. This prevents it from touching data created
by the regular Compose deployment. To deliberately migrate or reuse existing
data, set `GETOFFLINE_SWARM_MYSQL_VOLUME_NAME`,
`GETOFFLINE_SWARM_RABBITMQ_VOLUME_NAME`, or
`GETOFFLINE_SWARM_REGISTRY_VOLUME_NAME` to the intended existing volume name
after taking a backup.

## Storage and configuration

Downloaded media is stored in `./downloads` by default. To use another host directory, set `GETOFFLINE_DOWNLOADS_DIR` before starting Compose:

```bash
export GETOFFLINE_DOWNLOADS_DIR="/path/to/media"
docker compose up -d
```

Stop the application with `docker compose down`. Database and RabbitMQ data are kept in named Docker volumes.
