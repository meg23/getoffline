import os
from yt_dlp import YoutubeDL
from logger import log
from utils import sanitize, ensure_dir

def download_youtube_items(config, downloaded_items):
    defaults = config["defaults"]
    cookie_path = defaults["cookie_path"]

    for entry in config.get("youtube", []):
        try:
            name = sanitize(entry["name"])
            url = entry["url"]
            folder = os.path.join(defaults["output_root"], name)
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
            else:
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
