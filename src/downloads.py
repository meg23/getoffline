import os
import re
import yaml
import feedparser
import logging
import browser_cookie3
import http.cookiejar
import urllib.error
import urllib.request
import socket
from datetime import datetime
from pathlib import Path

from pywebcopy import save_website
from yt_dlp import YoutubeDL

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
website_defaults = defaults.get("website", {})


def get_website_setting(entry, key, fallback=None):
    """Resolve website download settings with per-entry overrides."""

    if fallback is None:
        fallback = website_defaults.get(key)
    return entry.get(key, website_defaults.get(key, fallback))

cj = browser_cookie3.chrome(domain_name="youtube.com")
cookie_jar = http.cookiejar.MozillaCookieJar(cookie_path)
for cookie in cj:
    print(cookie)
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
            "progress_hooks": [lambda d: downloaded_items.append(f"YouTube: {name} – {d['info_dict']['title']}") if d['status'] == 'finished' else None],
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

        log.info(f"▶️  Downloading YouTube ({download_type}): {name}")
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

for entry in config.get("websites", []):
    try:
        name = sanitize(entry["name"])
        url = entry["url"]
        folder = os.path.join(output_root, name)
        ensure_dir(folder)

        base_date = datetime.now().strftime(get_website_setting(entry, "date_format", "%Y-%m-%d"))
        project_name = base_date
        counter = 1
        while os.path.exists(os.path.join(folder, project_name)):
            project_name = f"{base_date}_{counter:02d}"
            counter += 1

        user_agent = get_website_setting(
            entry,
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        )
        timeout = get_website_setting(entry, "timeout", 60)

        log.info(f"🌐 Downloading website: {name} → {project_name}")
        save_website(
            url=url,
            project_folder=os.path.abspath(folder),
            project_name=project_name,
            bypass_robots=get_website_setting(entry, "bypass_robots", True),
        )

        project_path = Path(folder) / project_name
        has_files = project_path.exists() and any(p.is_file() for p in project_path.rglob("*"))

        if not has_files:
            log.warning(
                "⚠️  Website download produced no files for %s. Attempting fallback fetch...",
                name,
            )
            ensure_dir(project_path)
            try:
                request = urllib.request.Request(url, headers={"User-Agent": user_agent})
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    content = response.read()
                index_path = project_path / "index.html"
                index_path.write_bytes(content)
                has_files = True
                log.info("📝 Saved fallback HTML snapshot for %s", name)
            except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout) as fetch_error:
                log.error(
                    "❌ Fallback fetch failed for %s: %s",
                    name,
                    fetch_error,
                )

        if has_files:
            downloaded_items.append(f"Website: {name} – {project_name}")

    except Exception as e:
        log.error(f"❌ Failed to download website {entry}: {e}")

# Print summary of what was actually downloaded
if downloaded_items:
    print("\n✅ Download Summary:")
    for item in downloaded_items:
        print(f" - {item}")
else:
    print("\n📭 Nothing new was downloaded.")

