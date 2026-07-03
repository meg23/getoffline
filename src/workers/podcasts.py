import importlib
import os
import resource
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from pathlib import Path

import feedparser

from workers.content_filter import delete_media_artifacts
from workers.content_filter import log_filtered_deletion
from workers.content_filter import screen_transcript
from workers.download_store import build_item_uid
from workers.download_store import ensure_config_seeded
from workers.download_store import get_stored_config
from workers.download_store import init_database
from workers.download_store import is_downloaded
from workers.download_store import resolve_database_path
from workers.download_store import upsert_download
from workers.logger import get_logger
from workers.subtitles import cleanup_subtitle_sidecars_for_folder
from workers.subtitles import create_subtitles
from workers.utils import ensure_dir
from workers.utils import sanitize
from workers.utils import sanitize_channel_name
from workers.utils import split_title_filter_terms
from workers.utils import title_matches_filter

PODCAST_DOWNLOAD_RETRIES = 3
YoutubeDL = None


class _YoutubeDlQuietLogger:
    def debug(self, msg):
        _ = msg

    def warning(self, msg):
        _ = msg

    def error(self, msg):
        if msg:
            log.error("%s", msg)


log = get_logger("podcast")


def _http_retry_backoff(retry_count: int) -> int:
    return min(2 ** int(retry_count), 10)


def _coerce_positive_int(value: object, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(fallback)
    return max(1, parsed)


def _resolve_scan_limit(entry: dict, defaults: dict, source_max_downloads: int) -> int:
    """Return how many recent RSS entries to inspect for new episodes."""
    configured = (
        entry.get("scan_limit")
        or entry.get("podcast_scan_limit")
        or defaults.get("scan_limit")
        or defaults.get("podcast_scan_limit")
    )
    if configured:
        return max(
            source_max_downloads, _coerce_positive_int(configured, source_max_downloads)
        )
    return max(source_max_downloads, source_max_downloads * 10, 50)


def _download_episode_media(episode_job: dict):
    ydl_opts = episode_job["ydl_opts"]
    mp3_url = episode_job["mp3_url"]
    name = episode_job["name"]
    episode_title = episode_job["episode_title"]

    log.info(f"Downloading podcast: {name} – {episode_title}")
    last_download_error = None
    for attempt in range(1, PODCAST_DOWNLOAD_RETRIES + 1):
        try:
            with _get_youtubedl()(ydl_opts) as ydl:
                ydl.download([mp3_url])
            last_download_error = None
            break
        except Exception as download_exc:
            last_download_error = download_exc
            if attempt < PODCAST_DOWNLOAD_RETRIES:
                backoff_seconds = attempt * 2
                log.warning(
                    "Retrying podcast download (%s/%s) for %s – %s in %ss: %s",
                    attempt,
                    PODCAST_DOWNLOAD_RETRIES,
                    name,
                    episode_title,
                    backoff_seconds,
                    download_exc,
                )
                time.sleep(backoff_seconds)

    return last_download_error


def _episode_payload(
    *,
    db_path,
    source_name,
    source_url,
    storage_root,
    media_url,
    title,
    description,
    file_path,
    subtitle_enabled,
    subtitle_path,
    download_status,
    error_message=None,
    artwork_url=None,
):
    file_value = Path(file_path).expanduser().resolve() if file_path else None
    file_size = (
        file_value.stat().st_size if file_value and file_value.exists() else None
    )

    return {
        "source_type": "podcast",
        "source_name": source_name,
        "source_url": source_url,
        "item_uid": build_item_uid(
            item_id=None, item_url=media_url, media_url=media_url, title=title
        ),
        "item_id": None,
        "item_url": media_url,
        "media_url": media_url,
        "title": title,
        "description": description,
        "uploader": source_name,
        "channel": source_name,
        "extractor": "podcast-rss",
        "playlist_id": None,
        "playlist_title": source_name,
        "upload_date": None,
        "duration_seconds": None,
        "file_path": str(file_value) if file_value else None,
        "storage_root": str(Path(storage_root).expanduser().resolve()),
        "file_ext": file_value.suffix.lstrip(".") if file_value else None,
        "file_size_bytes": file_size,
        "expected_bytes": None,
        "format_id": None,
        "format_note": None,
        "audio_codec": None,
        "video_codec": "none",
        "resolution": "audio-only",
        "fps": None,
        "subtitle_enabled": subtitle_enabled,
        "subtitle_path": (
            str(Path(subtitle_path).expanduser().resolve()) if subtitle_path else None
        ),
        "download_status": download_status,
        "error_message": error_message,
        "raw_metadata": {
            "feed_url": source_url,
            "media_url": media_url,
            "title": title,
            "description": description,
            "artwork_url": artwork_url,
            "image_url": artwork_url,
        },
    }


def _delete_original_media_candidates(
    final_path: Path, expected_suffix: str
) -> list[Path]:
    final_path = Path(final_path).expanduser().resolve()
    expected_suffix = expected_suffix.lower()
    deleted = []
    if not final_path.exists() or final_path.suffix.lower() != expected_suffix:
        return deleted
    for candidate in final_path.parent.glob(f"{final_path.stem}.*"):
        candidate = candidate.expanduser().resolve()
        if candidate == final_path or candidate.suffix.lower() == expected_suffix:
            continue
        if candidate.suffix.lower() in {
            ".srt",
            ".vtt",
            ".ass",
            ".ssa",
            ".lrc",
            ".ttml",
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
        }:
            continue
        if candidate.is_file():
            candidate.unlink(missing_ok=True)
            deleted.append(candidate)
    return deleted


def _converted_media_ready(final_path: Path, expected_suffix: str) -> bool:
    final_path = Path(final_path).expanduser().resolve()
    return (
        final_path.exists()
        and final_path.is_file()
        and final_path.suffix.lower() == expected_suffix.lower()
    )


def _image_href(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("href") or value.get("url") or "").strip()
    href = getattr(value, "href", None)
    if href:
        return str(href).strip()
    url = getattr(value, "url", None)
    if url:
        return str(url).strip()
    return ""


def _image_dimension(value: object, key: str) -> int:
    raw_value = None
    if isinstance(value, dict):
        raw_value = value.get(key)
    else:
        raw_value = getattr(value, key, None)
    try:
        return max(0, int(float(str(raw_value or "0").strip())))
    except (TypeError, ValueError):
        return 0


def _append_artwork_candidate(
    candidates: list, value: object, source_priority: int = 0
) -> None:
    image_url = _image_href(value)
    if not image_url:
        return
    width = _image_dimension(value, "width")
    height = _image_dimension(value, "height")
    candidates.append(
        {
            "url": image_url,
            "width": width,
            "height": height,
            "source_priority": source_priority,
        }
    )


def _candidate_values(container: object, key: str) -> list:
    value = None
    if isinstance(container, dict):
        value = container.get(key)
    elif container is not None:
        value = getattr(container, key, None)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _append_podcast_artwork_candidates(candidates: list, value: object) -> None:
    if value is None:
        return
    for key in ("image", "itunes_image"):
        for image_value in _candidate_values(value, key):
            _append_artwork_candidate(candidates, image_value, source_priority=2)
    for key in ("media_thumbnail", "media_thumbnail_detail", "media_content"):
        for image_value in _candidate_values(value, key):
            _append_artwork_candidate(candidates, image_value, source_priority=1)


def _artwork_quality_score(candidate: dict) -> tuple:
    width = int(candidate.get("width") or 0)
    height = int(candidate.get("height") or 0)
    url = str(candidate.get("url") or "")
    area = width * height
    source_priority = int(candidate.get("source_priority") or 0)
    if area <= 0:
        area = max(width, height)
    if area <= 0:
        area = source_priority * 1_000_000
    format_bonus = 0
    lower_url = url.lower()
    if ".jpg" in lower_url or ".jpeg" in lower_url or ".png" in lower_url:
        format_bonus = 1
    return (area, source_priority, max(width, height), format_bonus, len(url))


def _podcast_artwork_url(feed: object, entry: object) -> str:
    candidates = []
    _append_podcast_artwork_candidates(candidates, entry)
    _append_podcast_artwork_candidates(candidates, getattr(feed, "feed", None))
    if not candidates:
        return ""
    best_candidate = max(candidates, key=_artwork_quality_score)
    return str(best_candidate.get("url") or "").strip()


def _entry_title(entry: object) -> str:
    if isinstance(entry, dict):
        return str(entry.get("title") or "").strip()
    try:
        return str(entry.title or "").strip()
    except AttributeError:
        return ""


def _entry_summary(entry: object):
    if isinstance(entry, dict):
        return entry.get("summary")
    try:
        return entry.summary
    except AttributeError:
        return None


def _download_podcasts_in_process(config, downloaded_items):
    defaults = config["defaults"]
    db_path = defaults.get("database_path") or resolve_database_path(defaults)
    init_database(db_path)
    ensure_config_seeded(db_path, defaults)
    stored_config = get_stored_config(db_path)
    defaults = stored_config["defaults"]
    config["defaults"] = defaults

    for entry in config.get("podcasts", []):
        if not entry.get("enabled", True):
            continue
        try:
            name = sanitize_channel_name(entry["name"])
            url = entry["url"]
            entry_subtitles_enabled = entry.get("subtitles", True)
            delete_explicit_content = bool(entry.get("delete_explicit_content", False))
            if str(
                os.getenv("GETOFFLINE_ENABLE_SUBTITLE_EXTRACTION", "1")
            ).strip().lower() not in {"1", "true", "yes", "on"}:
                entry_subtitles_enabled = False
            subtitle_offset_seconds = entry.get("subtitle_offset_seconds")
            source_max_downloads = _coerce_positive_int(
                entry.get("max_downloads") or defaults.get("max_downloads") or 3, 3
            )
            podcast_scan_limit = _resolve_scan_limit(
                entry, defaults, source_max_downloads
            )
            title_exclude_terms = split_title_filter_terms(entry.get("title_exclude"))
            folder = os.path.join(defaults["output_root"], name)
            ensure_dir(folder)

            forced_redownload = bool(entry.get("redownload", False))
            forced_episode_url = str(entry.get("episode_url") or "").strip()
            if forced_redownload and forced_episode_url:
                episode_candidates = [
                    {
                        "mp3_url": forced_episode_url,
                        "title": str(entry.get("episode_title") or "Untitled Episode"),
                        "description": entry.get("episode_description"),
                        "artwork_url": entry.get("episode_artwork_url"),
                    }
                ]
            else:
                feed = feedparser.parse(url)
                episode_candidates = []
                for ep in feed.entries[:podcast_scan_limit]:
                    if not ep.enclosures:
                        continue
                    episode_candidates.append(
                        {
                            "mp3_url": ep.enclosures[0].href,
                            "title": _entry_title(ep) or "Untitled Episode",
                            "description": _entry_summary(ep),
                            "artwork_url": _podcast_artwork_url(feed, ep),
                        }
                    )

            log.info(
                "Podcast download limits for %s: max_new_downloads=%d feed_scan_limit=%d",
                name,
                source_max_downloads,
                podcast_scan_limit,
            )

            episode_jobs = []
            for candidate in episode_candidates:
                mp3_url = candidate["mp3_url"]
                episode_title = candidate["title"]
                title_filter_match = title_matches_filter(
                    episode_title, title_exclude_terms
                )
                if title_filter_match:
                    log.info(
                        "Skipping podcast episode for %s because title matches exclude filter %r: %s",
                        name,
                        title_filter_match,
                        episode_title,
                    )
                    continue
                safe_episode_title = sanitize(episode_title)
                item_uid = build_item_uid(
                    item_id=None,
                    item_url=mp3_url,
                    media_url=mp3_url,
                    title=episode_title,
                )

                if len(episode_jobs) >= source_max_downloads:
                    break

                if not forced_redownload and is_downloaded(
                    db_path, "podcast", name, item_uid
                ):
                    continue

                out_path = f"{folder}/{safe_episode_title}.%(ext)s"

                ydl_opts = {
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
                    "restrictfilenames": True,
                    "outtmpl_na_placeholder": "NA",
                    "outtmpl": out_path,
                    "continuedl": not forced_redownload,
                    "overwrites": forced_redownload,
                    "retries": 10,
                    "file_access_retries": 3,
                    "fragment_retries": 10,
                    "retry_sleep_functions": {
                        "http": _http_retry_backoff,
                    },
                    "socket_timeout": 30,
                    "quiet": True,
                    "no_warnings": True,
                    "noprogress": True,
                    "logger": _YoutubeDlQuietLogger(),
                }
                ffmpeg_audio_filter = str(
                    defaults.get("ffmpeg_audio_filter") or ""
                ).strip()
                if ffmpeg_audio_filter:
                    ydl_opts["postprocessor_args"] = ["-af", ffmpeg_audio_filter]

                episode_jobs.append(
                    {
                        "name": name,
                        "source_url": url,
                        "entry_subtitles_enabled": entry_subtitles_enabled,
                        "delete_explicit_content": delete_explicit_content,
                        "subtitle_offset_seconds": subtitle_offset_seconds,
                        "episode_title": episode_title,
                        "description": candidate["description"],
                        "artwork_url": candidate["artwork_url"],
                        "mp3_url": mp3_url,
                        "final_audio": Path(folder)
                        / f"{safe_episode_title}.{defaults['audio_format']}",
                        "ydl_opts": ydl_opts,
                    }
                )

            worker_count = int(defaults.get("processing_workers", 2))
            worker_count = max(1, min(worker_count, len(episode_jobs) or 1))

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_by_job = {}
                for job in episode_jobs:
                    future = executor.submit(_download_episode_media, job)
                    future_by_job[future] = job
                for future in as_completed(future_by_job):
                    job = future_by_job[future]
                    last_download_error = future.result()

                    if last_download_error is not None:
                        upsert_download(
                            db_path,
                            _episode_payload(
                                db_path=db_path,
                                source_name=job["name"],
                                source_url=job["source_url"],
                                storage_root=defaults["output_root"],
                                media_url=job["mp3_url"],
                                title=job["episode_title"],
                                description=job["description"],
                                file_path=None,
                                subtitle_enabled=job["entry_subtitles_enabled"],
                                subtitle_path=None,
                                download_status="failed",
                                error_message=str(last_download_error),
                                artwork_url=job.get("artwork_url"),
                            ),
                        )
                        log.error(
                            "Failed to download podcast episode after %s attempts: %s – %s (%s)",
                            PODCAST_DOWNLOAD_RETRIES,
                            job["name"],
                            job["episode_title"],
                            last_download_error,
                        )
                        continue

                    expected_audio_suffix = f".{defaults['audio_format']}"
                    if not _converted_media_ready(
                        job["final_audio"], expected_audio_suffix
                    ):
                        _delete_original_media_candidates(
                            job["final_audio"], expected_audio_suffix
                        )
                        log.warning(
                            "Skipping podcast database insert because converted media is missing or not in the expected format: %s – %s expected=%s",
                            job["name"],
                            job["episode_title"],
                            job["final_audio"],
                        )
                        downloaded_items.append(
                            f"Skipped podcast after conversion failure: {job['name']} – {job['episode_title']}"
                        )
                        continue
                    deleted_originals = _delete_original_media_candidates(
                        job["final_audio"], expected_audio_suffix
                    )
                    if deleted_originals:
                        log.info(
                            "Deleted original podcast media after conversion: %s",
                            ", ".join(str(path) for path in deleted_originals),
                        )

                    subtitle_path = create_subtitles(
                        media_file=job["final_audio"],
                        subtitle_offset_seconds=job["subtitle_offset_seconds"],
                        entry_subtitles_enabled=(
                            job["entry_subtitles_enabled"]
                            or job["delete_explicit_content"]
                        ),
                        subtitle_transcription_mode=defaults.get(
                            "subtitle_transcription_mode", "in_process"
                        ),
                        logger=log,
                        context_name=f"podcast {job['name']}",
                        context_label="podcast",
                    )
                    if job["delete_explicit_content"] and not subtitle_path:
                        deleted_paths = delete_media_artifacts(job["final_audio"])
                        log.warning(
                            "Deleted podcast download because transcript generation failed before profanity screening: %s – %s deleted_artifacts=%s",
                            job["name"],
                            job["episode_title"],
                            ", ".join(str(path) for path in deleted_paths) or "none",
                        )
                        downloaded_items.append(
                            f"Skipped podcast after transcript failure: {job['name']} – {job['episode_title']}"
                        )
                        continue

                    try:
                        explicit_match = (
                            screen_transcript(subtitle_path)
                            if job["delete_explicit_content"]
                            else None
                        )
                    except Exception as screening_exc:
                        deleted_paths = delete_media_artifacts(job["final_audio"])
                        log.warning(
                            "Deleted podcast download because profanity screening failed before database insert: %s – %s error=%s deleted_artifacts=%s",
                            job["name"],
                            job["episode_title"],
                            screening_exc,
                            ", ".join(str(path) for path in deleted_paths) or "none",
                        )
                        downloaded_items.append(
                            f"Skipped podcast after profanity screening failure: {job['name']} – {job['episode_title']}"
                        )
                        continue
                    if explicit_match is not None:
                        deleted_paths = delete_media_artifacts(job["final_audio"])
                        log_filtered_deletion(
                            source_type="podcast",
                            source_name=job["name"],
                            title=job["episode_title"],
                            media_path=job["final_audio"],
                            match=explicit_match,
                            deleted_paths=deleted_paths,
                        )
                        downloaded_items.append(
                            f"Filtered podcast: {job['name']} – {job['episode_title']}"
                        )
                        log.warning(
                            "Deleted podcast after transcript screening: %s – %s (%s)",
                            job["name"],
                            job["episode_title"],
                            explicit_match.category,
                        )
                        continue
                    if subtitle_path and not job["entry_subtitles_enabled"]:
                        subtitle_path.unlink(missing_ok=True)
                        subtitle_path = None
                    if subtitle_path:
                        downloaded_items.append(
                            f"Subtitles: Podcast – {subtitle_path.name}"
                        )

                    upsert_download(
                        db_path,
                        _episode_payload(
                            db_path=db_path,
                            source_name=job["name"],
                            source_url=job["source_url"],
                            storage_root=defaults["output_root"],
                            media_url=job["mp3_url"],
                            title=job["episode_title"],
                            description=job["description"],
                            file_path=job["final_audio"],
                            subtitle_enabled=job["entry_subtitles_enabled"],
                            subtitle_path=subtitle_path,
                            download_status="downloaded",
                            artwork_url=job.get("artwork_url"),
                        ),
                    )

                    downloaded_items.append(
                        f"Podcast: {job['name']} – {job['episode_title']}"
                    )

            cleanup_subtitle_sidecars_for_folder(Path(folder))

        except Exception as e:
            log.error(f"Failed to process podcast {entry}: {e}")


def _parent_rss_mb() -> float:
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return float(rss_kb) / 1024.0


def download_podcasts(config, downloaded_items):
    log.info("Downloading podcasts natively in current worker process")
    _download_podcasts_in_process(config, downloaded_items)


def _get_youtubedl():
    return YoutubeDL or importlib.import_module("yt_dlp").YoutubeDL
