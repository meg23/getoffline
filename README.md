# 🎧 Listens: Automated Media Downloader

**Listens** is a Python-based tool to batch download YouTube videos and podcast episodes using a simple YAML configuration. It supports cookies for YouTube downloads, automatic audio extraction using `yt-dlp`, and optional ad scrubbing for downloaded audio.

## 🚀 Features

- Batch download from YouTube playlists/channels and podcast RSS feeds
- Automatic audio extraction to MP3
- Optional AI-assisted ad scrubbing for:
  - YouTube entries configured as `type: audio`
  - Downloaded podcast episodes
  - Automatic subtitle (`.srt`) generation for new downloads when `subtitles: true`
- Per-entry `scrub` flag for YouTube and podcast sources (`true`/`false`)
- Per-entry `subtitles` flag for YouTube and podcast sources (`true`/`false`)
- Automatically skips YouTube live streams
- Browser cookie support for private or age-restricted YouTube videos
- Easy YAML configuration

## 📦 Requirements

- Python 3.8+
- `yt-dlp`
- `feedparser`
- `browser_cookie3`
- `PyYAML`
- `ffmpeg`/`ffprobe`
- `openai-whisper` (only needed if ad scrubbing is enabled)

Install dependencies:

```bash
pip install -r src/requirements.txt
```

## ⚙️ Configuration

Edit `config.yaml` to define your YouTube playlists and podcast RSS feeds:

```yaml
defaults:
  output_root: ./downloads
  audio_format: mp3
  audio_quality: 0
  max_downloads: 3
  playlist_end: 3
  cookie_path: /tmp/cookies.txt
  ad_scrubber:
    enabled: true
    model: base
    min_ad_seconds: 8.0
    pre_roll: 2.0
    post_roll: 2.0
    min_hits: 1

youtube:
  - name: ACG
    url: https://www.youtube.com/playlist?list=...
    type: audio
    scrub: true
    subtitles: true

podcasts:
  - name: TheTimDillonShow
    url: https://audioboom.com/channels/...
    scrub: true
    subtitles: true
```

Ad scrubbing is enabled by default (`defaults.ad_scrubber.enabled: true`). Set it to `false` if you want to disable it globally, or set `scrub: false` on individual YouTube/podcast entries to disable scrubbing just for those sources.

YouTube live streams are skipped automatically and will not be downloaded.

## 🛠 Usage

Run the downloader:

```bash
python src/main.py
```

To build a standalone executable with Pex:

```bash
./scripts/build.sh
```

Clean up generated files:

```bash
./scripts/clean.sh
```

## 📁 Output

Downloaded files are stored under the `output_root` directory, sorted by source name and upload date.
When ad scrubbing is enabled and ads are detected, the original downloaded audio is preserved and a new ad-removed file is created:

- `<audio_file>.no_ads.<ext>`

Sidecar files are also written:

- `.<audio_file>.no_ads.<ext>.adscrubbed.json` (hidden cut-range metadata)
- `<audio_file>.no_ads.<ext>.removed_text.txt` (human-readable removed transcript text)

For newly downloaded YouTube/podcast media, subtitles are generated with Whisper when `subtitles: true`:

- `<playback_media>.srt`

VLC auto-loads these subtitles when the `.srt` basename matches the media file in the same folder.

## 📝 Logging

Logs are written to:

```bash
~/youtube/youtube_batch_dl.log
```

And streamed to your terminal.
