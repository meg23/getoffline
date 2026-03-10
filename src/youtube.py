import os
import time
from pathlib import Path
from typing import List

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

            extracted_audio_files: List[Path] = []

            def record_download_progress(d):
                if d.get("status") == "finished":
                    downloaded_items.append(f"YouTube: {name} – {d['info_dict']['title']}")

            def record_postprocess_file(d):
                if d.get("status") != "finished":
                    return
                info = d.get("info_dict") or {}
                candidate = d.get("filepath") or info.get("filepath") or info.get("_filename")
                if not candidate:
                    return

                path = Path(candidate)
                expected_ext = f".{defaults['audio_format']}"
                if path.suffix.lower() != expected_ext and path.with_suffix(expected_ext).exists():
                    path = path.with_suffix(expected_ext)

                if path.suffix.lower() == expected_ext and path.exists():
                    extracted_audio_files.append(path.resolve())

            ydl_opts = {
                "cookiefile": cookie_path,
                "max_downloads": defaults["max_downloads"],
                "playlistend": defaults["playlist_end"],
                "restrictfilenames": True,
                "outtmpl_na_placeholder": "NA",
                "download_archive": archive,
                "outtmpl": f"{folder}/%(upload_date)s-%(title)s.%(ext)s",
                "progress_hooks": [record_download_progress],
                "postprocessor_hooks": [record_postprocess_file],
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
            run_started_at = time.time()
            before = {p.resolve() for p in Path(folder).glob("*.mp3")}
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            if download_type != "audio":
                log.info("⏭️ Ad scrub skipped for %s (type=%s)", name, download_type)
                continue

            if not scrubber_enabled:
                log.info("⏭️ Ad scrub disabled in config for %s", name)
                continue

            after = {p.resolve() for p in Path(folder).glob("*.mp3")}
            delta_files = sorted(after - before)
            hook_files = sorted(set(extracted_audio_files))
            new_files = sorted(set(delta_files + hook_files))

            log.info(
                "📦 YouTube audio files for %s: before=%d after=%d detected_new=%d hook_detected=%d",
                name,
                len(before),
                len(after),
                len(delta_files),
                len(hook_files),
            )

            if hook_files:
                log.info(
                    "🎯 yt-dlp postprocessor reported %d audio file(s) for %s",
                    len(hook_files),
                    name,
                )

            if not new_files:
                recent_files = sorted(
                    [
                        mp3
                        for mp3 in after
                        if mp3.exists() and mp3.stat().st_mtime >= (run_started_at - 10)
                    ]
                )
                if recent_files:
                    log.info(
                        "🛟 Using recent-file fallback for %s: %d MP3 file(s) modified during this run",
                        name,
                        len(recent_files),
                    )
                    new_files = recent_files

            if not new_files:
                log.warning(
                    "⚠️ No candidate MP3 files found for ad scrubbing in %s after download",
                    name,
                )

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
