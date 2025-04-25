import os
import feedparser
from yt_dlp import YoutubeDL
from logger import log
from utils import sanitize, ensure_dir

def download_podcasts(config, downloaded_items):
    defaults = config["defaults"]

    for entry in config.get("podcasts", []):
        try:
            name = sanitize(entry["name"])
            url = entry["url"]
            folder = os.path.join(defaults["output_root"], name)
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

