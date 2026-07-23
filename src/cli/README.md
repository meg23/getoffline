# GetOffline CLI

The GetOffline CLI is a lightweight terminal application for browsing, searching, and playing media from your GetOffline library. It supports playback progress, favorites, played/unplayed filters, downloads, local media players, and optional audio-bridge playback.

From the repository root, start it with Python 3:

```bash
python3 src/cli/app.py --login --base-url http://127.0.0.1:8080
```

After saving credentials, start it normally with:

```bash
python3 src/cli/app.py
```

Run `python3 src/cli/app.py --help` to see additional options.
