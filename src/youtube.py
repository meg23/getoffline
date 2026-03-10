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
            hook_events = 0

            def record_download_progress(d):
                if d.get("status") == "finished":
                    downloaded_items.append(f"YouTube: {name} – {d['info_dict']['title']}")

            def record_postprocess_file(d):
                nonlocal hook_events
                hook_events += 1

                info = d.get("info_dict") or {}
                status = d.get("status")
                postprocessor = d.get("postprocessor") or "unknown"
                candidate = d.get("filepath") or info.get("filepath") or info.get("_filename")
                if not candidate:
                    return

                path = Path(candidate)
                expected_ext = f".{defaults['audio_format']}"

                # For audio extraction hooks, pre-compute the target audio path even if it
                # does not exist yet at hook time.
                if path.suffix.lower() != expected_ext and postprocessor == "FFmpegExtractAudio":
                    path = path.with_suffix(expected_ext)
                elif path.suffix.lower() != expected_ext and path.with_suffix(expected_ext).exists():
                    path = path.with_suffix(expected_ext)

                extracted_audio_files.append(path.resolve())

                log.info(
                    "🪝 yt-dlp hook (%s/%s) for %s: %s",
                    postprocessor,
                    status,
                    name,
                    path.name,
                )

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
            hook_files_all = sorted(set(extracted_audio_files))
            hook_files = [p for p in hook_files_all if p.exists()]
            new_files = sorted(set(delta_files + hook_files))

            log.info(
                "📦 YouTube audio files for %s: before=%d after=%d detected_new=%d hook_events=%d hook_candidates=%d",
                name,
                len(before),
                len(after),
                len(delta_files),
                hook_events,
                len(hook_files_all),
            )

            if hook_files_all:
                log.info(
                    "🎯 yt-dlp hook suggested %d candidate file(s) for %s (%d currently exist)",
                    len(hook_files_all),
                    name,
                    len(hook_files),
                )

            if not new_files:
                recent_files = sorted(
                    [
                        mp3
                        for mp3 in after
                        if mp3.exists() and mp3.stat().st_mtime >= (run_started_at - 60)
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
                all_audio_candidates = sorted(
                    [
                        mp3
                        for mp3 in after
                        if mp3.exists() and not mp3.stem.endswith('.scrubbed')
                    ],
                    key=lambda f: f.stat().st_mtime,
                    reverse=True,
                )
                if all_audio_candidates:
                    max_candidates = max(1, int(defaults.get('max_downloads', 3)))
                    new_files = sorted(all_audio_candidates[:max_candidates])
                    log.warning(
                        "🛟 Broad fallback for %s: forcing scrub attempt on %d recent MP3 file(s)",
                        name,
                        len(new_files),
                    )

            if not new_files:
                log.warning(
                    "⚠️ No candidate MP3 files found for ad scrubbing in %s after download",
                    name,
                )

            for mp3 in new_files:
                log.info("🧼 Starting ad scrub for YouTube file: %s", mp3.name)
                try:
                    scrubbed_output = scrub_audio_file(mp3, scrubber_cfg)
                    if scrubbed_output:
                        log.info("✅ Ad scrubbed YouTube file: %s", scrubbed_output.name)
                        downloaded_items.append(f"Ad scrubbed: YouTube – {scrubbed_output.name}")
                    else:
                        log.info("ℹ️ Ad scrub made no changes for YouTube file: %s", mp3.name)
                except Exception as scrub_exc:
                    log.warning(f"Ad scrub failed for {mp3}: {scrub_exc}")

        except Exception as e:
            log.error(f"❌ Failed to download YouTube: {entry}: {e}")
