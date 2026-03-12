import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional

from yt_dlp import YoutubeDL

from logger import get_logger
from scrubbing import scrub_media_file
from subtitles import create_subtitles_and_optional_visualizer
from utils import create_audio_visualizer_video, ensure_dir, normalize_media_filename, sanitize




_EMOJI_RE = re.compile(r"[🇦-🇿🌀-🫿☀-➿️]+")


def _clean_log_title(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown title"

    text = _EMOJI_RE.sub("", text)
    text = " ".join(text.split())
    return text or "unknown title"


def _human_size(num_bytes: Optional[float]) -> str:
    if not num_bytes or num_bytes <= 0:
        return "unknown size"

    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{num_bytes:.0f} B"


class _YoutubeDlQuietLogger:
    def debug(self, msg):
        if not msg:
            return
        message = str(msg).strip()
        if not message:
            return

        # yt-dlp sends normal activity lines to debug(); keep them visible.
        if message.startswith("[debug]"):
            log.debug("%s", message)
        else:
            log.info("%s", message)

    def warning(self, msg):
        if msg:
            log.warning("%s", msg)

    def error(self, msg):
        if msg:
            log.error("%s", msg)


log = get_logger("youtube")


def _process_audio_media_file(
    media_file: Path,
    name: str,
    scrubber_cfg: dict,
    scrubber_enabled: bool,
    entry_scrub_enabled: bool,
    entry_subtitles_enabled: bool,
    entry_visualize_enabled: bool,
    subtitle_offset_seconds,
):
    downloaded_summary_items = []
    playback_audio, skip_subtitles_after_scrub_failure = scrub_media_file(
        media_file=media_file,
        scrubber_cfg=scrubber_cfg,
        scrubber_enabled=scrubber_enabled,
        entry_scrub_enabled=entry_scrub_enabled,
        logger=log,
        context_name=name,
        context_label="YouTube",
    )

    create_subtitles_and_optional_visualizer(
        media_file=playback_audio,
        scrubber_cfg=scrubber_cfg,
        subtitle_offset_seconds=subtitle_offset_seconds,
        entry_subtitles_enabled=entry_subtitles_enabled,
        entry_visualize_enabled=entry_visualize_enabled,
        logger=log,
        context_name=name,
        context_label="YouTube",
        skip_subtitles_after_scrub_failure=skip_subtitles_after_scrub_failure,
    )

    return downloaded_summary_items

def download_youtube_items(config, downloaded_items):
    defaults = config["defaults"]
    cookie_path = defaults["cookie_path"]
    scrubber_cfg = defaults.get("ad_scrubber", {})
    scrubber_enabled = scrubber_cfg.get("enabled", False)

    def skip_live_streams(info_dict, *, incomplete=False):
        _ = incomplete
        live_status = (info_dict.get("live_status") or "").lower()
        if info_dict.get("is_live") or live_status in {"is_live", "is_upcoming", "was_live", "post_live"}:
            title = _clean_log_title(info_dict.get("title"))
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
            entry_visualize_enabled = entry.get("visualize", False)
            subtitle_offset_seconds = entry.get("subtitle_offset_seconds")

            extracted_audio_files: List[Path] = []
            completed_download_ids = set()
            download_progress_markers = {}

            def get_download_key(info: dict, fallback: str) -> str:
                return (
                    str(info.get("id") or "").strip()
                    or str(info.get("webpage_url") or "").strip()
                    or str(info.get("original_url") or "").strip()
                    or fallback
                )

            def record_download_progress(d):
                status = d.get("status")
                info = d.get("info_dict") or {}
                title = info.get("title", "unknown title")
                output_file = d.get("filename") or info.get("_filename") or "unknown file"
                download_key = get_download_key(info, output_file)

                if status == "downloading":
                    downloaded_bytes = d.get("downloaded_bytes") or 0
                    total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate")
                    if total_bytes:
                        pct = int((float(downloaded_bytes) / float(total_bytes)) * 100)
                        step = (pct // 10) * 10
                        previous_step = download_progress_markers.get(download_key, -10)
                        if step >= previous_step + 10:
                            download_progress_markers[download_key] = step
                            log.info(
                                "Download progress for %s: %s%% (%s/%s)",
                                name,
                                min(step, 100),
                                _human_size(downloaded_bytes),
                                _human_size(total_bytes),
                            )
                    elif download_key not in download_progress_markers:
                        download_progress_markers[download_key] = 0
                        log.info("Download progress for %s: %s (size unknown)", name, output_file)

                if status == "finished":
                    total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate")
                    elapsed = d.get("elapsed")

                    if elapsed:
                        log.info(
                            "Finished download for %s: %s (%s in %.1fs)",
                            name,
                            output_file,
                            _human_size(total_bytes),
                            float(elapsed),
                        )
                    else:
                        log.info(
                            "Finished download for %s: %s (%s)",
                            name,
                            output_file,
                            _human_size(total_bytes),
                        )

                    if download_key not in completed_download_ids:
                        completed_download_ids.add(download_key)
                        downloaded_items.append(f"YouTube: {name} – {title}")

            def record_postprocess_file(d):
                info = d.get("info_dict") or {}
                postprocessor = d.get("postprocessor") or "unknown"
                pp_status = d.get("status")
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
                if pp_status:
                    log.info(
                        "Post-process %s for %s via %s: %s",
                        pp_status,
                        name,
                        postprocessor,
                        path.name,
                    )
                else:
                    log.info(
                        "Post-processed for %s via %s: %s",
                        name,
                        postprocessor,
                        path.name,
                    )


            ydl_opts = {
                "cookiefile": cookie_path,
                "playlistend": defaults["playlist_end"],
                "restrictfilenames": True,
                "outtmpl_na_placeholder": "NA",
                "download_archive": archive,
                "outtmpl": f"{folder}/%(upload_date)s-%(title)s.%(ext)s",
                "progress_hooks": [record_download_progress],
                "postprocessor_hooks": [record_postprocess_file],
                "match_filter": skip_live_streams,
                "ignoreerrors": True,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "logger": _YoutubeDlQuietLogger(),
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

            log.info(f"Downloading YouTube ({download_type}): {name}")
            log.info("Archive for %s: %s", name, archive)
            before_audio = {p.resolve() for p in Path(folder).glob("*.mp3")}
            before_video = {p.resolve() for p in Path(folder).glob("*.mp4")}

            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            after_audio = {p.resolve() for p in Path(folder).glob("*.mp3")}
            after_video = {p.resolve() for p in Path(folder).glob("*.mp4")}

            delta_audio = sorted(after_audio - before_audio)
            delta_video = sorted(after_video - before_video)
            hook_files_all = sorted(set(extracted_audio_files))
            hook_files = [p for p in hook_files_all if p.exists()]
            new_audio_files = sorted(set(delta_audio + hook_files))

            if download_type == "audio":
                normalized_audio_files = []
                for audio_path in new_audio_files:
                    if not audio_path.exists():
                        continue
                    normalized_audio = normalize_media_filename(audio_path)
                    if normalized_audio != audio_path:
                        log.info("Normalized YouTube filename: %s -> %s", audio_path.name, normalized_audio.name)
                    normalized_audio_files.append(normalized_audio)
                new_audio_files = sorted(set(normalized_audio_files))

            log.info(
                "YouTube files for %s: new_audio=%d new_video=%d postprocess_candidates=%d",
                name,
                len(new_audio_files),
                len(delta_video),
                len(hook_files_all),
            )

            if download_type == "audio":
                worker_count = int(defaults.get("processing_workers", 2))
                worker_count = max(1, min(worker_count, len(new_audio_files) or 1))
                log.info("Running YouTube post-processing with %d worker(s) for %s", worker_count, name)

                if worker_count == 1:
                    for mp3 in new_audio_files:
                        downloaded_items.extend(
                            _process_audio_media_file(
                                mp3,
                                name,
                                scrubber_cfg,
                                scrubber_enabled,
                                entry_scrub_enabled,
                                entry_subtitles_enabled,
                                entry_visualize_enabled,
                                subtitle_offset_seconds,
                            )
                        )
                else:
                    with ThreadPoolExecutor(max_workers=worker_count) as executor:
                        futures = [
                            executor.submit(
                                _process_audio_media_file,
                                mp3,
                                name,
                                scrubber_cfg,
                                scrubber_enabled,
                                entry_scrub_enabled,
                                entry_subtitles_enabled,
                                entry_visualize_enabled,
                                subtitle_offset_seconds,
                            )
                            for mp3 in new_audio_files
                        ]
                        for future in as_completed(futures):
                            try:
                                downloaded_items.extend(future.result())
                            except Exception as processing_exc:
                                log.warning("YouTube post-processing failed for %s: %s", name, processing_exc)
            else:
                if entry_visualize_enabled and not entry_subtitles_enabled:
                    log.info("Visualizer skipped for %s because subtitles are disabled", name)
                elif entry_subtitles_enabled:
                    for media_file in delta_video:
                        if not media_file.exists():
                            continue
                        create_subtitles_and_optional_visualizer(
                            media_file=media_file,
                            scrubber_cfg=scrubber_cfg,
                            subtitle_offset_seconds=subtitle_offset_seconds,
                            entry_subtitles_enabled=entry_subtitles_enabled,
                            entry_visualize_enabled=False,
                            logger=log,
                            context_name=name,
                            context_label="YouTube",
                        )
                else:
                    log.info("Ad scrub skipped for %s (type=%s)", name, download_type)

        except Exception as e:
            log.error(f"Failed to download YouTube: {entry}: {e}")
