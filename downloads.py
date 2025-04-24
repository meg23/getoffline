import os
import re
import yaml
import feedparser
import logging
import browser_cookie3
import http.cookiejar
from yt_dlp import YoutubeDL
from pathlib import Path

log_path = os.path.expanduser("~/youtube/youtube_batch_dl.log")
os.makedirs(os.path.dirname(log_path), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_path, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

def sanitize(name):
    return re.sub(r"[^\w.-]", "_", name)

def ensure_dir(path):
    Path(path).expanduser().mkdir(parents=True, exist_ok=True)

with open("config.yaml") as f:
    config = yaml.safe_load(f)

defaults = config["defaults"]
output_root = os.path.expanduser(defaults["output_root"])
cookie_path = os.path.expanduser(defaults["cookie_path"])

cj = browser_cookie3.chrome(domain_name="youtube.com")
cookie_jar = http.cookiejar.MozillaCookieJar(cookie_path)
for cookie in cj:
    cookie_jar.set_cookie(cookie)
cookie_jar.save(ignore_discard=True, ignore_expires=True)

downloaded_items = []

for entry in config.get("youtube", []):
    try:
        name = sanitize(entry["name"])
        url = entry["url"]
        folder = os.path.join(output_root, name)
        archive = os.path.join(folder, f"{name}_downloaded.txt")
        ensure_dir(folder)

        download_type = entry.get("type", "audio").lower()

        ydl_opts = {
            "cookiefile": cookie_path,
            "max_downloads": defaults["max_downloads"],
            "playlistend": defaults["playlist_end"],
            "restrictfilenames": True,
            "outtmpl_na_placeholder": "NA",
            "download_archive": archive,
            "outtmpl": f"{folder}/%(upload_date)s-%(title)s.%(ext)s",
            "progress_hooks": [lambda d: downloaded_items.append(f"YouTube: {name} – {d['info_dict']['title']}") if d['status'] == 'finished' else None]
        }

        if download_type == "video":
            ydl_opts["format"] = "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]"
        else:  # audio
            ydl_opts.update({
                "format": "bestaudio/best",
                "extract_audio": True,
                "audio_format": defaults["audio_format"],
                "audio_quality": str(defaults["audio_quality"]),
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': defaults['audio_quality']
                }],
            })

        log.info(f"▶️ Downloading YouTube ({download_type}): {name}")
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    except Exception as e:
        log.error(f"❌ Failed to download YouTube: {entry}: {e}")

for entry in config.get("podcasts", []):
    try:
        name = sanitize(entry["name"])
        url = entry["url"]
        folder = os.path.join(output_root, name)
        archive = os.path.join(folder, f"{name}_downloaded.txt")
        ensure_dir(folder)

        downloaded = set()
        if os.path.exists(archive):
            with open(archive) as f:
                downloaded = set(line.strip() for line in f)

        feed = feedparser.parse(url)
        entries = feed.entries[:defaults["max_downloads"]]

        for ep in entries:
            if not ep.enclosures:
                continue
            mp3_url = ep.enclosures[0].href
            if mp3_url in downloaded:
                continue
            ep_title = sanitize(ep.title)
            out_path = f"{folder}/{ep_title}.%(ext)s"

            ydl_opts = {
                "extract_audio": True,
                "audio_format": defaults["audio_format"],
                "audio_quality": str(defaults["audio_quality"]),
                "restrictfilenames": True,
                "outtmpl_na_placeholder": "NA",
                "outtmpl": out_path,
            }

            log.info(f"🎙️ Downloading podcast: {name} – {ep_title}")
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([mp3_url])

            with open(archive, "a") as f:
                f.write(mp3_url + "\n")

            downloaded_items.append(f"Podcast: {name} – {ep_title}")

    except Exception as e:
        log.error(f"❌ Failed to process podcast {entry}: {e}")

# Print summary of what was actually downloaded
if downloaded_items:
    print("\n✅ Download Summary:")
    for item in downloaded_items:
        print(f" - {item}")
else:
    print("\n📭 Nothing new was downloaded.")

