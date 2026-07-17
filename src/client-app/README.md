# GetOffline Console client

`app.py` is a dependency-light ncurses client for GetOffline. It uses
the `getoffline-sdk` package for API communication and delegates audio playback
to either a terminal-friendly local player (`mpv`, `cvlc`, `vlc`, VLC.app on macOS, or `ffplay`) or a generic HTTP audio bridge.

## Run

From this repository:

```bash
PYTHONPATH=../packages:.. python app.py --login --base-url http://localhost:8000
PYTHONPATH=../packages:.. python app.py
PYTHONPATH=../packages:.. python app.py --playback-backend bridge --bridge-url http://bridge.local/play --volume 0.75
```

If `getoffline-sdk` is installed in your environment, `PYTHONPATH` is not needed.
Credentials and optional playback defaults are stored in `$XDG_CONFIG_HOME/getoffline-console/credentials.json` or
`~/.config/getoffline-console/credentials.json` with `0600` permissions.

## Keys

- `j`/`k` or arrow keys: move through the library
- `Enter`/`p`: play the selected item from its saved resume position
- `s`: stop playback and save progress
- `-` / `+`: lower or raise playback volume
- `/`: search the library/transcripts
- `a`: queue a new YouTube/media URL for download
- `m` / `u`: mark selected media played or unplayed
- `f`: toggle favorite
- `1` / `2` / `3` / `4`: switch to unplayed, played, favorites, or all media
- `r`: refresh
- `q`: quit

## Notes

The app is intentionally console music-player-like: the terminal UI is always available while an
external player or bridge handles media decoding. Playback progress is periodically saved
through the SDK while playback is running and once again when playback stops. The local
player auto-detection prefers `mpv` or VLC before falling back to `ffplay`, because
`ffplay` can consume noticeably more CPU during startup/seeking on some systems.
On macOS, the client also checks the standard VLC.app binary at
`/Applications/VLC.app/Contents/MacOS/VLC`; you can pass that path with `--player`
if your shell cannot find it automatically.

The generic audio bridge mode posts to the configured `--bridge-url` with JSON containing
the GetOffline stream URL, an `Authorization` header, `seek_seconds`, `title`,
`media_kind`, `episode_id`, and `volume` as a clamped `0.0`-to-`1.0` gain. Runtime
volume changes post `session_id` and `volume` to `--bridge-volume-url`, or to a sibling
`/volume` endpoint when that option is omitted. If no `--bridge-stop-url` is provided, the client
posts stop requests to a sibling `/stop` endpoint, so `http://bridge.local/play`
defaults to `http://bridge.local/stop`.
