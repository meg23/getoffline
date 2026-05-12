import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import feedparser
from yt_dlp import YoutubeDL

from database import build_item_uid, ensure_config_seeded, get_stored_config, init_database, is_downloaded, resolve_database_path, upsert_download
from logger import get_logger
from subtitles import cleanup_subtitle_sidecars_for_folder, create_subtitles
from utils import ensure_dir, sanitize, sanitize_channel_name


PODCAST_DOWNLOAD_RETRIES = 3


class _YoutubeDlQuietLogger:
    def debug(self, msg):
        _ = msg

    def warning(self, msg):
        _ = msg

    def error(self, msg):
        if msg:
            log.error("%s", msg)


log = get_logger("podcast")


def _download_episode_media(episode_job: dict):
    ydl_opts = episode_job["ydl_opts"]
    mp3_url = episode_job["mp3_url"]
    name = episode_job["name"]
    episode_title = episode_job["episode_title"]

    log.info(f"Downloading podcast: {name} – {episode_title}")
    last_download_error = None
    for attempt in range(1, PODCAST_DOWNLOAD_RETRIES + 1):
        try:
            with YoutubeDL(ydl_opts) as ydl:
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
):
    file_value = Path(file_path).expanduser().resolve() if file_path else None
    file_size = file_value.stat().st_size if file_value and file_value.exists() else None

    return {
        "source_type": "podcast",
        "source_name": source_name,
        "source_url": source_url,
        "item_uid": build_item_uid(item_id=None, item_url=media_url, media_url=media_url, title=title),
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
        "subtitle_path": str(Path(subtitle_path).expanduser().resolve()) if subtitle_path else None,
        "download_status": download_status,
        "error_message": error_message,
        "raw_metadata": {
            "feed_url": source_url,
            "media_url": media_url,
            "title": title,
            "description": description,
        },
    }


def download_podcasts(config, downloaded_items):
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
            if str(os.getenv("GETOFFLINE_ENABLE_SUBTITLE_EXTRACTION", "1")).strip().lower() not in {"1", "true", "yes", "on"}:
                entry_subtitles_enabled = False
            subtitle_offset_seconds = entry.get("subtitle_offset_seconds")
            folder = os.path.join(defaults["output_root"], name)
            ensure_dir(folder)

            feed = feedparser.parse(url)
            entries = feed.entries[: defaults["max_downloads"]]

            episode_jobs = []
            for ep in entries:
                if not ep.enclosures:
                    continue

                mp3_url = ep.enclosures[0].href
                episode_title = str(getattr(ep, "title", "")).strip() or "Untitled Episode"
                safe_episode_title = sanitize(episode_title)
                item_uid = build_item_uid(
                    item_id=None,
                    item_url=mp3_url,
                    media_url=mp3_url,
                    title=episode_title,
                )

                if is_downloaded(db_path, "podcast", name, item_uid):
                    continue

                out_path = f"{folder}/{safe_episode_title}.%(ext)s"

                ydl_opts = {
                    "extract_audio": True,
                    "audio_format": defaults["audio_format"],
                    "audio_quality": str(defaults["audio_quality"]),
                    "restrictfilenames": True,
                    "outtmpl_na_placeholder": "NA",
                    "outtmpl": out_path,
                    "continuedl": True,
                    "retries": 10,
                    "file_access_retries": 3,
                    "fragment_retries": 10,
                    "retry_sleep_functions": {
                        "http": lambda n: min(2**n, 10),
                    },
                    "socket_timeout": 30,
                    "quiet": True,
                    "no_warnings": True,
                    "noprogress": True,
                    "logger": _YoutubeDlQuietLogger(),
                }
                ffmpeg_audio_filter = str(defaults.get("ffmpeg_audio_filter") or "").strip()
                if ffmpeg_audio_filter:
                    ydl_opts["postprocessor_args"] = ["-af", ffmpeg_audio_filter]

                episode_jobs.append(
                    {
                        "name": name,
                        "source_url": url,
                        "entry_subtitles_enabled": entry_subtitles_enabled,
                        "subtitle_offset_seconds": subtitle_offset_seconds,
                        "episode_title": episode_title,
                        "description": getattr(ep, "summary", None),
                        "mp3_url": mp3_url,
                        "final_audio": Path(folder) / f"{safe_episode_title}.{defaults['audio_format']}",
                        "ydl_opts": ydl_opts,
                    }
                )

            worker_count = int(defaults.get("processing_workers", 2))
            worker_count = max(1, min(worker_count, len(episode_jobs) or 1))

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_by_job = {executor.submit(_download_episode_media, job): job for job in episode_jobs}
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

                    subtitle_path = create_subtitles(
                        media_file=job["final_audio"],
                        subtitle_offset_seconds=job["subtitle_offset_seconds"],
                        entry_subtitles_enabled=job["entry_subtitles_enabled"],
                        subtitle_transcription_mode=defaults.get("subtitle_transcription_mode", "subprocess"),
                        logger=log,
                        context_name=f"podcast {job['name']}",
                        context_label="podcast",
                    )
                    if subtitle_path:
                        downloaded_items.append(f"Subtitles: Podcast – {subtitle_path.name}")

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
                        ),
                    )

                    downloaded_items.append(f"Podcast: {job['name']} – {job['episode_title']}")

            cleanup_subtitle_sidecars_for_folder(Path(folder))

        except Exception as e:
            log.error(f"Failed to process podcast {entry}: {e}")
