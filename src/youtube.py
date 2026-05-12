import os
import re
import shutil
import subprocess
import sys
import json
import resource
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse


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
from summary_tasks import generate_missing_summaries
from utils import ensure_dir, normalize_media_filename, sanitize, sanitize_channel_name

_EMOJI_RE = re.compile(r"[🇦-🇿🌀-🫿☀-➿️]+")


log = get_logger("youtube")

_YTDLP_REMOTE_COMPONENT = "ejs:github"


def _apply_ffmpeg_audio_filter(media_file: Path, ffmpeg_audio_filter: str) -> bool:
    source = Path(media_file).expanduser().resolve()
    if not source.exists() or not source.is_file():
        return False

    output_file = source.with_name(f"{source.stem}.normalized{source.suffix}")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-af",
        ffmpeg_audio_filter,
        "-vn",
        str(output_file),
    ]

    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        log.warning("Skipping FFmpeg audio filter for %s because ffmpeg is not installed", source.name)
        output_file.unlink(missing_ok=True)
        return False

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        log.warning(
            "Failed to apply FFmpeg audio filter for %s (code=%s): %s",
            source.name,
            completed.returncode,
            stderr or "unknown error",
        )
        output_file.unlink(missing_ok=True)
        return False

    output_file.replace(source)
    log.info("Applied FFmpeg audio filter for %s", source.name)
    return True


def _enable_youtube_ejs_remote_component(ydl_opts: Dict, context_label: str):
    """Enable yt-dlp's YouTube EJS remote component when a JS runtime is available."""
    deno_binary = shutil.which("deno")
    if not deno_binary:
        return

    existing_value = ydl_opts.get("remote_components")
    if isinstance(existing_value, list):
        components = existing_value
    elif isinstance(existing_value, str) and existing_value.strip():
        components = [part.strip() for part in existing_value.split(",") if part.strip()]
    else:
        components = []

    if _YTDLP_REMOTE_COMPONENT in components:
        return

    components.append(_YTDLP_REMOTE_COMPONENT)
    ydl_opts["remote_components"] = components

    log.info(
        "Enabled yt-dlp remote component %s for %s (runtime: %s)",
        _YTDLP_REMOTE_COMPONENT,
        context_label,
        deno_binary,
    )


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




def _extract_youtube_video_id(url: Optional[str]) -> Optional[str]:
    candidate = str(url or "").strip()
    if not candidate:
        return None

    parsed = urlparse(candidate)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""

    if host.endswith("youtu.be"):
        value = path.strip("/")
        return value or None

    if "youtube.com" in host:
        if path.startswith("/watch"):
            query_values = parse_qs(parsed.query or "")
            video_id = str((query_values.get("v") or [""])[0]).strip()
            return video_id or None
        if path.startswith("/shorts/") or path.startswith("/embed/"):
            parts = [segment for segment in path.split("/") if segment]
            if len(parts) >= 2:
                return parts[1].strip() or None

    return None

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
    _enable_youtube_ejs_remote_component(ydl_opts, "source-name resolution")

    with _get_youtubedl()(ydl_opts) as ydl:
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
                return sanitize_channel_name(value)

        title = str(info.get("title") or "").strip()
        if title:
            return sanitize_channel_name(title)

    return "youtube-single"


def search_youtube_videos(query: str, limit: int = 8) -> List[Dict[str, str]]:
    search_query = str(query or "").strip()
    if not search_query:
        return []

    bounded_limit = max(1, min(int(limit), 20))
    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "extract_flat": "discard_in_playlist",
        "logger": _YoutubeDlQuietLogger(),
    }
    _enable_youtube_ejs_remote_component(ydl_opts, "search")

    try:
        with _get_youtubedl()(ydl_opts) as ydl:
            payload = ydl.extract_info(f"ytsearch{bounded_limit}:{search_query}", download=False) or {}
    except Exception as exc:
        log.warning("YouTube search failed for query=%r: %s", search_query, exc)
        return []

    results = []
    for item in payload.get("entries") or []:
        if not isinstance(item, dict):
            continue
        video_url = str(item.get("webpage_url") or item.get("url") or "").strip()
        if not video_url:
            continue
        thumbnails = item.get("thumbnails") or []
        thumbnail_url = ""
        if thumbnails and isinstance(thumbnails, list):
            thumb = thumbnails[0]
            if isinstance(thumb, dict):
                thumbnail_url = str(thumb.get("url") or "").strip()

        results.append(
            {
                "title": str(item.get("title") or "Untitled").strip(),
                "url": video_url,
                "thumbnail": thumbnail_url,
                "channel": str(item.get("channel") or item.get("uploader") or "").strip(),
                "duration": str(item.get("duration_string") or "").strip(),
            }
        )

    return results


class _YoutubeDlQuietLogger:
    def __init__(self, run_stats: Optional[Dict[str, int]] = None):
        self.run_stats = run_stats if run_stats is not None else {}

    def _count(self, key: str):
        self.run_stats[key] = self.run_stats.get(key, 0) + 1

    def _record_message(self, message: str):
        lower = message.lower()
        if "[download] downloading item " in lower:
            self._count("playlist_item_announced")
        if "unavailable" in lower:
            self._count("messages_unavailable")
        if "private" in lower:
            self._count("messages_private")
        if "sign in" in lower or "age-restricted" in lower:
            self._count("messages_auth")

    def debug(self, msg):
        if not msg:
            return
        message = str(msg).strip()
        if not message:
            return

        self._record_message(message)

        if message.startswith("[debug]"):
            log.debug("%s", message)
        else:
            log.info("%s", message)

    def warning(self, msg):
        if msg:
            message = str(msg).strip()
            if message:
                self._record_message(message)
                self._count("warnings")
                log.warning("%s", message)

    def error(self, msg):
        if msg:
            message = str(msg).strip()
            if message:
                self._record_message(message)
                self._count("errors")
                log.error("%s", message)


def _process_media_file(
    media_file: Path,
    name: str,
    entry_subtitles_enabled: bool,
    subtitle_offset_seconds,
    subtitle_transcription_mode: str,
):
    downloaded_summary_items = []

    create_subtitles(
        media_file=media_file,
        subtitle_offset_seconds=subtitle_offset_seconds,
        entry_subtitles_enabled=entry_subtitles_enabled,
        subtitle_transcription_mode=subtitle_transcription_mode,
        logger=log,
        context_name=name,
        context_label="YouTube",
    )

    return downloaded_summary_items


def _resolve_subtitle_worker_count(configured_workers: int) -> int:
    """Serialize Whisper subtitle work to avoid unstable multi-worker crashes."""
    return 1


def _build_youtube_payload(
    *,
    source_name: str,
    source_url: str,
    info: Dict,
    output_file: Optional[str],
    storage_root: str,
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

    item_url = (
        str(info.get("webpage_url") or "").strip()
        or str(info.get("original_url") or "").strip()
        or str(info.get("url") or "").strip()
        or None
    )
    media_url = str(info.get("requested_url") or "").strip() or None
    item_id = str(info.get("id") or "").strip() or None
    if not item_id:
        item_id = _extract_youtube_video_id(item_url) or _extract_youtube_video_id(media_url)
    title = str(info.get("title") or "").strip() or None

    compact_metadata = {
        "id": info.get("id"),
        "title": info.get("title"),
        "webpage_url": info.get("webpage_url"),
        "uploader": info.get("uploader"),
        "channel": info.get("channel"),
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "extractor": info.get("extractor"),
        "extractor_key": info.get("extractor_key"),
        "playlist_id": info.get("playlist_id"),
        "playlist_title": info.get("playlist_title"),
        "format_id": info.get("format_id"),
        "format_note": info.get("format_note"),
        "fps": info.get("fps"),
        "width": info.get("width"),
        "height": info.get("height"),
        "filesize": info.get("filesize"),
        "filesize_approx": info.get("filesize_approx"),
        "acodec": info.get("acodec"),
        "vcodec": info.get("vcodec"),
    }

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
        "storage_root": str(Path(storage_root).expanduser().resolve()),
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
        "raw_metadata": compact_metadata,
    }


def _download_youtube_items_in_process(config, downloaded_items):
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
            name = sanitize_channel_name(entry["name"])
            url = entry["url"]
            folder = os.path.join(defaults["output_root"], name)
            ensure_dir(folder)

            download_type = entry.get("type", "audio").lower()
            entry_subtitles_enabled = entry.get("subtitles", True)
            subtitle_offset_seconds = entry.get("subtitle_offset_seconds")
            should_generate_subtitles = entry_subtitles_enabled
            subtitle_transcription_mode = str(defaults.get("subtitle_transcription_mode", "subprocess"))
            if str(os.getenv("GETOFFLINE_ENABLE_SUBTITLE_EXTRACTION", "1")).strip().lower() not in {"1", "true", "yes", "on"}:
                should_generate_subtitles = False
            is_forced_redownload = bool(entry.get("redownload", False))

            extracted_audio_files: List[Path] = []
            postprocessed_file_by_key: Dict[str, Path] = {}
            normalized_path_map: Dict[Path, Path] = {}
            completed_download_ids = set()
            download_progress_markers = {}
            known_download_titles = {}
            finished_download_info: Dict[str, Dict] = {}
            failed_download_reasons: Dict[str, int] = {}
            candidate_entries_seen_keys = set()
            candidate_entries_allowed_keys = set()
            candidate_entries_allowed_examples: List[str] = []
            progress_status_counts: Dict[str, int] = {}
            ytdlp_message_stats: Dict[str, int] = {}
            skip_reason_counts: Dict[str, int] = {}
            skip_reason_examples: Dict[str, str] = {}
            subtitle_futures_by_media: Dict[Path, object] = {}

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

            def _entry_key(info_dict: dict) -> str:
                return get_download_key(
                    info_dict,
                    str(info_dict.get("url") or info_dict.get("title") or "unknown-entry").strip() or "unknown-entry",
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

            def _record_download_failure(reason: str):
                reason_key = str(reason or "unknown failure").strip() or "unknown failure"
                failed_download_reasons[reason_key] = failed_download_reasons.get(reason_key, 0) + 1

            def _record_skip(reason: str, info_dict: dict):
                reason_key = str(reason or "unknown").strip() or "unknown"
                skip_reason_counts[reason_key] = skip_reason_counts.get(reason_key, 0) + 1

                if reason_key not in skip_reason_examples:
                    title = _clean_log_title(info_dict.get("title"))
                    if title and title != "unknown title":
                        skip_reason_examples[reason_key] = title

            def skip_known_downloads(info_dict, *, incomplete=False):
                _ = incomplete
                entry_key = _entry_key(info_dict)
                candidate_entries_seen_keys.add(entry_key)
                live_reason = skip_live_streams(info_dict, incomplete=incomplete)
                if live_reason:
                    _record_skip(live_reason, info_dict)
                    return live_reason

                webpage_url = str(info_dict.get("webpage_url") or info_dict.get("original_url") or "").strip().lower()
                if info_dict.get("_type") == "url" and info_dict.get("ie_key") == "Youtube" and not webpage_url:
                    webpage_url = str(info_dict.get("url") or "").strip().lower()

                candidate_urls = [
                    webpage_url,
                    str(info_dict.get("original_url") or "").strip().lower(),
                    str(info_dict.get("url") or "").strip().lower(),
                ]
                if any("/shorts/" in candidate for candidate in candidate_urls if candidate):
                    reason = "Skipping YouTube Shorts entry from playlist."
                    _record_skip(reason, info_dict)
                    return reason

                item_url = str(info_dict.get("webpage_url") or info_dict.get("original_url") or "").strip() or None
                media_url = str(info_dict.get("url") or "").strip() or None
                item_id = str(info_dict.get("id") or "").strip() or None
                if not item_id:
                    item_id = _extract_youtube_video_id(item_url) or _extract_youtube_video_id(media_url)
                title = str(info_dict.get("title") or "").strip() or None
                item_uid = build_item_uid(item_id=item_id, item_url=item_url, media_url=media_url, title=title)
                legacy_item_uid = build_item_uid(item_id=None, item_url=item_url, media_url=media_url, title=title)
                if not is_forced_redownload and (
                    is_downloaded(db_path, "youtube", name, item_uid)
                    or (legacy_item_uid != item_uid and is_downloaded(db_path, "youtube", name, legacy_item_uid))
                ):
                    reason = "Skipping already downloaded item in DB"
                    _record_skip(reason, info_dict)
                    return f"{reason}: {_clean_log_title(title)}"
                if not is_forced_redownload and title and has_episode_title_for_source(db_path, "youtube", name, title):
                    reason = "Skipping duplicate title in DB"
                    _record_skip(reason, info_dict)
                    return f"{reason}: {_clean_log_title(title)}"
                candidate_entries_allowed_keys.add(entry_key)
                title = _clean_log_title(info_dict.get("title"))
                if title != "unknown title" and title not in candidate_entries_allowed_examples and len(candidate_entries_allowed_examples) < 3:
                    candidate_entries_allowed_examples.append(title)
                return None

            def record_download_progress(d):
                status = str(d.get("status") or "unknown")
                progress_status_counts[status] = progress_status_counts.get(status, 0) + 1
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

                if status == "error":
                    reason = str(d.get("error") or "yt-dlp reported an item download error")
                    _record_download_failure(reason)
                    log.warning("YouTube item failed for %s: %s", name, reason)

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

                    if subtitle_executor is not None and download_type == "video" and should_generate_subtitles and output_file:
                        candidate_file = Path(output_file).expanduser().resolve()
                        if candidate_file.exists() and candidate_file not in subtitle_futures_by_media:
                            subtitle_futures_by_media[candidate_file] = subtitle_executor.submit(
                                _process_media_file,
                                candidate_file,
                                name,
                                should_generate_subtitles,
                                subtitle_offset_seconds,
                                subtitle_transcription_mode,
                            )

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

                    if subtitle_executor is not None and should_generate_subtitles and resolved_path not in subtitle_futures_by_media:
                        subtitle_futures_by_media[resolved_path] = subtitle_executor.submit(
                            _process_media_file,
                            resolved_path,
                            name,
                            should_generate_subtitles,
                            subtitle_offset_seconds,
                            subtitle_transcription_mode,
                        )
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
                "extractor_args": {
                    "youtube": {
                        "skip": ["shorts"],
                    }
                },
                "progress_hooks": [record_download_progress],
                "postprocessor_hooks": [record_postprocess_file],
                "match_filter": skip_known_downloads,
                "ignoreerrors": True,
                "quiet": True,
                "no_warnings": False,
                "noprogress": True,
                "logger": _YoutubeDlQuietLogger(ytdlp_message_stats),
            }
            if cookie_path:
                ydl_opts["cookiefile"] = cookie_path
            _enable_youtube_ejs_remote_component(ydl_opts, f"download source {name}")

            ffmpeg_audio_filter = str(defaults.get("ffmpeg_audio_filter") or "").strip()

            if download_type == "video":
                ydl_opts["format"] = "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]"
                if ffmpeg_audio_filter:
                    ydl_opts["postprocessors"] = [
                        {
                            "key": "FFmpegVideoConvertor",
                            "preferedformat": "mp4",
                        }
                    ]
                    ydl_opts["postprocessor_args"] = [
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        "-af",
                        ffmpeg_audio_filter,
                    ]
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
                                "preferredcodec": defaults["audio_format"],
                                "preferredquality": defaults["audio_quality"],
                            }
                        ],
                    }
                )

            log.info(f"Downloading YouTube ({download_type}): {name}")
            log.info("YouTube download mode for %s: full entry extraction enabled", name)
            if is_forced_redownload:
                log.info("YouTube download mode for %s: forced redownload (duplicate skip disabled)", name)
            before_audio = {p.resolve() for p in Path(folder).glob("*.mp3")}
            before_video = {p.resolve() for p in Path(folder).glob("*.mp4")}

            configured_subtitle_workers = int(defaults.get("processing_workers", 2))
            configured_subtitle_workers = max(1, configured_subtitle_workers)
            subtitle_worker_count = _resolve_subtitle_worker_count(configured_subtitle_workers)
            if subtitle_worker_count < configured_subtitle_workers:
                log.warning(
                    "Configured %d processing worker(s), but YouTube subtitle generation is limited to %d worker for stability.",
                    configured_subtitle_workers,
                    subtitle_worker_count,
                )

            subtitle_executor = ThreadPoolExecutor(max_workers=subtitle_worker_count)
            with _get_youtubedl()(ydl_opts) as ydl:
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

                if ffmpeg_audio_filter:
                    filtered_audio_files = []
                    for audio_file in new_audio_files:
                        candidate = Path(audio_file).expanduser().resolve()
                        if _apply_ffmpeg_audio_filter(candidate, ffmpeg_audio_filter):
                            filtered_audio_files.append(candidate)
                    if filtered_audio_files:
                        new_audio_files = sorted(set(filtered_audio_files))

            log.info(
                "YouTube files for %s: new_audio=%d new_video=%d postprocess_candidates=%d",
                name,
                len(new_audio_files),
                len(delta_video),
                len(hook_files_all),
            )

            if skip_reason_counts:
                skip_parts = []
                for reason, count in sorted(skip_reason_counts.items(), key=lambda item: item[0].lower()):
                    example = skip_reason_examples.get(reason)
                    if example:
                        skip_parts.append(f"{reason}={count} (e.g., {example})")
                    else:
                        skip_parts.append(f"{reason}={count}")
                log.info("YouTube skips for %s: %s", name, "; ".join(skip_parts))

            if failed_download_reasons:
                failure_parts = [f"{reason}={count}" for reason, count in sorted(failed_download_reasons.items(), key=lambda item: item[0].lower())]
                log.warning("YouTube download errors for %s: %s", name, "; ".join(failure_parts))

            if not completed_download_ids:
                seen_count = len(candidate_entries_seen_keys)
                allowed_count = len(candidate_entries_allowed_keys)
                failed_count = sum(failed_download_reasons.values())
                hook_finished_count = progress_status_counts.get("finished", 0)
                hook_error_count = progress_status_counts.get("error", 0)

                announced_count = ytdlp_message_stats.get("playlist_item_announced", 0)
                log.warning(
                    "No new YouTube media downloaded for %s (playlist_items_seen=%d, allowed_after_filters=%d, skipped_by_filters=%d, failed_downloads=%d, hook_finished_events=%d, hook_error_events=%d, ytdlp_items_announced=%d).",
                    name,
                    seen_count,
                    allowed_count,
                    max(seen_count - allowed_count, 0),
                    failed_count,
                    hook_finished_count,
                    hook_error_count,
                    announced_count,
                )

                if allowed_count > 0 and hook_finished_count == 0 and hook_error_count == 0:
                    example_text = ", ".join(candidate_entries_allowed_examples) if candidate_entries_allowed_examples else "no example titles"
                    log.warning(
                        "YouTube accepted playlist entries for %s but did not emit item download events. Sample allowed entries: %s",
                        name,
                        example_text,
                    )
                if announced_count > 0 and hook_finished_count == 0 and hook_error_count == 0:
                    unavailable_count = ytdlp_message_stats.get("messages_unavailable", 0)
                    private_count = ytdlp_message_stats.get("messages_private", 0)
                    auth_count = ytdlp_message_stats.get("messages_auth", 0)
                    if unavailable_count or private_count or auth_count:
                        log.warning(
                            "yt-dlp announced %d playlist item(s) for %s but produced no file events (unavailable_msgs=%d, private_msgs=%d, auth_msgs=%d).",
                            announced_count,
                            name,
                            unavailable_count,
                            private_count,
                            auth_count,
                        )
                    else:
                        log.warning(
                            "yt-dlp announced %d playlist item(s) for %s but produced no file events. This usually indicates extraction-only behavior or upstream blocking.",
                            announced_count,
                            name,
                        )

            media_files_for_subtitles = new_audio_files if download_type == "audio" else delta_video
            worker_count = subtitle_worker_count
            worker_count = max(1, min(worker_count, len(media_files_for_subtitles) or 1))
            log.info(
                "Running YouTube subtitle processing with %d worker(s) for %s (type=%s)",
                worker_count,
                name,
                download_type,
            )

            for media_file in media_files_for_subtitles:
                candidate = Path(media_file).expanduser().resolve()
                if candidate in subtitle_futures_by_media:
                    continue
                subtitle_futures_by_media[candidate] = subtitle_executor.submit(
                    _process_media_file,
                    candidate,
                    name,
                    should_generate_subtitles,
                    subtitle_offset_seconds,
                    subtitle_transcription_mode,
                )

            for future in as_completed(list(subtitle_futures_by_media.values())):
                try:
                    downloaded_items.extend(future.result())
                except Exception as processing_exc:
                    log.warning("YouTube post-processing failed for %s: %s", name, processing_exc)

            subtitle_executor.shutdown(wait=True)

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
                        storage_root=defaults["output_root"],
                        subtitle_enabled=should_generate_subtitles,
                        download_status="downloaded",
                    ),
                )
                record.clear()
                del info
            finished_download_info.clear()
            generate_missing_summaries(db_path, limit=10)

            cleanup_subtitle_sidecars_for_folder(Path(folder))

        except Exception as e:
            log.error(f"Failed to download YouTube: {entry}: {e}")


def _download_youtube_entry_in_subprocess(payload: Dict) -> Dict:
    config = payload["config"]
    downloaded_items: List[str] = []
    _download_youtube_items_in_process(config, downloaded_items)
    return {"downloaded_items": downloaded_items}


def _parent_rss_mb() -> float:
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return float(rss_kb) / 1024.0


def download_youtube_items(config, downloaded_items):
    if YoutubeDL is not None or str(os.getenv("GETOFFLINE_YTDLP_SUBPROCESS", "1")).strip().lower() in {"0", "false", "no", "off"}:
        _download_youtube_items_in_process(config, downloaded_items)
        return

    base_config = dict(config)
    youtube_entries = list(config.get("youtube", []))
    before_all = _parent_rss_mb()
    for entry in youtube_entries:
        single_config = dict(base_config)
        single_config["youtube"] = [entry]
        payload = {"config": single_config}
        worker_script = Path(__file__).with_name("youtube_subprocess.py")
        proc = subprocess.run(
            [sys.executable, str(worker_script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            log.error("YouTube subprocess failed for %s: %s", entry.get("name"), (proc.stderr or "").strip())
            continue
        try:
            response = json.loads(proc.stdout or "{}")
        except Exception as exc:
            log.error("Invalid YouTube subprocess response for %s: %s", entry.get("name"), exc)
            continue
        downloaded_items.extend(response.get("downloaded_items") or [])
    after_all = _parent_rss_mb()
    log.info("YouTube parent RSS before=%.2fMB after=%.2fMB delta=%.2fMB", before_all, after_all, after_all - before_all)
YoutubeDL = None


def _get_youtubedl():
    global YoutubeDL
    if YoutubeDL is None:
        from yt_dlp import YoutubeDL as _YoutubeDL
        YoutubeDL = _YoutubeDL
    return YoutubeDL
