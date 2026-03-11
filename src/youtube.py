import os
from pathlib import Path
from typing import List

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from ad_scrubber import generate_whisper_subtitles, scrub_audio_file
from logger import log
from utils import ensure_dir, sanitize


def download_youtube_items(config, downloaded_items):
    defaults = config["defaults"]
    cookie_path = defaults["cookie_path"]
    scrubber_cfg = defaults.get("ad_scrubber", {})
    scrubber_enabled = scrubber_cfg.get("enabled", False)

    log.info("🧼 YouTube ad scrubber enabled: %s", scrubber_enabled)

    def skip_live_streams(info_dict, *, incomplete=False):
        _ = incomplete
        live_status = (info_dict.get("live_status") or "").lower()
        if info_dict.get("is_live") or live_status in {"is_live", "is_upcoming", "was_live", "post_live"}:
            title = info_dict.get("title", "unknown title")
            return f"Skipping live stream: {title}"
        return None

    for entry in config.get("youtube", []):
        try:
            name = sanitize(entry["name"])
            url = entry["url"]
            folder = os.path.join(defaults["output_root"], name)
            archive = os.path.join(folder, f"{name}_downloaded.txt")
            ensure_dir(folder)

            download_type = entry.get("type", "audio").lower()
            entry_scrub_enabled = entry.get("scrub", True)
            entry_subtitles_enabled = entry.get("subtitles", False)
            subtitle_offset_seconds = entry.get("subtitle_offset_seconds")

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
                "match_filter": skip_live_streams,
                "ignoreerrors": True,
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
            before_audio = {p.resolve() for p in Path(folder).glob("*.mp3")}
            before_video = {p.resolve() for p in Path(folder).glob("*.mp4")}

            download_warning = None
            with YoutubeDL(ydl_opts) as ydl:
                try:
                    ydl.download([url])
                except DownloadError as exc:
                    message = str(exc)
                    if "Maximum number of downloads reached" in message:
                        download_warning = message
                        log.info(
                            "⏭️ yt-dlp stopped early for %s due to max_downloads; continuing post-processing",
                            name,
                        )
                    else:
                        raise

            if download_warning:
                log.info("ℹ️ yt-dlp notice for %s: %s", name, download_warning)

            after_audio = {p.resolve() for p in Path(folder).glob("*.mp3")}
            after_video = {p.resolve() for p in Path(folder).glob("*.mp4")}

            delta_audio = sorted(after_audio - before_audio)
            delta_video = sorted(after_video - before_video)
            hook_files_all = sorted(set(extracted_audio_files))
            hook_files = [p for p in hook_files_all if p.exists()]
            new_audio_files = sorted(set(delta_audio + hook_files))

            log.info(
                "📦 YouTube files for %s: new_audio=%d new_video=%d hook_events=%d hook_candidates=%d",
                name,
                len(new_audio_files),
                len(delta_video),
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

            playback_files = []
            if download_type == "audio":
                if scrubber_enabled and entry_scrub_enabled:
                    for mp3 in new_audio_files:
                        log.info("🧼 Starting ad scrub for YouTube file: %s", mp3.name)
                        playback_audio = mp3
                        try:
                            scrubbed_output = scrub_audio_file(mp3, scrubber_cfg)
                            if scrubbed_output:
                                playback_audio = scrubbed_output
                                log.info("✅ Ad scrubbed YouTube file: %s", scrubbed_output.name)
                            else:
                                log.info("ℹ️  Ad scrub made no changes for YouTube file: %s", mp3.name)
                        except Exception as scrub_exc:
                            log.warning("Ad scrub failed for %s: %s", mp3, scrub_exc)
                        playback_files.append(playback_audio)
                else:
                    log.info("⏭️ Ad scrub disabled for %s (global=%s entry=%s)", name, scrubber_enabled, entry_scrub_enabled)
                    playback_files.extend(new_audio_files)
            else:
                log.info("⏭️ Ad scrub skipped for %s (type=%s)", name, download_type)
                playback_files.extend(delta_video)

            if entry_subtitles_enabled:
                for media_file in playback_files:
                    if not media_file.exists():
                        continue
                    try:
                        subtitle_settings = dict(scrubber_cfg)
                        if subtitle_offset_seconds is not None:
                            subtitle_settings["subtitle_time_offset_seconds"] = float(subtitle_offset_seconds)
                        subtitle_path = generate_whisper_subtitles(media_file, subtitle_settings)
                        log.info("✅ Generated YouTube subtitles: %s", subtitle_path.name)
                    except Exception as subtitle_exc:
                        log.warning("Subtitle generation failed for %s: %s", media_file, subtitle_exc)

        except Exception as e:
            log.error(f"❌ Failed to download YouTube: {entry}: {e}")
