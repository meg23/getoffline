# GetOffline

[![Vibecoded](https://img.shields.io/badge/vibecoded-yes-ff69b4?style=flat-square)](https://github.com/meg23/getoffline)

GetOffline is a self-hosted media library for saving YouTube videos and podcast episodes for offline playback. It combines a browser-based library with background download workers, FFmpeg conversion, transcript generation, and per-user settings.

## Features

- YouTube channels, playlists, individual videos, and podcast RSS feeds
- Browser playback with search, filters, favorites, and playback progress
- Automatic audio conversion, subtitles, and Whisper transcripts
- Scheduled source updates and manual downloads from the dashboard
- Optional explicit-content filtering for downloaded and uploaded media
- Opt-in video profanity censorship that transcribes staged media first, redacts
  subtitles/search text, and applies mute or beep filtering during conversion
- Username/password authentication with separate libraries and settings

## Start with Docker Compose

Docker Compose starts the frontend, API, database, RabbitMQ, scheduler, and worker services. Set a secret key, then build and start the stack:

```bash
export GETOFFLINE_DJANGO_SECRET_KEY="replace-with-a-long-random-secret"
docker compose up --build -d
```

The API automatically applies database migrations and serves the application backend while the frontend serves the app at [http://127.0.0.1:8080](http://127.0.0.1:8080). Create the first user from the API container:

```bash
docker compose exec api python -m django create_user alice --password 'change-this-password'
```

Then sign in through the browser and add YouTube or podcast sources from **Settings**. Use **Update Downloads** to check sources immediately; scheduled updates run using the configured interval.

Automatic video censorship is disabled by default. Enable it per profile in
**Settings**, choose mute or beep, and optionally retain the uncensored file with
an `_original` suffix. YouTube sources can inherit or override that policy.
Dashboard video links and drag-and-drop videos use the profile policy; API/SDK
downloads remain audio by default and use it only when `media_type=video` is
requested. Existing uncensored videos can be processed with the player or
library **Censor** action. Media stays unavailable while censoring is in progress.

## Storage and configuration

Downloaded media is stored in `./downloads` by default. To use another host directory, set `GETOFFLINE_DOWNLOADS_DIR` before starting Compose:

```bash
export GETOFFLINE_DOWNLOADS_DIR="/path/to/media"
docker compose up -d
```

Stop the application with `docker compose down`. Database and RabbitMQ data are kept in named Docker volumes.

## Building with Podman

If you're using Podman instead of Docker, use Podman's current `podman compose`
subcommand. It delegates to the installed Compose provider (for example,
`podman-compose`):

```bash
# Rebuild all services
podman compose up --build -d

# Rebuild a specific service (e.g., after code changes)
podman compose up --build -d api

# Force rebuild without cache
podman compose build --no-cache
podman compose up -d

# Rebuild without starting containers
podman compose build api
```

Build individual images with the podman CLI:

```bash
# Build the API image
podman build -f deploy/docker/api.Dockerfile -t getoffline_api:latest .

# Build the frontend
podman build -f deploy/docker/frontend.Dockerfile -t getoffline_frontend:latest .

# Build with no cache
podman build --no-cache -f deploy/docker/api.Dockerfile -t getoffline_api:latest .
```

Clean rebuild (remove old images first):

```bash
podman compose down
podman compose build --no-cache
podman compose up -d
```

View logs and service status:

```bash
# View logs
podman compose logs -f api frontend

# Check service status
podman compose ps

# Restart services
podman compose restart
```
