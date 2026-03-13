# Listens: Automated Media Downloader

**Listens** is a Python-based tool to batch download YouTube videos and podcast episodes. Source lists still come from `config.yaml`, while runtime defaults and download settings (including full `cookies.txt` content) are now persisted in SQLite and editable in the web UI.

## Features

- Batch download from YouTube playlists/channels and podcast RSS feeds
- Central SQLite download history using SQLAlchemy (replaces per-source text archives)
- Automatic audio extraction to MP3
- Automatic Whisper subtitle (`.srt`) generation for new audio downloads when `subtitles: true`
- Per-entry `subtitles` flag for YouTube and podcast sources (`true`/`false`)
- Optional per-entry `subtitle_offset_seconds` to override subtitle timing offset for that source
- Automatically skips YouTube live streams
- Browser cookie support for private or age-restricted YouTube videos
- Easy YAML configuration
- Built-in local web app for browsing and playing downloaded audio/video in your browser

## Requirements

- Python 3.8+
- `yt-dlp`
- `feedparser`
- `browser_cookie3`
- `PyYAML`
- `ffmpeg`/`ffprobe`
- `openai-whisper`
- `SQLAlchemy`

Install dependencies:

```bash
pip install -r src/requirements.txt
```

## Configuration

Edit `config.yaml` to define your YouTube playlists and podcast RSS feeds:

```yaml

defaults:
  output_root: ./downloads
  database_path: ./downloads/downloads.sqlite3
  audio_format: mp3
  audio_quality: 0
  max_downloads: 3
  playlist_end: 3
  cookie_path: /tmp/cookies.txt

youtube:
  - name: ACG
    url: https://www.youtube.com/playlist?list=...
    type: audio
    subtitles: true

podcasts:
  - name: TheTimDillonShow
    url: https://audioboom.com/channels/...
    subtitles: true
```

YouTube live streams are skipped automatically and will not be downloaded.

## Usage

Run the downloader:

```bash
python src/main.py download
```

Start the local media web app:

```bash
python src/main.py serve --host 127.0.0.1 --port 8080
```

Then open `http://127.0.0.1:8080` in your browser to play audio/video files from your library.

Open `http://127.0.0.1:8080/settings` to edit persisted defaults (`output_root`, formats, limits, etc.) and store the full YouTube `cookies.txt` payload directly in the database.

Use the **Update Downloads** button in the web UI to trigger background downloads without running a second process, and use **Mark played**/**Mark unplayed** to track listening/watching progress.

To build a standalone executable with Pex:

```bash
./scripts/build.sh
```

Clean up generated files:

```bash
./scripts/clean.sh
```

## Output

Downloaded files are stored under the `output_root` directory, sorted by source name and upload date.

Download tracking and app settings are stored in one SQLite database (`defaults.database_path`, default: `<output_root>/downloads.sqlite3`). This includes media metadata plus key/value defaults and a dedicated download-settings row for persisted YouTube cookie text.

For newly downloaded YouTube/podcast audio media, subtitles are generated with Whisper when `subtitles: true` (YouTube-provided captions are not downloaded, and video items do not get subtitles).
A per-entry timing adjustment can be set with `subtitle_offset_seconds` to keep SRT timing in sync:

- `<playback_media>.srt`

VLC auto-loads these subtitles when the `.srt` basename matches the media file in the same folder.

## Logging

Logs are written to:

```bash
~/youtube/youtube_batch_dl.log
```

And streamed to your terminal.
