# GetOffline

GetOffline is a self-hosted media library that downloads YouTube videos and podcast episodes into a searchable, browser-based library. It includes background workers for downloads and FFmpeg conversion, Whisper transcript generation, subtitle support, explicit-content filtering, playback progress and favorites, scheduled updates, per-user libraries and settings, and username/password authentication. Start the complete application with Docker Compose by setting a Django secret and launching the services:

```bash
export GETOFFLINE_DJANGO_SECRET_KEY="replace-with-a-long-random-secret"
docker compose up --build -d
```

Then open [http://127.0.0.1:8080](http://127.0.0.1:8080) in a browser. Downloaded media is stored in `./downloads` by default; set `GETOFFLINE_DOWNLOADS_DIR` to use another host directory. Create a user after startup with `docker compose exec api python -m django create_user <username> --password <password>`.
