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

Docker Compose starts the frontend, API, database, RabbitMQ, scheduler, and worker services. Set a secret key, then build and start the stack:

```bash
export GETOFFLINE_DJANGO_SECRET_KEY="replace-with-a-long-random-secret"
docker compose up --build -d
```

The frontend automatically applies database migrations and serves the app at [http://127.0.0.1:8080](http://127.0.0.1:8080). Create the first user from the API container:

```bash
docker compose exec api python -m django create_user alice --password 'change-this-password'
```

Then sign in through the browser and add YouTube or podcast sources from **Settings**. Use **Update Downloads** to check sources immediately; scheduled updates run using the configured interval.

## Storage and configuration

Downloaded media is stored in `./downloads` by default. To use another host directory, set `GETOFFLINE_DOWNLOADS_DIR` before starting Compose:

```bash
export GETOFFLINE_DOWNLOADS_DIR="/path/to/media"
docker compose up -d
```

Stop the application with `docker compose down`. Database and RabbitMQ data are kept in named Docker volumes.
