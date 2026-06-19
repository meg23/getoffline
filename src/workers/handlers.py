import hashlib
import os
import subprocess
from pathlib import Path
from typing import Iterable

from app.queue import publish_job
from django.utils import timezone
from workers.logger import get_logger
from models.jobs import create_job
from models.models import Download, Job, MediaSummary, ProfileConfigValue, SourceConfig, TranscriptSegment
from workers.utils import sanitize_channel_name
from workers.youtube import _apply_ytdlp_player_js_variant_workaround, _enable_youtube_ejs_remote_component
from workers.subtitles import create_subtitles
from workers.summary_tasks import _load_segments_from_subtitle
from workers.summarization import summarize_segments


log = get_logger("workers.handlers")


class _WorkerYtDlpLogger:
    def debug(self, msg):
        if msg and _yt_dlp_verbose_enabled():
            log.info("yt-dlp debug: %s", msg)

    def warning(self, msg):
        if msg:
            log.warning("yt-dlp warning: %s", msg)

    def error(self, msg):
        if msg:
            log.error("yt-dlp error: %s", msg)


def _yt_dlp_progress_hook(event: dict) -> None:
    status = event.get("status")
    filename = event.get("filename") or event.get("tmpfilename")
    downloaded = event.get("downloaded_bytes")
    total = event.get("total_bytes") or event.get("total_bytes_estimate")
    speed = event.get("speed")
    eta = event.get("eta")
    def mb(value):
        return round(float(value) / (1024 * 1024), 2) if value is not None else None
    if status == "downloading":
        log.info(
            "yt-dlp downloading filename=%s downloaded_mb=%s total_mb=%s speed_mb_s=%s eta=%s",
            filename,
            mb(downloaded),
            mb(total),
            mb(speed),
            eta,
        )
    elif status == "finished":
        log.info("yt-dlp download finished filename=%s total_mb=%s", filename, mb(total or downloaded))
    elif status == "error":
        log.error("yt-dlp download error filename=%s event=%s", filename, event)
    else:
        log.info("yt-dlp progress status=%s filename=%s event=%s", status, filename, event)


def _yt_dlp_verbose_enabled() -> bool:
    return str(os.getenv("GETOFFLINE_YTDLP_VERBOSE", "0")).strip().lower() in {"1", "true", "yes", "on"}


def _yt_dlp_base_options(**overrides) -> dict:
    options = {
        "logger": _WorkerYtDlpLogger(),
        "progress_hooks": [_yt_dlp_progress_hook],
        "verbose": _yt_dlp_verbose_enabled(),
        "quiet": False,
        "no_warnings": False,
    }
    options.update(overrides)
    return options


def _log_youtube_response(prefix: str, payload: dict) -> None:
    entries = payload.get("entries") if isinstance(payload, dict) else None
    log.info(
        "%s extractor=%s extractor_key=%s id=%s title=%s webpage_url=%s entries=%s live_status=%s availability=%s",
        prefix,
        payload.get("extractor"),
        payload.get("extractor_key"),
        payload.get("id"),
        payload.get("title"),
        payload.get("webpage_url"),
        len(entries or []) if entries is not None else 0,
        payload.get("live_status"),
        payload.get("availability"),
    )


def _profile_setting(profile_id: str, key: str, default: str) -> str:
    value = (
        ProfileConfigValue.objects.filter(profile_id=profile_id, key=key)
        .values_list("value", flat=True)
        .first()
    )
    return str(value if value not in {None, ""} else default)


def _download_output_root(profile_id: str) -> Path:
    root = _profile_setting(profile_id, "output_root", f"./downloads/profiles/{profile_id}")
    return Path(root).expanduser().resolve()


def _preferred_media_kind(download: Download, payload: dict) -> str:
    requested = str(payload.get("media_type") or "").strip().lower()
    if requested in {"audio", "video"}:
        return requested
    source_media_type = (
        SourceConfig.objects.filter(
            profile_id=download.profile_id,
            source_type=download.source_type,
            name=download.source_name,
        )
        .values_list("media_type", flat=True)
        .first()
    )
    if str(source_media_type or "").strip().lower() in {"audio", "video"}:
        return str(source_media_type).strip().lower()
    if download.source_type == SourceConfig.SOURCE_PODCAST:
        return "audio"
    return "video" if (download.file_ext or "").lower() in {"mp4", "mkv", "webm", "mov"} else "audio"


def _target_path(source_path: Path, target_ext: str) -> Path:
    clean_ext = target_ext.lower().lstrip(".") or source_path.suffix.lstrip(".")
    candidate = source_path.with_name(f"{source_path.stem}.converted.{clean_ext}")
    counter = 1
    while candidate.exists() and candidate != source_path:
        candidate = source_path.with_name(f"{source_path.stem}.converted-{counter}.{clean_ext}")
        counter += 1
    return candidate


def _preferred_target_ext(profile_id: str, media_kind: str) -> str:
    return _profile_setting(profile_id, "audio_format" if media_kind == "audio" else "video_format", "mp3" if media_kind == "audio" else "mp4").strip().lower()


def _download_requires_ffmpeg(download: Download, payload: dict) -> tuple[bool, str, str]:
    media_kind = _preferred_media_kind(download, payload)
    target_ext = _preferred_target_ext(download.profile_id, media_kind)
    current_ext = (download.file_ext or Path(str(download.file_path or "")).suffix.lstrip(".")).strip().lower()
    return current_ext != target_ext, media_kind, target_ext


def _ffmpeg_audio_args(profile_id: str, target_ext: str) -> list[str]:
    quality = _profile_setting(profile_id, "audio_quality", "0").strip() or "0"
    audio_filter = _profile_setting(profile_id, "ffmpeg_audio_filter", "").strip()
    args = ["-vn"]
    if target_ext == "mp3":
        args.extend(["-codec:a", "libmp3lame", "-q:a", quality])
    elif target_ext == "opus":
        args.extend(["-codec:a", "libopus", "-b:a", "96k"])
    else:
        args.extend(["-codec:a", "aac", "-b:a", "192k"])
    if audio_filter:
        args.extend(["-af", audio_filter])
    return args


def _ffmpeg_video_args(profile_id: str, target_ext: str) -> list[str]:
    codec = _profile_setting(profile_id, "video_codec", "h264").strip().lower()
    args = ["-map", "0:v:0?", "-map", "0:a:0?", "-map", "0:s?", "-c:s", "mov_text" if target_ext == "mp4" else "copy"]
    if codec == "copy":
        args.extend(["-c:v", "copy"])
    elif codec in {"h264", "avc"}:
        args.extend(["-c:v", "libx264", "-crf", "23", "-preset", "ultrafast", "-pix_fmt", "yuv420p"])
    else:
        args.extend(["-c:v", "libx265", "-tag:v", "hvc1", "-crf", "28", "-preset", "medium"])
    args.extend(["-c:a", "aac", "-b:a", "192k"])
    if target_ext == "mp4":
        args.extend(["-movflags", "+faststart"])
    return args


def _tail_text(value: str | None, *, limit: int = 4000) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"...{text[-limit:]}"


def transcode_media(job: Job) -> None:
    """FFmpeg worker: convert a downloaded file, update its row, then remove the original."""
    payload = job.payload if isinstance(job.payload, dict) else {}
    log.info("FFmpeg worker received job job_id=%s profile_id=%s payload=%s", job.id, job.profile_id, payload)
    download_id = payload.get("download_id")
    download = Download.objects.filter(pk=download_id, profile_id=job.profile_id).first() if download_id else None
    deferred_lookup = payload.get("download_lookup") if isinstance(payload.get("download_lookup"), dict) else None
    deferred_defaults = payload.get("download_defaults") if isinstance(payload.get("download_defaults"), dict) else None
    if download is None and not (deferred_lookup and deferred_defaults and payload.get("source_file_path")):
        log.warning("FFmpeg worker skipped missing download job_id=%s download_id=%s", job.id, download_id)
        return
    source_path = Path(str(download.file_path if download is not None else payload.get("source_file_path"))).expanduser().resolve()
    log.info(
        "FFmpeg worker loaded download job_id=%s download_id=%s title=%s source_type=%s source_name=%s file_path=%s file_ext=%s db_size_bytes=%s status=%s",
        job.id,
        download.id if download is not None else "deferred",
        download.title if download is not None else deferred_defaults.get("title"),
        download.source_type if download is not None else deferred_lookup.get("source_type"),
        download.source_name if download is not None else deferred_lookup.get("source_name"),
        download.file_path if download is not None else payload.get("source_file_path"),
        download.file_ext if download is not None else Path(str(payload.get("source_file_path"))).suffix.lstrip("."),
        download.file_size_bytes if download is not None else deferred_defaults.get("file_size_bytes"),
        download.download_status if download is not None else "deferred_insert",
    )
    if not source_path.exists():
        log.error("FFmpeg worker input file is missing job_id=%s download_id=%s path=%s", job.id, download.id if download is not None else "deferred", source_path)
        raise FileNotFoundError(f"Downloaded file is missing: {source_path}")
    input_size = source_path.stat().st_size
    media_kind = _preferred_media_kind(download, payload) if download is not None else str(payload.get("media_type") or "video")
    target_ext = "mp3" if media_kind == "audio" else _preferred_target_ext(job.profile_id, media_kind)
    target_path = _target_path(source_path, target_ext)
    ffmpeg_path = _profile_setting(job.profile_id, "ffmpeg_path", "ffmpeg")
    codec_args = _ffmpeg_audio_args(job.profile_id, target_ext) if media_kind == "audio" else _ffmpeg_video_args(job.profile_id, target_ext)
    command = [ffmpeg_path, "-y", "-i", str(source_path), *codec_args, str(target_path)]
    log.info(
        "FFmpeg conversion prepared job_id=%s download_id=%s media_kind=%s input=%s input_size_bytes=%s target=%s target_ext=%s ffmpeg_path=%s codec_args=%s",
        job.id,
        download.id if download is not None else "deferred",
        media_kind,
        source_path,
        input_size,
        target_path,
        target_ext,
        ffmpeg_path,
        codec_args,
    )
    log.info("FFmpeg conversion starting job_id=%s download_id=%s command=%s", job.id, download.id if download is not None else "deferred", command)
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        log.error(
            "FFmpeg conversion failed job_id=%s download_id=%s returncode=%s stdout_tail=%s stderr_tail=%s",
            job.id,
            download.id if download is not None else "deferred",
            exc.returncode,
            _tail_text(exc.stdout),
            _tail_text(exc.stderr),
        )
        raise
    log.info(
        "FFmpeg conversion subprocess finished job_id=%s download_id=%s returncode=%s stdout_tail=%s stderr_tail=%s",
        job.id,
        download.id if download is not None else "deferred",
        result.returncode,
        _tail_text(result.stdout, limit=1000),
        _tail_text(result.stderr, limit=1000),
    )
    if not target_path.exists():
        log.error("FFmpeg conversion output missing job_id=%s download_id=%s target=%s", job.id, download.id if download is not None else "deferred", target_path)
        raise FileNotFoundError(f"FFmpeg output file was not created: {target_path}")
    old_path = source_path
    output_size = target_path.stat().st_size
    output_root = Path(str(payload.get("output_root") or _download_output_root(job.profile_id))).expanduser().resolve()
    log.info(
        "FFmpeg conversion updating database job_id=%s download_id=%s old_path=%s new_path=%s old_size_bytes=%s new_size_bytes=%s",
        job.id,
        download.id if download is not None else "deferred",
        old_path,
        target_path,
        input_size,
        output_size,
    )
    if download is None:
        final_defaults = dict(deferred_defaults)
        final_defaults.update(
            {
                "file_path": str(target_path),
                "file_path_relative": str(target_path.relative_to(output_root)) if target_path.is_relative_to(output_root) else None,
                "file_ext": target_path.suffix.lstrip("."),
                "file_size_bytes": output_size,
                "download_status": "downloaded",
                "completed_at": timezone.now(),
                "last_seen_at": timezone.now(),
            }
        )
        download, _created = Download.objects.update_or_create(**deferred_lookup, defaults=final_defaults)
    else:
        download.file_path = str(target_path)
        download.file_path_relative = str(target_path.relative_to(output_root)) if target_path.is_relative_to(output_root) else None
        download.file_ext = target_path.suffix.lstrip(".")
        download.file_size_bytes = output_size
        download.download_status = "downloaded"
        download.completed_at = timezone.now()
        download.last_seen_at = timezone.now()
        download.save(update_fields=["file_path", "file_path_relative", "file_ext", "file_size_bytes", "download_status", "completed_at", "last_seen_at"])
    log.info(
        "FFmpeg conversion finished job_id=%s download_id=%s output=%s output_size_bytes=%s original_deferred_delete=%s",
        job.id,
        download.id,
        target_path,
        output_size,
        old_path != target_path,
    )
    child = create_job(
        profile_id=job.profile_id,
        job_type="generate_transcript",
        payload={"download_id": download.id, "original_file_path": str(old_path) if old_path != target_path else "", "subtitles": payload.get("subtitles", True), "subtitle_offset_seconds": payload.get("subtitle_offset_seconds")},
        idempotency_key=f"generate_transcript:{job.profile_id}:{download.id}",
    )
    _publish_created_job(child)
    log.info("FFmpeg worker queued transcript job parent_job_id=%s download_id=%s child_job_id=%s", job.id, download.id, child.id)


def _find_downloaded_file(info: dict, ydl) -> Path | None:
    requested = info.get("requested_downloads") if isinstance(info, dict) else None
    if isinstance(requested, list):
        for item in requested:
            if isinstance(item, dict):
                candidate = item.get("filepath") or item.get("filename")
                if candidate and Path(candidate).exists():
                    return Path(candidate).expanduser().resolve()
    for key in ("filepath", "_filename", "filename"):
        candidate = info.get(key) if isinstance(info, dict) else None
        if candidate and Path(candidate).exists():
            return Path(candidate).expanduser().resolve()
    prepared = ydl.prepare_filename(info) if isinstance(info, dict) else ""
    if prepared and Path(prepared).exists():
        return Path(prepared).expanduser().resolve()
    return None



def _is_youtube_video_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return "youtube.com/watch" in lowered or "youtu.be/" in lowered or "youtube.com/shorts/" in lowered


def _youtube_video_url(entry: dict) -> str:
    item_id = str(entry.get("id") or "").strip()
    webpage_url = str(entry.get("webpage_url") or "").strip()
    raw_url = str(entry.get("url") or "").strip()
    if _is_youtube_video_url(webpage_url):
        return webpage_url
    if item_id and len(item_id) == 11:
        return f"https://www.youtube.com/watch?v={item_id}"
    if raw_url and len(raw_url) == 11 and raw_url.startswith("http") is False:
        return f"https://www.youtube.com/watch?v={raw_url}"
    if _is_youtube_video_url(raw_url):
        return raw_url
    return webpage_url or raw_url


def _download_with_yt_dlp(job: Job, payload: dict) -> Download | dict | None:
    download_url = str(payload.get("media_url") or payload.get("item_url") or payload.get("url") or "").strip()
    if not download_url:
        log.warning("Download worker skipped job with no URL job_id=%s payload=%s", job.id, payload)
        return None
    source_name = str(payload.get("source_name") or payload.get("source_type") or "GetOffline").strip()
    source_type = str(payload.get("source_type") or "youtube").strip()
    if source_type == SourceConfig.SOURCE_YOUTUBE and not _is_youtube_video_url(download_url):
        fallback_uid = str(payload.get("item_uid") or "").strip()
        if len(fallback_uid) == 11:
            download_url = f"https://www.youtube.com/watch?v={fallback_uid}"
            log.info("Downloader converted YouTube item uid to video URL job_id=%s item_uid=%s url=%s", job.id, fallback_uid, download_url)
        else:
            log.warning("Download worker skipped non-video YouTube URL job_id=%s url=%s payload=%s", job.id, download_url, payload)
            return None
    output_root = _download_output_root(job.profile_id)
    output_dir = output_root / sanitize_channel_name(source_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(output_dir / "%(title).200B [%(id)s].%(ext)s")
    ydl_opts = _yt_dlp_base_options(
        outtmpl=outtmpl,
        continuedl=True,
        retries=3,
        fragment_retries=3,
        noplaylist=True,
        playlist_items="1",
        playlistend=1,
    )
    max_height = _profile_setting(job.profile_id, "ytdlp_video_max_height", "720").strip()
    requested_media_type = str(payload.get("media_type") or ("audio" if source_type == SourceConfig.SOURCE_PODCAST else "video")).strip().lower()
    if source_type == SourceConfig.SOURCE_YOUTUBE and requested_media_type != "audio" and max_height.isdigit():
        ydl_opts["format"] = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/best[height<={max_height}]/best"
    if source_type == SourceConfig.SOURCE_YOUTUBE:
        _enable_youtube_ejs_remote_component(ydl_opts, f"download job {job.id}", _profile_setting(job.profile_id, "deno_path", "deno"))
        _apply_ytdlp_player_js_variant_workaround(ydl_opts)

    log.info(
        "yt-dlp download starting job_id=%s profile_id=%s source_type=%s source_name=%s url=%s output_template=%s options=%s",
        job.id,
        job.profile_id,
        source_type,
        source_name,
        download_url,
        outtmpl,
        {k: v for k, v in ydl_opts.items() if k not in {"logger", "progress_hooks"}},
    )
    from yt_dlp import YoutubeDL

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(download_url, download=True) or {}
        if isinstance(info, dict):
            _log_youtube_response("yt-dlp download response", info)
            downloaded_file = _find_downloaded_file(info, ydl)
        else:
            downloaded_file = None
    if downloaded_file is None:
        log.warning("yt-dlp download finished but file path was not found job_id=%s url=%s", job.id, download_url)
        return None
    now = timezone.now()
    item_uid = str(payload.get("item_uid") or info.get("id") or download_url)[:255]
    title = str(payload.get("title") or info.get("title") or downloaded_file.stem)
    download_lookup = {
        "profile_id": job.profile_id,
        "source_type": source_type,
        "source_name": source_name,
        "item_uid": item_uid,
    }
    download_defaults = {
        "source_url": payload.get("source_url") or download_url,
        "item_id": str(info.get("id") or item_uid)[:255],
        "item_url": payload.get("item_url") or info.get("webpage_url") or download_url,
        "media_url": download_url,
        "title": title,
        "description": info.get("description"),
        "uploader": info.get("uploader"),
        "channel": info.get("channel"),
        "upload_date": str(payload.get("published") or info.get("upload_date") or ""),
        "duration_seconds": int(info.get("duration")) if info.get("duration") else None,
        "file_path": str(downloaded_file),
        "file_path_relative": str(downloaded_file.relative_to(output_root)) if downloaded_file.is_relative_to(output_root) else None,
        "file_ext": downloaded_file.suffix.lstrip("."),
        "file_size_bytes": downloaded_file.stat().st_size if downloaded_file.exists() else None,
        "download_status": "downloaded",
        "last_seen_at": now,
        "completed_at": now,
    }
    media_kind = str(payload.get("media_type") or ("audio" if source_type == SourceConfig.SOURCE_PODCAST else "video")).strip().lower()
    target_ext = "mp3" if media_kind == "audio" else downloaded_file.suffix.lstrip(".").lower()
    if media_kind == "audio" and downloaded_file.suffix.lstrip(".").lower() != target_ext:
        log.info(
            "Download worker deferred database insert until conversion job_id=%s source_file=%s current_ext=%s target_ext=%s media_kind=%s",
            job.id,
            downloaded_file,
            downloaded_file.suffix.lstrip("."),
            target_ext,
            media_kind,
        )
        return {
            "source_file_path": str(downloaded_file),
            "output_root": str(output_root),
            "media_type": media_kind,
            "subtitles": payload.get("subtitles", True),
            "subtitle_offset_seconds": payload.get("subtitle_offset_seconds"),
            "download_lookup": download_lookup,
            "download_defaults": {key: value for key, value in download_defaults.items() if key not in {"last_seen_at", "completed_at"}},
            "item_uid": item_uid,
        }
    download, _created = Download.objects.update_or_create(
        **download_lookup,
        defaults=download_defaults,
    )
    log.info(
        "Download worker saved download row job_id=%s download_id=%s file_path=%s size_bytes=%s",
        job.id,
        download.id,
        downloaded_file,
        downloaded_file.stat().st_size if downloaded_file.exists() else None,
    )
    return download



def _source_from_payload(payload: dict) -> SourceConfig | None:
    source_id = payload.get("source_id")
    if not source_id:
        return None
    try:
        return SourceConfig.objects.filter(pk=int(source_id)).first()
    except (TypeError, ValueError):
        return None


def _downloaded_count_for_source(profile_id: str, source: SourceConfig) -> int:
    return Download.objects.filter(
        profile_id=profile_id,
        source_type=source.source_type,
        source_name=source.name,
        download_status="downloaded",
    ).count()


def _source_download_limit_reached(profile_id: str, payload: dict) -> bool:
    source = _source_from_payload(payload)
    if source is None:
        return False
    limit = _source_limit(source)
    downloaded_count = _downloaded_count_for_source(profile_id, source)
    if downloaded_count >= limit:
        log.info(
            "Download worker skipped because source max downloads is already reached profile_id=%s source_id=%s source_name=%s downloaded=%s limit=%s",
            profile_id,
            source.id,
            source.name,
            downloaded_count,
            limit,
        )
        return True
    log.info(
        "Download worker source max downloads check passed profile_id=%s source_id=%s source_name=%s downloaded=%s limit=%s",
        profile_id,
        source.id,
        source.name,
        downloaded_count,
        limit,
    )
    return False


def _publish_created_job(job: Job) -> None:
    log.info("Publishing child job job_id=%s job_type=%s profile_id=%s", job.id, job.job_type, job.profile_id)
    publish_job({"job_id": job.id, "job_type": job.job_type, "profile_id": job.profile_id, "attempt": 1})
    log.info("Published child job job_id=%s job_type=%s profile_id=%s", job.id, job.job_type, job.profile_id)


def _fallback_uid(*parts: object) -> str:
    text = "|".join(str(part or "") for part in parts).strip() or "unknown"
    return f"generated:{hashlib.sha1(text.encode('utf-8')).hexdigest()}"


def _idempotency_key(*parts: object) -> str:
    text = "|".join(str(part or "") for part in parts).strip() or "unknown"
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    prefix = ":".join(str(part or "") for part in parts[:3])[:160]
    return f"{prefix}:{digest}"[:255]


def _episode_was_downloaded(*, profile_id: str, source: SourceConfig, item_uid: str, item_url: str, title: str) -> bool:
    rows = Download.objects.filter(profile_id=profile_id, source_type=source.source_type, source_name=source.name)
    if item_uid and rows.filter(item_uid=item_uid).exists():
        return True
    if item_url and rows.filter(item_url=item_url).exists():
        return True
    if title and rows.filter(title=title).exists():
        return True
    return False


def _source_limit(source: SourceConfig) -> int:
    if source.max_downloads:
        limit = max(1, int(source.max_downloads))
        log.info("Using source max downloads source_id=%s source_name=%s limit=%s", source.id, source.name, limit)
        return limit
    profile_default = (
        ProfileConfigValue.objects.filter(profile_id=source.profile_id, key="max_downloads")
        .values_list("value", flat=True)
        .first()
    )
    if str(profile_default or "").strip().isdigit():
        return max(1, int(str(profile_default).strip()))
    limit = 10
    log.info("Using fallback max downloads source_id=%s source_name=%s limit=%s", source.id, source.name, limit)
    return limit


def _download_job_already_queued(idempotency_key: str) -> bool:
    return Job.objects.filter(
        idempotency_key=idempotency_key,
        status__in=[Job.STATUS_QUEUED, Job.STATUS_RUNNING],
    ).exists()


def _podcast_candidates(source: SourceConfig) -> Iterable[dict]:
    log.info("Checking podcast feed source_id=%s source_name=%s url=%s", source.id, source.name, source.url)
    import feedparser

    feed = feedparser.parse(source.url)
    if getattr(feed, "bozo", False):
        log.warning("Podcast feed parse warning source_id=%s source_name=%s error=%s", source.id, source.name, getattr(feed, "bozo_exception", "unknown"))
    entries = list(getattr(feed, "entries", []) or [])[: _source_limit(source)]
    feed_meta = getattr(feed, "feed", {}) or {}
    feed_title = str(getattr(feed_meta, "title", "") or getattr(feed_meta, "get", lambda _key, _default="": _default)("title", "") or "")
    log.info("Podcast feed parsed source_id=%s source_name=%s feed_title=%s entries_considered=%s limit=%s", source.id, source.name, feed_title, len(entries), _source_limit(source))
    for entry in entries:
        enclosure_url = ""
        for enclosure in getattr(entry, "enclosures", []) or []:
            enclosure_url = str(getattr(enclosure, "href", "") or enclosure.get("href", "")).strip()
            if enclosure_url:
                break
        item_url = enclosure_url or str(getattr(entry, "link", "") or "").strip()
        title = str(getattr(entry, "title", "") or item_url or "Untitled podcast episode").strip()
        published = str(getattr(entry, "published", "") or getattr(entry, "updated", "") or "").strip()
        item_uid = str(getattr(entry, "id", "") or getattr(entry, "guid", "") or item_url or "").strip()
        item_uid = item_uid or _fallback_uid(source.url, title, published)
        log.info("Podcast episode candidate source_id=%s source_name=%s item_uid=%s title=%s media_url=%s published=%s", source.id, source.name, item_uid[:255], title, enclosure_url or item_url, published)
        yield {
            "item_uid": item_uid[:255],
            "item_url": item_url,
            "media_url": enclosure_url or item_url,
            "title": title,
            "published": published,
        }


def _youtube_entries_from_url(url: str, limit: int, *, source: SourceConfig, reason: str) -> list[dict]:
    ydl_opts = _yt_dlp_base_options(
        extract_flat=True,
        skip_download=True,
        playlistend=limit,
        playlist_items=f"1-{limit}",
    )
    _enable_youtube_ejs_remote_component(ydl_opts, f"update source {source.name}", _profile_setting(source.profile_id, "deno_path", "deno"))
    _apply_ytdlp_player_js_variant_workaround(ydl_opts)
    log.info(
        "yt-dlp extract starting source_id=%s source_name=%s reason=%s url=%s options=%s",
        source.id,
        source.name,
        reason,
        url,
        {k: v for k, v in ydl_opts.items() if k not in {"logger", "progress_hooks"}},
    )
    from yt_dlp import YoutubeDL

    with YoutubeDL(ydl_opts) as ydl:
        payload = ydl.extract_info(url, download=False) or {}
    if isinstance(payload, dict):
        _log_youtube_response(f"yt-dlp extract response ({reason})", payload)
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not entries:
        entries = [payload]
    return [entry for entry in list(entries or []) if isinstance(entry, dict)]


def _youtube_candidate_from_entry(source: SourceConfig, entry: dict) -> dict | None:
    item_id = str(entry.get("id") or "").strip()
    item_url = _youtube_video_url(entry)
    if not _is_youtube_video_url(item_url):
        log.info(
            "Skipping non-video YouTube entry source_id=%s source_name=%s entry_id=%s title=%s url=%s",
            source.id,
            source.name,
            item_id,
            entry.get("title"),
            item_url,
        )
        return None
    title = str(entry.get("title") or item_url or "Untitled YouTube episode").strip()
    item_uid = item_id if len(item_id) == 11 else item_url or _fallback_uid(source.url, title)
    return {
        "item_uid": item_uid[:255],
        "item_url": item_url,
        "media_url": item_url,
        "title": title,
        "published": str(entry.get("upload_date") or entry.get("timestamp") or ""),
    }


def _youtube_candidates(source: SourceConfig) -> Iterable[dict]:
    log.info("Checking YouTube source source_id=%s source_name=%s url=%s", source.id, source.name, source.url)
    limit = _source_limit(source)
    entries = _youtube_entries_from_url(source.url, limit, source=source, reason="source")
    log.info("YouTube source parsed source_id=%s source_name=%s entries=%s limit=%s", source.id, source.name, len(entries), limit)
    yielded = 0
    for entry in entries:
        if yielded >= limit:
            break
        candidate = _youtube_candidate_from_entry(source, entry)
        if candidate is not None:
            yielded += 1
            yield candidate
            continue
        nested_url = _youtube_video_url(entry)
        if nested_url and nested_url != source.url:
            remaining = limit - yielded
            log.info(
                "Drilling into YouTube non-video entry source_id=%s source_name=%s nested_url=%s remaining=%s",
                source.id,
                source.name,
                nested_url,
                remaining,
            )
            for nested_entry in _youtube_entries_from_url(nested_url, remaining, source=source, reason="nested-entry"):
                if yielded >= limit:
                    break
                nested_candidate = _youtube_candidate_from_entry(source, nested_entry)
                if nested_candidate is None:
                    continue
                yielded += 1
                yield nested_candidate


def _candidates_for_source(source: SourceConfig) -> Iterable[dict]:
    if source.source_type == SourceConfig.SOURCE_PODCAST:
        return _podcast_candidates(source)
    if source.source_type == SourceConfig.SOURCE_YOUTUBE:
        return _youtube_candidates(source)
    log.warning("Unsupported source type for episode check source_id=%s source_type=%s", source.id, source.source_type)
    return []


def check_for_episodes(job: Job) -> None:
    """Serial discovery worker: scan every profile's sources and enqueue never-downloaded episodes."""
    profile_ids = list(
        SourceConfig.objects.filter(enabled=True)
        .order_by("profile_id")
        .values_list("profile_id", flat=True)
        .distinct()
    )
    log.info("Episode check started job_id=%s profiles=%s", job.id, len(profile_ids))
    total_sources = 0
    total_seen = 0
    total_enqueued = 0
    for profile_id in profile_ids:
        sources = list(SourceConfig.objects.filter(profile_id=profile_id, enabled=True).order_by("position", "id"))
        log.info("Episode check profile started job_id=%s profile_id=%s sources=%s", job.id, profile_id, len(sources))
        for source in sources:
            total_sources += 1
            source_seen = 0
            source_enqueued = 0
            limit = _source_limit(source)
            log.info(
                "Episode check source started profile_id=%s source_id=%s source_type=%s source_name=%s url=%s max_downloads=%s",
                profile_id,
                source.id,
                source.source_type,
                source.name,
                source.url,
                limit,
            )
            for candidate in _candidates_for_source(source):
                source_seen += 1
                total_seen += 1
                if source_enqueued >= limit:
                    log.info(
                        "Source max downloads reached profile_id=%s source_id=%s source_name=%s limit=%s seen=%s enqueued=%s",
                        profile_id,
                        source.id,
                        source.name,
                        limit,
                        source_seen,
                        source_enqueued,
                    )
                    break
                item_uid = str(candidate.get("item_uid") or "")[:255]
                item_url = str(candidate.get("item_url") or "")
                title = str(candidate.get("title") or "")
                if _episode_was_downloaded(
                    profile_id=profile_id,
                    source=source,
                    item_uid=item_uid,
                    item_url=item_url,
                    title=title,
                ):
                    log.info(
                        "Episode already downloaded profile_id=%s source_id=%s item_uid=%s title=%s",
                        profile_id,
                        source.id,
                        item_uid,
                        title,
                    )
                    continue
                if source.source_type == SourceConfig.SOURCE_PODCAST:
                    log.info("New podcast episode found profile_id=%s source_id=%s source_name=%s item_uid=%s title=%s media_url=%s", profile_id, source.id, source.name, item_uid, title, candidate.get("media_url") or item_url)
                elif source.source_type == SourceConfig.SOURCE_YOUTUBE:
                    log.info("New YouTube episode found profile_id=%s source_id=%s source_name=%s item_uid=%s title=%s item_url=%s", profile_id, source.id, source.name, item_uid, title, item_url)
                idempotency_key = _idempotency_key("download_episode", profile_id, source.id, item_uid or item_url or title)
                if _download_job_already_queued(idempotency_key):
                    source_enqueued += 1
                    log.info(
                        "Download episode job already queued profile_id=%s source_id=%s item_uid=%s title=%s reserved_for_source=%s limit=%s",
                        profile_id,
                        source.id,
                        item_uid,
                        title,
                        source_enqueued,
                        limit,
                    )
                    continue
                child = create_job(
                    profile_id=profile_id,
                    job_type="download_episode",
                    payload={
                        "source_id": source.id,
                        "source_type": source.source_type,
                        "source_name": source.name,
                        "source_url": source.url,
                        "media_type": source.media_type or ("audio" if source.source_type == SourceConfig.SOURCE_PODCAST else "video"),
                        "source_max_downloads": limit,
                        "item_uid": item_uid,
                        "item_url": item_url,
                        "media_url": candidate.get("media_url") or item_url,
                        "title": title,
                        "published": candidate.get("published") or "",
                        "subtitles": bool(source.subtitles),
                        "subtitle_offset_seconds": source.subtitle_offset_seconds,
                    },
                    idempotency_key=idempotency_key,
                )
                _publish_created_job(child)
                source_enqueued += 1
                total_enqueued += 1
                log.info("Download episode job enqueued profile_id=%s source_id=%s child_job_id=%s item_uid=%s title=%s enqueued_for_source=%s limit=%s", profile_id, source.id, child.id, item_uid, title, source_enqueued, limit)
            log.info(
                "Episode check source finished profile_id=%s source_id=%s source_type=%s seen=%s enqueued=%s",
                profile_id,
                source.id,
                source.source_type,
                source_seen,
                source_enqueued,
            )
        log.info("Episode check profile finished job_id=%s profile_id=%s", job.id, profile_id)
    log.info(
        "Episode check finished job_id=%s profiles=%s sources=%s episodes_seen=%s enqueued_download_jobs=%s",
        job.id,
        len(profile_ids),
        total_sources,
        total_seen,
        total_enqueued,
    )


def update_downloads(job: Job) -> None:
    log.info("update_downloads routed to episode checker job_id=%s", job.id)
    check_for_episodes(job)


def download_episode(job: Job) -> None:
    """Serial downloader worker: download one queued episode and enqueue transcript work.

    The queue is intentionally single-consumer/prefetch=1 so episode downloads happen one at a time.
    """
    log.info("Download worker started job_id=%s profile_id=%s payload=%s", job.id, job.profile_id, job.payload)
    payload = job.payload if isinstance(job.payload, dict) else {}
    download_id = payload.get("download_id")
    if not download_id:
        if _source_download_limit_reached(job.profile_id, payload):
            return
        downloaded_result = _download_with_yt_dlp(job, payload)
        if downloaded_result is None:
            log.warning("Download worker did not create a download row job_id=%s", job.id)
            return
        if isinstance(downloaded_result, dict):
            child = create_job(
                profile_id=job.profile_id,
                job_type="transcode_media",
                payload=downloaded_result,
                idempotency_key=f"transcode_media:{job.profile_id}:{downloaded_result.get('item_uid') or downloaded_result.get('source_file_path')}",
            )
            _publish_created_job(child)
            log.info("Download worker queued FFmpeg job before database insert parent_job_id=%s child_job_id=%s source_file=%s", job.id, child.id, downloaded_result.get("source_file_path"))
            return
        download_id = downloaded_result.id
    download = Download.objects.filter(pk=download_id, profile_id=job.profile_id).first()
    if download is None:
        log.warning("Download worker could not find downloaded row for next stage job_id=%s download_id=%s", job.id, download_id)
        return
    media_kind = _preferred_media_kind(download, payload)
    target_ext = "mp3" if media_kind == "audio" else (download.file_ext or "")
    requires_ffmpeg = media_kind == "audio" and (download.file_ext or "").lower() != "mp3"
    next_job_type = "transcode_media" if requires_ffmpeg else "generate_transcript"
    next_payload = {"download_id": download_id, "media_type": media_kind, "subtitles": payload.get("subtitles", True), "subtitle_offset_seconds": payload.get("subtitle_offset_seconds")} if requires_ffmpeg else {"download_id": download_id, "subtitles": payload.get("subtitles", True), "subtitle_offset_seconds": payload.get("subtitle_offset_seconds")}
    log.info(
        "Download worker selected next stage parent_job_id=%s download_id=%s file_ext=%s media_kind=%s target_ext=%s next_job_type=%s",
        job.id,
        download_id,
        download.file_ext,
        media_kind,
        target_ext,
        next_job_type,
    )
    child = create_job(
        profile_id=job.profile_id,
        job_type=next_job_type,
        payload=next_payload,
        idempotency_key=f"{next_job_type}:{job.profile_id}:{download_id}",
    )
    _publish_created_job(child)
    log.info("Download worker queued next stage parent_job_id=%s download_id=%s child_job_id=%s child_job_type=%s", job.id, download_id, child.id, child.job_type)


def download_single(job: Job) -> None:
    log.info("download_single routed to downloader job_id=%s", job.id)
    download_episode(job)


def _subtitles_enabled_for_download(download: Download, payload: dict) -> bool:
    if "subtitles" in payload:
        return str(payload.get("subtitles")).strip().lower() not in {"0", "false", "no", "off"}
    value = SourceConfig.objects.filter(profile_id=download.profile_id, source_type=download.source_type, name=download.source_name).values_list("subtitles", flat=True).first()
    return True if value is None else bool(value)


def _subtitle_offset_for_download(download: Download, payload: dict) -> float | None:
    if payload.get("subtitle_offset_seconds") not in {None, ""}:
        return float(payload.get("subtitle_offset_seconds"))
    return SourceConfig.objects.filter(profile_id=download.profile_id, source_type=download.source_type, name=download.source_name).values_list("subtitle_offset_seconds", flat=True).first()


def generate_transcript(job: Job) -> None:
    """Generate Whisper subtitles/transcript segments, then enqueue summary work."""
    log.info("Transcript worker started job_id=%s profile_id=%s payload=%s", job.id, job.profile_id, job.payload)
    payload = job.payload if isinstance(job.payload, dict) else {}
    download_id = payload.get("download_id")
    if not download_id:
        log.warning("Transcript worker skipped job with no download_id job_id=%s", job.id)
        return
    download = Download.objects.filter(pk=download_id, profile_id=job.profile_id).first()
    if download is None:
        log.warning("Transcript worker skipped missing download job_id=%s download_id=%s profile_id=%s", job.id, download_id, job.profile_id)
        return
    media_path = Path(str(download.file_path or "")).expanduser().resolve()
    log.info(
        "Transcript worker loaded download job_id=%s download_id=%s title=%s source_type=%s source_name=%s file_path=%s file_ext=%s subtitle_path=%s",
        job.id,
        download_id,
        download.title,
        download.source_type,
        download.source_name,
        download.file_path,
        download.file_ext,
        download.subtitle_path,
    )
    if not media_path.exists():
        log.warning("Transcript worker skipped missing media file job_id=%s download_id=%s path=%s", job.id, download_id, media_path)
    else:
        enabled = _subtitles_enabled_for_download(download, payload)
        subtitle_offset = _subtitle_offset_for_download(download, payload)
        transcription_mode = _profile_setting(job.profile_id, "subtitle_transcription_mode", "in_process")
        log.info(
            "Transcript worker starting subtitle generation job_id=%s download_id=%s enabled=%s media_path=%s size_bytes=%s offset=%s mode=%s",
            job.id,
            download_id,
            enabled,
            media_path,
            media_path.stat().st_size,
            subtitle_offset,
            transcription_mode,
        )
        subtitle_path = create_subtitles(media_path, subtitle_offset, enabled, log, download.title or media_path.name, "download", transcription_mode)
        if subtitle_path is not None:
            output_root = _download_output_root(job.profile_id)
            download.subtitle_path = str(subtitle_path)
            download.subtitle_path_relative = str(subtitle_path.relative_to(output_root)) if subtitle_path.is_relative_to(output_root) else None
            download.save(update_fields=["subtitle_path", "subtitle_path_relative"])
            segments = _load_segments_from_subtitle(Path(subtitle_path))
            TranscriptSegment.objects.filter(download=download).delete()
            TranscriptSegment.objects.bulk_create([TranscriptSegment(download=download, subtitle_path=str(subtitle_path), start_seconds=0.0, end_seconds=None, text=text) for text in segments])
            log.info("Transcript worker saved subtitles job_id=%s download_id=%s subtitle_path=%s segments=%s", job.id, download_id, subtitle_path, len(segments))
        else:
            log.warning("Transcript worker completed without subtitle output job_id=%s download_id=%s enabled=%s media_path=%s", job.id, download_id, enabled, media_path)
    child = create_job(
        profile_id=job.profile_id,
        job_type="generate_summary",
        payload={"download_id": download_id, "original_file_path": payload.get("original_file_path") or ""},
        idempotency_key=f"generate_summary:{job.profile_id}:{download_id}",
    )
    _publish_created_job(child)
    log.info("Transcript worker queued summary job parent_job_id=%s download_id=%s child_job_id=%s", job.id, download_id, child.id)


def generate_summary(job: Job) -> None:
    log.info("Summary worker started job_id=%s profile_id=%s payload=%s", job.id, job.profile_id, job.payload)
    payload = job.payload if isinstance(job.payload, dict) else {}
    download_id = payload.get("download_id")
    download = Download.objects.filter(pk=download_id, profile_id=job.profile_id).first() if download_id else None
    if download is None:
        log.warning("Summary worker skipped missing download job_id=%s download_id=%s", job.id, download_id)
        return
    segments = list(download.transcript_segments.order_by("start_seconds", "id").values_list("text", flat=True))
    if not segments and download.subtitle_path:
        segments = _load_segments_from_subtitle(Path(download.subtitle_path))
    if segments:
        model_name = _profile_setting(job.profile_id, "summary_model", "qwen2.5:0.5b")
        timeout_seconds = int(_profile_setting(job.profile_id, "summary_timeout_seconds", "90"))
        result = summarize_segments(segments, model_name=model_name, mode="in_process", timeout_seconds=timeout_seconds)
        summary = str(result.get("summary_text") or "").strip()
        if summary:
            MediaSummary.objects.update_or_create(download=download, defaults={"summary_text": summary, "model_name": str(result.get("model_name") or model_name), "source_segment_count": len(segments), "updated_at": timezone.now()})
            log.info("Summary worker generated summary job_id=%s download_id=%s segments=%s chars=%s", job.id, download_id, len(segments), len(summary))
        else:
            log.warning("Summary worker got empty summary job_id=%s download_id=%s", job.id, download_id)
    else:
        log.warning("Summary worker skipped generation with no transcript segments job_id=%s download_id=%s subtitle_path=%s", job.id, download_id, download.subtitle_path)
    original_file_path = str(payload.get("original_file_path") or "").strip()
    if download is not None and original_file_path:
        original_path = Path(original_file_path).expanduser().resolve()
        current_path = Path(str(download.file_path or "")).expanduser().resolve() if download.file_path else None
        if current_path is not None and original_path != current_path:
            original_path.unlink(missing_ok=True)
            log.info(
                "Summary worker deleted pre-transcode original media job_id=%s download_id=%s original=%s current=%s",
                job.id,
                download.id,
                original_path,
                current_path,
            )
        else:
            log.info("Summary worker kept original media because it matches current file job_id=%s download_id=%s path=%s", job.id, download.id, original_path)
    if download is not None:
        log.info("Summary worker finalized media row job_id=%s download_id=%s file_path=%s file_ext=%s", job.id, download.id, download.file_path, download.file_ext)
    return None


def summarize_missing(job: Job) -> None:
    downloads = list(Download.objects.filter(profile_id=job.profile_id, summary__isnull=True).order_by("-last_seen_at")[:100])
    log.info("Summarize-missing fanout started job_id=%s profile_id=%s candidates=%s", job.id, job.profile_id, len(downloads))
    enqueued = 0
    for download in downloads:
        child = create_job(
            profile_id=job.profile_id,
            job_type="generate_summary",
            payload={"download_id": download.id},
            idempotency_key=f"generate_summary:{job.profile_id}:{download.id}",
        )
        _publish_created_job(child)
        enqueued += 1
    log.info("Summarize-missing fanout finished job_id=%s profile_id=%s enqueued_summary_jobs=%s", job.id, job.profile_id, enqueued)


def sync_media(job: Job) -> None:
    log.info("Sync worker placeholder started job_id=%s profile_id=%s payload=%s", job.id, job.profile_id, job.payload)
    log.info("Sync worker placeholder finished job_id=%s", job.id)
    return None


HANDLERS = {
    "check_for_episodes": check_for_episodes,
    "update_downloads": update_downloads,
    "download_episode": download_episode,
    "download_single": download_single,
    "transcode_media": transcode_media,
    "generate_transcript": generate_transcript,
    "generate_summary": generate_summary,
    "summarize_missing": summarize_missing,
    "sync_media": sync_media,
}
