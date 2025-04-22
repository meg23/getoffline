# 🎧 Listens: Automated Media Downloader

**Listens** is a Python-based tool to batch download YouTube videos and podcast episodes using a simple YAML configuration. It supports cookies for YouTube downloads and automatic audio extraction using `yt-dlp`.

## 🚀 Features

- Batch download from YouTube playlists and podcast RSS feeds
- Automatic audio extraction to MP3
- Customizable download settings
- Browser cookie support for private videos
- Easy YAML configuration

## 📦 Requirements

- Python 3.8+
- `yt-dlp`
- `feedparser`
- `browser_cookie3`
- `PyYAML`

Install dependencies:

```bash
pip install -r requirements.txt
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

youtube:
  - name: ACG
    url: https://www.youtube.com/playlist?list=...

podcasts:
  - name: TheTimDillonShow
    url: https://audioboom.com/channels/...
```

## 🛠 Usage

Run the downloader:

```bash
python downloads.py
```

To build a standalone executable with Pex:

```bash
./build.sh
```

Clean up generated files:

```bash
./clean.sh
```

## 📁 Output

Downloaded files are stored under the `output_root` directory, sorted by source name and upload date.

## 🔒 Cookie Support

The downloader can use your Chrome browser's cookies to access private or age-restricted YouTube videos. You can also export cookies manually and save them to the path defined in `cookie_path`.

## 📝 Logging

Logs are written to:

```
~/youtube/youtube_batch_dl.log
```

And streamed to your terminal.

---

Feel free to customize `config.yaml` with your favorite channels and podcasts!

