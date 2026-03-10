import os
from pathlib import Path

from yt_dlp import YoutubeDL

from ad_scrubber import scrub_audio_file
from logger import log
from utils import ensure_dir, sanitize


def download_youtube_items(config, downloaded_items):
    defaults = config["defaults"]
    cookie_path = defaults["cookie_path"]
    scrubber_cfg = defaults.get("ad_scrubber", {})
    scrubber_enabled = scrubber_cfg.get("enabled", False)

    log.info("🧼 YouTube ad scrubber enabled: %s", scrubber_enabled)

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
                "progress_hooks": [
                    lambda d: downloaded_items.append(
                        f"YouTube: {name} – {d['info_dict']['title']}"
                    )
                    if d["status"] == "finished"
                    else None
                ],
            }

            if download_type == "video":
                ydl_opts["format"] = "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]"
            else:
                ydl_opts.update(
                    {
                        "format": "bestaudio/best",
                        "extract_audio": True,
                        "audio_format": defaults["audio_format"],
                        "audio_quality": str(defaults["audio_quality"]),
                        "postprocessors": [
                            {
                                "key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3",
                                "preferredquality": defaults["audio_quality"],
                            }
                        ],
                    }
                )

            log.info(f"▶️  Downloading YouTube ({download_type}): {name}")
            before = {p.resolve() for p in Path(folder).glob("*.mp3")}
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            if download_type != "audio":
                log.info("⏭️ Ad scrub skipped for %s (type=%s)", name, download_type)
                continue

            if not scrubber_enabled:
                log.info("⏭️ Ad scrub disabled in config for %s", name)
                continue

            if download_type == "audio" and scrubber_enabled:
                after = {p.resolve() for p in Path(folder).glob("*.mp3")}
                new_files = sorted(after - before)

                if not new_files:
                    log.info("ℹ️ No new MP3 files found for ad scrubbing in %s", name)

                for mp3 in new_files:
                    log.info("🧼 Starting ad scrub for YouTube file: %s", mp3.name)
                    try:
                        if scrub_audio_file(mp3, scrubber_cfg):
                            log.info("✅ Ad scrubbed YouTube file: %s", mp3.name)
                            downloaded_items.append(f"Ad scrubbed: YouTube – {mp3.name}")
                        else:
                            log.info("ℹ️ Ad scrub made no changes for YouTube file: %s", mp3.name)
                    except Exception as scrub_exc:
                        log.warning(f"Ad scrub failed for {mp3}: {scrub_exc}")

        except Exception as e:
            log.error(f"❌ Failed to download YouTube: {entry}: {e}")
