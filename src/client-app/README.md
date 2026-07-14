# GetOffline Console client

`getoffline_console.py` is a dependency-light ncurses client for GetOffline. It uses
the `getoffline-sdk` package for API communication and delegates audio playback
to a terminal-friendly player (`mpv`, `ffplay`, `cvlc`, or `vlc`).

## Run

From this repository:

```bash
PYTHONPATH=../packages:.. python getoffline_console.py --login --base-url http://localhost:8000
PYTHONPATH=../packages:.. python getoffline_console.py
```

If `getoffline-sdk` is installed in your environment, `PYTHONPATH` is not needed.
Credentials are stored in `$XDG_CONFIG_HOME/getoffline-console/credentials.json` or
`~/.config/getoffline-console/credentials.json` with `0600` permissions.

## Keys

- `j`/`k` or arrow keys: move through the library
- `Enter`/`p`: play the selected item from its saved resume position
- `s`: stop playback and save progress
- `/`: search the library/transcripts
- `a`: queue a new YouTube/media URL for download
- `m` / `u`: mark selected media played or unplayed
- `f`: toggle favorite
- `1` / `2` / `3` / `4`: switch to unplayed, played, favorites, or all media
- `r`: refresh
- `q`: quit

## Notes

The app is intentionally console music-player-like: the terminal UI is always available while an
external player handles media decoding. Playback progress is periodically saved
through the SDK while the player process is running and once again when playback
stops.
