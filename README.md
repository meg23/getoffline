# GetOffline: Automated Media Downloader

**GetOffline** is a Python-based tool to batch download YouTube videos and podcast episodes. Runtime defaults, source lists, and download settings (including full `cookies.txt` content) are persisted in SQLite and editable in the web UI.

## Features

- Batch download from YouTube playlists/channels and podcast RSS feeds
- Central SQLite download history using SQLAlchemy (replaces per-source text archives)
- Automatic audio extraction to MP3
- Configurable FFmpeg audio filter for automatic volume/loudness normalization on extracted audio and YouTube video audio tracks
- Automatic Whisper subtitle (`.srt`) generation for new audio downloads when `subtitles: true`
- YouTube Whisper subtitle generation is serialized to one worker for runtime stability on current Python/Whisper stacks
- Per-entry `subtitles` flag for YouTube and podcast sources (`true`/`false`)
- Optional per-source transcript filter that deletes newly downloaded YouTube videos or podcast episodes when conservative profanity or sexual-content terms are detected
- Optional per-entry `subtitle_offset_seconds` to override subtitle timing offset for that source
- Automatically skips YouTube live streams in configured sources while allowing a live video to be downloaded from the web app's **+** button
- Browser cookie support for private or age-restricted YouTube videos
- Database-backed runtime configuration with optional `config.yml` bootstrap paths
- Built-in local web app for browsing and playing downloaded audio/video in your browser
- Separate profiles with isolated databases, settings, source feeds, playback history, and download folders
- Optional offline sync that copies selected media to a directory on disk or to a connected Android phone with `adb push`

## Requirements

Install the following system tools first:

- `make`
- `ffmpeg` (includes `ffprobe`)
- `deno` (used by yt-dlp's YouTube challenge solver runtime)
- Python 3.8+

Python dependencies are installed automatically by the Makefile.

If `deno` is available on `PATH`, GetOffline enables yt-dlp's recommended YouTube remote component (`ejs:github`) for challenge solving.

GetOffline applies the yt-dlp `youtube:player_js_variant=main` workaround for known challenge-solver instability (see yt-dlp issue #16256).

When upgrading yt-dlp from PyPI/pip, install with the default extra so EJS support is present:

```bash
pip install -U "yt-dlp[default]"
```

GetOffline sets `--remote-components ejs:github` automatically when `deno` is available, matching yt-dlp guidance for non-GitHub-release installs.


## Configuration

On startup, defaults are seeded in SQLite automatically and can be edited at `/settings`.
Initial bootstrap values come from built-in defaults, an optional `config.yml`, and optional environment overrides:

```yaml
defaults:
  output_root: ./downloads
  database_path: ./downloads/downloads.sqlite3
```

If `config.yml` is present in the current working directory, relative paths are resolved from that directory. Without a config file, both paths default under a `downloads/` folder in the current working directory.

Environment variables still override the file when needed:

- `GETOFFLINE_OUTPUT_ROOT`
- `GETOFFLINE_DATABASE_PATH`

YouTube live streams are skipped automatically for configured playlist and channel sources. To download a specific live video, paste its URL into the web app's **+** dialog. The download remains active until the stream ends or the application stops.

## Usage

Build and run the app:

```bash
make run
```

This command creates a virtual environment, installs Python dependencies, validates required system dependencies, builds the executable, and runs the app.

You can still run the Python entrypoint directly if needed:

```bash
python src/main.py
```

Then open `http://127.0.0.1:8080` in your browser to play audio/video files from your library.

Open `http://127.0.0.1:8080/settings` to edit persisted defaults (`output_root`, formats, limits, etc.), store the full YouTube `cookies.txt` payload directly in the database, and manage YouTube/podcast sources with add/delete/enable/disable controls. Each source also has a **Delete downloads containing profanity or sexual content** checkbox. When enabled, GetOffline transcribes every new item for screening, deletes matching media and sidecars, and records it as filtered so it is not downloaded again. This local term-based filter is intentionally conservative and can miss context, euphemisms, or transcription errors.

Use the **Update Downloads** button in the web UI to trigger background downloads immediately, and use **Mark played**/**Mark unplayed** to track listening/watching progress.

Downloads are also checked automatically on the interval configured in **Settings → Auto update interval (minutes)** (default: 20). Automatic checks continue for every profile, not only the profile currently visible in the web app.

Use the profile menu in the top-right corner of the library or Settings page to switch profiles, create a profile, or rename the current profile. The initial `default` profile keeps the existing database and download directory. Each newly created profile receives its own database, settings, source lists, playback history, and download directory.

## Directory sync

GetOffline can copy selected downloads to a normal directory on disk (including a mounted external drive) or to an Android phone so they are available to watch or listen to offline. Choose **Local disk** or **Android device** in Settings; Android-only ADB settings are hidden when Local disk is selected. Directory sync writes media, optional subtitles, `GetOffline.xspf`, and a `syncdb.txt` history file directly to the selected folder. Paths recorded in `syncdb.txt` are skipped on later runs so tagged media is not copied repeatedly.

Android sync uses Android Debug Bridge (`adb`), which is more automation-friendly than the standard MTP file browser.

To configure Android sync:

1. Install Android platform tools so `adb` is available on the computer running GetOffline.
2. Enable Developer options and USB debugging on the phone, then authorize the computer when Android prompts you.
3. Open `http://127.0.0.1:8080/settings`, choose **Android device**, and optionally enable automatic sync after downloads.
4. Choose the phone folder, for example `/sdcard/Movies/GetOffline`, and the maximum number of unplayed items to copy each sync.

To sync over Wi-Fi, pair the device with `adb` first, then switch **ADB connection** to **Wi-Fi (connect to paired device)** in settings and enter the device address, such as `192.168.1.50:5555`. GetOffline runs `adb connect <address>` before each sync/delete job and then uses that Wi-Fi serial for normal `adb push`, shell, and media-scan commands. If you omit a port, GetOffline defaults to `:5555`.

When enabled, GetOffline periodically syncs the selected destination using the same interval as automatic download checks, and it also attempts a sync after new downloads finish. The **Save and sync** button in Settings persists the configuration and starts a sync immediately. Completed destination paths are recorded in `syncdb.txt` and skipped on later runs. When `ffmpeg` is available, GetOffline tags copied media with VLC-visible title/artist/album metadata and embeds podcast artwork when the feed provides an image. Android sync also asks the device's media scanner to rescan pushed files. Each sync writes `GetOffline.xspf`, a VLC-compatible playlist with titles, source names, file locations, and each item's saved playback position as a VLC `start-time` option.

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

Media rows now keep a relative path reference alongside the resolved file path so you can move the downloads directory and keep database-backed playback references working after updating `output_root`.

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

Every media deletion performed by the transcript filter writes a warning-level
`CONTENT_FILTER_DELETION` audit event to this log. The event includes the source
type and name, item title, matched category and term, original media path, and
the list of artifacts that were successfully deleted. For example, these events
can be reviewed with:

```bash
grep 'CONTENT_FILTER_DELETION' ~/youtube/youtube_batch_dl.log
```
