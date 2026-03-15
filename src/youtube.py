import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional

from yt_dlp import YoutubeDL

from database import (
    build_item_uid,
    ensure_config_seeded,
    get_stored_config,
    init_database,
    is_downloaded,
    materialize_youtube_cookie_file,
    resolve_database_path,
    upsert_download,
    has_episode_title_for_source,
)
from logger import get_logger
from subtitles import cleanup_subtitle_sidecars_for_folder, create_subtitles
from utils import ensure_dir, normalize_media_filename, sanitize

_EMOJI_RE = re.compile(r"[🇦-🇿🌀-🫿☀-➿️]+")


log = get_logger("youtube")


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


def resolve_youtube_source_name(url: str, cookie_file: Optional[str] = None) -> str:
    source_url = str(url or "").strip()
    if not source_url:
        raise ValueError("Missing YouTube URL")

    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _YoutubeDlQuietLogger(),
    }
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(source_url, download=False)

    if info and isinstance(info, dict):
        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            for entry in entries:
                if isinstance(entry, dict):
                    info = entry
                    break

        for key in ("channel", "uploader", "uploader_id"):
            value = str(info.get(key) or "").strip()
            if value:
                return sanitize(value)

        title = str(info.get("title") or "").strip()
        if title:
            return sanitize(title)

    return "youtube-single"


class _YoutubeDlQuietLogger:
    def debug(self, msg):
        if not msg:
            return
        message = str(msg).strip()
        if not message:
            return

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


def _process_audio_media_file(
    media_file: Path,
    name: str,
    entry_subtitles_enabled: bool,
    subtitle_offset_seconds,
):
    downloaded_summary_items = []

    create_subtitles(
        media_file=media_file,
        subtitle_offset_seconds=subtitle_offset_seconds,
        entry_subtitles_enabled=entry_subtitles_enabled,
        logger=log,
        context_name=name,
        context_label="YouTube",
    )

    return downloaded_summary_items


def _build_youtube_payload(
    *,
    source_name: str,
    source_url: str,
    info: Dict,
    output_file: Optional[str],
    subtitle_enabled: bool,
    download_status: str,
    error_message: Optional[str] = None,
) -> Dict:
    path = Path(output_file).expanduser().resolve() if output_file else None
    file_size = path.stat().st_size if path and path.exists() else None
    resolution = None
    width, height = info.get("width"), info.get("height")
    if width and height:
        resolution = f"{width}x{height}"

    item_id = str(info.get("id") or "").strip() or None
    item_url = (
        str(info.get("webpage_url") or "").strip()
        or str(info.get("original_url") or "").strip()
        or str(info.get("url") or "").strip()
        or None
    )
    media_url = str(info.get("requested_url") or "").strip() or None
    title = str(info.get("title") or "").strip() or None

    return {
        "source_type": "youtube",
        "source_name": source_name,
        "source_url": source_url,
        "item_uid": build_item_uid(item_id=item_id, item_url=item_url, media_url=media_url, title=title),
        "item_id": item_id,
        "item_url": item_url,
        "media_url": media_url,
        "title": title,
        "description": info.get("description"),
        "uploader": info.get("uploader"),
        "channel": info.get("channel") or info.get("uploader"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "playlist_id": info.get("playlist_id"),
        "playlist_title": info.get("playlist_title"),
        "upload_date": info.get("upload_date"),
        "duration_seconds": info.get("duration"),
        "file_path": str(path) if path else None,
        "file_ext": path.suffix.lstrip(".") if path else info.get("ext"),
        "file_size_bytes": file_size,
        "expected_bytes": info.get("filesize") or info.get("filesize_approx"),
        "format_id": info.get("format_id"),
        "format_note": info.get("format_note"),
        "audio_codec": info.get("acodec"),
        "video_codec": info.get("vcodec"),
        "resolution": resolution,
        "fps": info.get("fps"),
        "subtitle_enabled": subtitle_enabled,
        "subtitle_path": None,
        "download_status": download_status,
        "error_message": error_message,
        "raw_metadata": info,
    }


def download_youtube_items(config, downloaded_items):
    defaults = config["defaults"]
    db_path = defaults.get("database_path") or resolve_database_path(defaults)
    init_database(db_path)
    ensure_config_seeded(db_path, defaults)
    stored_config = get_stored_config(db_path)
    defaults = stored_config["defaults"]
    config["defaults"] = defaults
    config["download_settings"] = stored_config["download_settings"]
    cookie_path = materialize_youtube_cookie_file(db_path)

    def skip_live_streams(info_dict, *, incomplete=False):
        _ = incomplete
        live_status = (info_dict.get("live_status") or "").lower()
        if info_dict.get("is_live") or live_status in {"is_live", "is_upcoming", "was_live", "post_live"}:
            title = _clean_log_title(info_dict.get("title"))
            return f"Skipping live stream: {title}"
        return None

    for entry in config.get("youtube", []):
        if not entry.get("enabled", True):
            continue
        try:
            name = sanitize(entry["name"])
            url = entry["url"]
            folder = os.path.join(defaults["output_root"], name)
            ensure_dir(folder)

            download_type = entry.get("type", "audio").lower()
            entry_subtitles_enabled = entry.get("subtitles", True)
            subtitle_offset_seconds = entry.get("subtitle_offset_seconds")
            should_generate_subtitles = entry_subtitles_enabled and download_type == "audio"

            extracted_audio_files: List[Path] = []
            postprocessed_file_by_key: Dict[str, Path] = {}
            normalized_path_map: Dict[Path, Path] = {}
            completed_download_ids = set()
            download_progress_markers = {}
            known_download_titles = {}
            finished_download_info: Dict[str, Dict] = {}

            subtitle_or_aux_exts = {
                ".srt", ".vtt", ".ass", ".ssa", ".lrc", ".ttml", ".srv1", ".srv2", ".srv3", ".json3"
            }

            def _normalized_stem(value: str) -> str:
                normalized = re.sub(r"\.{2,}", ".", str(value or "")).rstrip(". ")
                return normalized or "item"

            def _resolve_postprocessed_audio_path(candidate_path: Path) -> Path:
                expected_ext = f".{defaults['audio_format']}"
                if candidate_path.suffix.lower() == expected_ext and candidate_path.exists():
                    return candidate_path

                converted = candidate_path.with_suffix(expected_ext)
                if converted.exists():
                    return converted

                target_stem = _normalized_stem(candidate_path.stem)
                siblings = sorted(candidate_path.parent.glob(f"*{expected_ext}"), key=lambda p: p.stat().st_mtime, reverse=True)
                for sibling in siblings:
                    if _normalized_stem(sibling.stem) == target_stem:
                        return sibling

                return converted

            def get_download_key(info: dict, fallback: str) -> str:
                return (
                    str(info.get("id") or "").strip()
                    or str(info.get("webpage_url") or "").strip()
                    or str(info.get("original_url") or "").strip()
                    or fallback
                )

            def _is_subtitle_or_aux_download(output_file: str) -> bool:
                path = Path(str(output_file or ""))
                return path.suffix.lower() in subtitle_or_aux_exts

            def _get_best_title(info: dict, output_file: str, download_key: str) -> str:
                title = str(info.get("title") or "").strip()
                if title:
                    known_download_titles[download_key] = title
                    return title

                known_title = known_download_titles.get(download_key)
                if known_title:
                    return known_title

                file_stem = Path(str(output_file or "")).stem.strip()
                return file_stem or "unknown title"

            def skip_known_downloads(info_dict, *, incomplete=False):
                _ = incomplete
                live_reason = skip_live_streams(info_dict, incomplete=incomplete)
                if live_reason:
                    return live_reason

                webpage_url = str(info_dict.get("webpage_url") or info_dict.get("original_url") or "").strip().lower()
                if info_dict.get("_type") == "url" and info_dict.get("ie_key") == "Youtube" and not webpage_url:
                    webpage_url = str(info_dict.get("url") or "").strip().lower()
                if "/shorts/" in webpage_url:
                    return "Skipping YouTube Shorts entry from playlist."

                item_id = str(info_dict.get("id") or "").strip() or None
                item_url = str(info_dict.get("webpage_url") or info_dict.get("original_url") or "").strip() or None
                media_url = str(info_dict.get("url") or "").strip() or None
                title = str(info_dict.get("title") or "").strip() or None
                item_uid = build_item_uid(item_id=item_id, item_url=item_url, media_url=media_url, title=title)
                if is_downloaded(db_path, "youtube", name, item_uid):
                    return f"Skipping already downloaded item in DB: {_clean_log_title(title)}"
                if title and has_episode_title_for_source(db_path, "youtube", name, title):
                    return f"Skipping duplicate title in DB: {_clean_log_title(title)}"
                return None

            def record_download_progress(d):
                status = d.get("status")
                info = d.get("info_dict") or {}
                output_file = d.get("filename") or info.get("_filename") or "unknown file"
                download_key = get_download_key(info, output_file)
                title = _get_best_title(info, output_file, download_key)

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

                    if _is_subtitle_or_aux_download(output_file):
                        return

                    finished_download_info[download_key] = {
                        "info": info,
                        "output_file": output_file,
                    }

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

                download_key = get_download_key(info, str(candidate))

                path = Path(candidate)
                expected_ext = f".{defaults['audio_format']}"

                if download_type == "audio":
                    if path.suffix.lower() != expected_ext and postprocessor == "FFmpegExtractAudio":
                        path = _resolve_postprocessed_audio_path(path)
                    elif path.suffix.lower() != expected_ext and path.with_suffix(expected_ext).exists():
                        path = path.with_suffix(expected_ext)

                    resolved_path = path.resolve()
                    extracted_audio_files.append(resolved_path)
                    postprocessed_file_by_key[download_key] = resolved_path
                else:
                    resolved_path = path.resolve()
                    postprocessed_file_by_key[download_key] = resolved_path

                if pp_status:
                    log.info("Post-process %s for %s via %s: %s", pp_status, name, postprocessor, path.name)
                else:
                    log.info("Post-processed for %s via %s: %s", name, postprocessor, path.name)

            ydl_opts = {
                "playlistend": defaults["playlist_end"],
                "restrictfilenames": True,
                "outtmpl_na_placeholder": "NA",
                "outtmpl": f"{folder}/%(upload_date)s-%(title)s.%(ext)s",
                "progress_hooks": [record_download_progress],
                "postprocessor_hooks": [record_postprocess_file],
                "match_filter": skip_known_downloads,
                "ignoreerrors": True,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "logger": _YoutubeDlQuietLogger(),
                "extract_flat": "in_playlist",
            }
            if cookie_path:
                ydl_opts["cookiefile"] = cookie_path

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
                    original_audio = audio_path.resolve()
                    normalized_audio = normalize_media_filename(audio_path)
                    normalized_path_map[original_audio] = normalized_audio.resolve()
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
                                should_generate_subtitles,
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
                                should_generate_subtitles,
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
                log.info("Subtitles skipped for %s (type=%s)", name, download_type)

            for download_key, record in finished_download_info.items():
                info = record["info"]
                out_path = record["output_file"]
                resolved_file = out_path
                out_candidate = Path(out_path)

                postprocessed_path = postprocessed_file_by_key.get(download_key)
                if postprocessed_path and postprocessed_path.exists():
                    resolved_file = str(postprocessed_path)
                elif download_type == "audio":
                    audio_candidate = _resolve_postprocessed_audio_path(out_candidate)
                    if audio_candidate.exists():
                        resolved_file = str(audio_candidate)

                resolved_path = Path(resolved_file).expanduser().resolve()
                remapped_path = normalized_path_map.get(resolved_path)
                if remapped_path is not None:
                    resolved_file = str(remapped_path)

                upsert_download(
                    db_path,
                    _build_youtube_payload(
                        source_name=name,
                        source_url=url,
                        info=info,
                        output_file=resolved_file,
                        subtitle_enabled=should_generate_subtitles,
                        download_status="downloaded",
                    ),
                )

            cleanup_subtitle_sidecars_for_folder(Path(folder))

        except Exception as e:
            log.error(f"Failed to download YouTube: {entry}: {e}")
