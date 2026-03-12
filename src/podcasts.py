import os
from pathlib import Path

import feedparser
from yt_dlp import YoutubeDL

from database import build_item_uid, init_database, is_downloaded, resolve_database_path, upsert_download
from logger import get_logger
from subtitles import cleanup_subtitle_sidecars_for_folder, create_subtitles
from utils import ensure_dir, sanitize


class _YoutubeDlQuietLogger:
    def debug(self, msg):
        _ = msg

    def warning(self, msg):
        _ = msg

    def error(self, msg):
        if msg:
            log.error("%s", msg)


log = get_logger("podcast")


def _episode_payload(
    *,
    db_path,
    source_name,
    source_url,
    media_url,
    title,
    description,
    file_path,
    subtitle_enabled,
    subtitle_path,
    download_status,
    error_message=None,
):
    file_value = Path(file_path) if file_path else None
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
        "subtitle_path": str(subtitle_path) if subtitle_path else None,
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
    defaults["database_path"] = db_path
    init_database(db_path)

    for entry in config.get("podcasts", []):
        try:
            name = sanitize(entry["name"])
            url = entry["url"]
            entry_subtitles_enabled = entry.get("subtitles", True)
            subtitle_offset_seconds = entry.get("subtitle_offset_seconds")
            folder = os.path.join(defaults["output_root"], name)
            ensure_dir(folder)

            feed = feedparser.parse(url)
            entries = feed.entries[: defaults["max_downloads"]]

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
                    "quiet": True,
                    "no_warnings": True,
                    "noprogress": True,
                    "logger": _YoutubeDlQuietLogger(),
                }

                log.info(f"Downloading podcast: {name} – {episode_title}")
                try:
                    with YoutubeDL(ydl_opts) as ydl:
                        ydl.download([mp3_url])
                except Exception as download_exc:
                    upsert_download(
                        db_path,
                        _episode_payload(
                            db_path=db_path,
                            source_name=name,
                            source_url=url,
                            media_url=mp3_url,
                            title=episode_title,
                            description=getattr(ep, "summary", None),
                            file_path=None,
                            subtitle_enabled=entry_subtitles_enabled,
                            subtitle_path=None,
                            download_status="failed",
                            error_message=str(download_exc),
                        ),
                    )
                    raise

                final_audio = Path(folder) / f"{safe_episode_title}.{defaults['audio_format']}"
                subtitle_path = create_subtitles(
                    media_file=final_audio,
                    subtitle_offset_seconds=subtitle_offset_seconds,
                    entry_subtitles_enabled=entry_subtitles_enabled,
                    logger=log,
                    context_name=f"podcast {name}",
                    context_label="podcast",
                )
                if subtitle_path:
                    downloaded_items.append(f"Subtitles: Podcast – {subtitle_path.name}")

                upsert_download(
                    db_path,
                    _episode_payload(
                        db_path=db_path,
                        source_name=name,
                        source_url=url,
                        media_url=mp3_url,
                        title=episode_title,
                        description=getattr(ep, "summary", None),
                        file_path=final_audio,
                        subtitle_enabled=entry_subtitles_enabled,
                        subtitle_path=subtitle_path,
                        download_status="downloaded",
                    ),
                )

                downloaded_items.append(f"Podcast: {name} – {episode_title}")

            cleanup_subtitle_sidecars_for_folder(Path(folder))

        except Exception as e:
            log.error(f"Failed to process podcast {entry}: {e}")
