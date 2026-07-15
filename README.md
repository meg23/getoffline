# GetOffline: Automated Media Downloader

**GetOffline** is a Python-based tool to batch download YouTube videos and podcast episodes. Runtime defaults, source lists, and download settings (including full `cookies.txt` content) are persisted in the Django database and editable in the web UI.

## Features

- Batch download from YouTube playlists/channels and podcast RSS feeds
- Central Django database download history (replaces per-source text archives)
- Automatic audio extraction to MP3
- Configurable FFmpeg audio filter for automatic volume/loudness normalization on extracted audio and YouTube video audio tracks
- Automatic Whisper subtitle (`.srt`) generation for new audio downloads when `subtitles: true`
- YouTube Whisper subtitle generation is serialized to one worker for runtime stability on current Python/Whisper stacks
- Per-entry `subtitles` flag for YouTube and podcast sources (`true`/`false`)
- Optional per-source transcript filter that deletes newly downloaded YouTube videos or podcast episodes when conservative profanity or sexual-content terms are detected
- Optional per-entry `subtitle_offset_seconds` to override subtitle timing offset for that source
- Automatically skips YouTube live streams in configured sources while allowing a live video to be downloaded from the web app's **+** button
- Browser cookie support for private or age-restricted YouTube videos
- Database-backed runtime configuration managed from the web UI
- Built-in local web app for browsing and playing downloaded audio/video in your browser
- Username/password login with per-user settings, source feeds, playback history, and download folders
- Optional offline transfer that copies selected media to a directory on disk or to a connected Android phone with `adb push`

## Requirements

Install the following system tools first:

- `make`
- `ffmpeg` (includes `ffprobe`)
- `quickjs` / `qjs` (used by yt-dlp's YouTube challenge solver runtime)
- Python 3.8+

Python dependencies are installed automatically by the Makefile.

If `qjs` (QuickJS) is available on `PATH`, GetOffline configures yt-dlp to use the `quickjs` JavaScript runtime and enables the YouTube remote component (`ejs:github`) for challenge solving.

GetOffline applies the yt-dlp `youtube:player_js_variant=main` workaround for known challenge-solver instability (see yt-dlp issue #16256).

When upgrading yt-dlp from PyPI/pip, install with the default extra so EJS support is present:

```bash
pip install -U "yt-dlp[default]"
```

GetOffline sets `js_runtimes={"quickjs": {"path": "qjs"}}` and `--remote-components ejs:github` automatically when QuickJS is available, matching yt-dlp's external JavaScript runtime support while keeping the download image small.


## Configuration

On startup, defaults are seeded into the configured Django database automatically and can be edited at `/settings`. Environment variables configure the Django/MySQL connection for deployments; app runtime settings such as `output_root`, formats, limits, source lists, cookies, and transfer options live in the database.

YouTube live streams are skipped automatically for configured playlist and channel sources. To download a specific live video, paste its URL into the web app's **+** dialog. The download remains active until the stream ends or the application stops.

## Split app/workers deployment

For deployments that should keep the frontend responsive, the repo includes a Django frontend in `src/app`, shared Django ORM models in `src/models`, and RabbitMQ workers in `src/workers`. Both the app and workers connect to the same MySQL database through Django's ORM using PyMySQL, so no native mysqlclient build is required. The app reads data and publishes jobs; workers consume queue-specific jobs. Run only one downloads worker to avoid downloading too quickly from YouTube, keep the FFmpeg worker running so downloaded files are converted after download, and run multiple transfer workers if you need concurrency. Run `make migrate-db` after pulling Django model changes so existing MySQL tables get any missing columns, then use `make run-app-debug` to start the Django frontend with `GETOFFLINE_DJANGO_DEBUG=1`. See `docs/app-workers-mysql-rabbitmq.md`.


## Usage

Run the Django app directly in development mode:

```bash
make run-app-debug
```

To import local videos, open the web app and use the browser drag-and-drop importer. It copies supported files into `manual/<original-name>` under `output_root`, registers them as manual videos, and then runs the existing subtitle/filter pipeline. Existing manual files are renamed with numeric suffixes instead of overwritten.

Supported video extensions are `.mp4`, `.mkv`, `.webm`, and `.mov`. The importer skips non-video files and uses the **Delete drag-and-drop uploads containing profanity or sexual content** setting under **Settings → General**.

Then open `http://127.0.0.1:8080` in your browser to play audio/video files from your library.

Audio and video files dragged into the library page are copied into the `manual`
folder and transcribed. In **Settings → General**, enable **Delete drag-and-drop
uploads containing profanity or sexual content** to screen those generated
transcripts with the same filter used for configured YouTube and podcast
sources. Matching uploads and their sidecars are deleted, marked as filtered in
the database, and recorded with a `CONTENT_FILTER_DELETION` audit event.

Open `http://127.0.0.1:8080/settings` to edit persisted defaults (`output_root`, formats, limits, etc.), store the full YouTube `cookies.txt` payload directly in the database, and manage YouTube/podcast sources with add/delete/enable/disable controls. Each source also has a **Delete downloads containing profanity or sexual content** checkbox. When enabled, GetOffline transcribes every new item for screening, deletes matching media and sidecars, and records it as filtered so it is not downloaded again. This local profanityfilter-based filter is intentionally conservative and can miss context, euphemisms, or transcription errors.

To test the transcript filter inside Docker, rebuild the transcript worker image
after pulling code changes, then run the diagnostic CLI:

```sh
docker compose build worker-transcripts
docker compose up -d worker-transcripts
docker compose exec worker-transcripts python -m workers.content_filter --text "that was shit"
```

The expected output is `matched category=profanity term='profanityfilter'` followed
by the matched sentence. If you still see an old warning such as
`profanityfilter is unavailable`, the running container is using an older image;
rebuild and recreate the worker with the commands above.

Use the **Update Downloads** button in the web UI to trigger background downloads immediately, and use **Mark played**/**Mark unplayed** to track listening/watching progress.

Downloads are also checked automatically on the interval configured in **Settings → Auto update interval (minutes)** (default: 20).

The web app requires username/password login. Create users from the command line after migrations with `python -m django create_user <username> --password <password>`. The old profile switcher has been removed; each signed-in user gets one implicit library/settings partition keyed by their username.

## Directory transfer

GetOffline can copy selected downloads to a normal directory on disk (including a mounted external drive) or to an Android phone so they are available to watch or listen to offline. Choose **Local disk** or **Android device** in Settings; Android-only ADB settings are hidden when Local disk is selected. Directory transfer writes media, optional subtitles, `GetOffline.xspf`, and a `transferdb.txt` history file directly to the selected folder. Paths recorded in `transferdb.txt` are skipped on later runs so tagged media is not copied repeatedly.

For a filesystem-only cron sync that does not use the database transfer queue, use `scripts/sync-media-downloads.sh`. It accepts the downloads directory, the destination directory, and the owner to apply to copied files. Each copied audio/video file is flattened into the destination as `<artist> - <original filename>`, where `<artist>` is the source file's parent folder name. Existing destination files are skipped unless the source file is newer. Example cron entry to run every 15 minutes:

```cron
*/15 * * * * /path/to/getoffline/scripts/sync-media-downloads.sh /srv/getoffline/downloads /mnt/offline-media getoffline:getoffline >> /var/log/getoffline-media-sync.log 2>&1
```

Set `DRY_RUN=1` before the command to preview the planned copies, or `VERBOSE=1` to log up-to-date files that were skipped. After each non-dry-run sync, the script runs `chown -R <owner[:group]>` on the destination so all synced files are owned by the requested user, such as `jellyfin:jellyfin`.

Android transfer uses Android Debug Bridge (`adb`), which is more automation-friendly than the standard MTP file browser.

To configure Android transfer:

1. Install Android platform tools so `adb` is available on the computer running GetOffline.
2. Enable Developer options and USB debugging on the phone, then authorize the computer when Android prompts you.
3. Open `http://127.0.0.1:8080/settings`, choose **Android device**, and optionally enable automatic transfer after downloads.
4. Choose the phone folder, for example `/sdcard/Movies/GetOffline`, and the maximum number of unplayed items to copy each transfer.

To transfer over Wi-Fi, pair the device with `adb` first, then switch **ADB connection** to **Wi-Fi (connect to paired device)** in settings and enter the device address, such as `192.168.1.50:5555`. GetOffline runs `adb connect <address>` before each transfer/delete job and then uses that Wi-Fi serial for normal `adb push`, shell, and media-scan commands. If you omit a port, GetOffline defaults to `:5555`.

When running with Docker Compose, `adb` is installed in the `worker-transfer` container and its pairing keys are kept in the `adb-data` volume. Pair from inside that same container so the worker can reuse the key later:

```bash
docker compose exec worker-transfer adb pair PHONE_IP:PAIRING_PORT
docker compose exec worker-transfer adb connect PHONE_IP:5555
docker compose exec worker-transfer adb devices
```

Use the **Wireless debugging** screen on Android for the temporary pairing port and code. The pairing port is usually different from the later `5555` connection port. If `adb devices` shows `unauthorized`, accept the authorization prompt on the phone or pair again from the container. USB debugging from containers requires passing the host USB bus into the container (for example `/dev/bus/usb`) and may require privileged device access, so Wi-Fi debugging is the simpler container setup.

When enabled, GetOffline periodically transfers to the selected destination using the same interval as automatic download checks, and it also attempts a transfer after new downloads finish. The **Save and transfer** button in Settings persists the configuration and starts a transfer immediately. Completed destination paths are recorded in `transferdb.txt` and skipped on later runs. When `ffmpeg` is available, GetOffline tags copied media with VLC-visible title/artist/album metadata and embeds podcast artwork when the feed provides an image. Android transfer also asks the device's media scanner to rescan pushed files. Each transfer writes `GetOffline.xspf`, a VLC-compatible playlist with titles, source names, file locations, and each item's saved playback position as a VLC `start-time` option.

Clean up generated files:

```bash
./scripts/clean.sh
```

## Output

Downloaded files are stored under the `output_root` directory, sorted by source name and upload date.

Download tracking and app settings are stored in the configured Django database. This includes media metadata plus key/value defaults and a dedicated download-settings row for persisted YouTube cookie text.

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

### Docker Compose split deployment

A Docker Compose deployment is available for the Django frontend with bundled nginx, RabbitMQ, MySQL, and all worker types. The Compose build uses separate multi-stage images for the frontend/migration path, lightweight workers, download workers, a lightweight Alpine FFmpeg worker, and Debian slim transcript workers so each runtime image keeps only its runtime OS packages, prebuilt Python wheels, and application files; FFmpeg is installed in the download worker image for yt-dlp post-processing, in the FFmpeg worker image for queued conversions, and in the transcript image so long audio can be probed and split into bounded transcription chunks; deferred explicit-content screening after queued conversion runs in the transcript worker rather than the FFmpeg worker. The transcript image preinstalls the default faster-whisper model at build time (override with `--build-arg WHISPER_MODEL=<model>` if needed) so first transcription jobs do not download Hugging Face model files into a runtime volume. MySQL data is persisted in the named `mysql-data` volume, RabbitMQ data is persisted in `rabbitmq-data`, and downloaded media is bind-mounted from `GETOFFLINE_DOWNLOADS_DIR` (default `./downloads`). Set `GETOFFLINE_DJANGO_SECRET_KEY` and optionally override `GETOFFLINE_DB_NAME`, `GETOFFLINE_DB_USER`, `GETOFFLINE_DB_PASSWORD`, and `GETOFFLINE_DB_ROOT_PASSWORD`, then run `docker compose up --build -d`. Downloader logs should show inline FFmpeg conversion before transcript jobs are queued when conversion is needed. Video conversion defaults to fast H.264/AAC MP4 for Jellyfin-friendly playback. The frontend container serves bundled nginx on `http://localhost:8080`, while RabbitMQ management is exposed on `http://localhost:15672`; set `GETOFFLINE_CSRF_TRUSTED_ORIGINS` if you expose the app on another host or scheme. The `migrate` service runs automatically before the frontend and workers start; rerun it manually with `docker compose run --rm migrate` after pulling model changes if needed.
