import hashlib
import importlib
import os
import re
import subprocess
import threading
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from django.db import IntegrityError, transaction
from django.utils import timezone

from frontend.queue import publish_job
from models.domain import DownloadStatus, JobStatus, SourceType, parse_str_enum
from models.jobs import create_job
from models.models import (
    AppConfigValue,
    Download,
    Job,
    ProfileConfigValue,
    SourceConfig,
    TranscriptSegment,
)
from workers.content_filter import (
    delete_media_artifacts,
    log_filtered_deletion,
    screen_transcript,
)
from workers.logger import get_logger
from workers.media_fetch import ensure_local_media
from workers.pdf_ocr import PdfOcrPage, extract_pdf_pages, split_sentences
from workers.subtitles import create_subtitles
from workers.utils import sanitize_channel_name
from workers.ytdlp_helpers import (
    apply_ytdlp_player_js_variant_workaround,
    enable_youtube_quickjs_remote_component,
    resolve_youtube_source_name,
)

log = get_logger("workers.handlers")

_YTDLP_TITLE_FILENAME_BYTES = 160
_YTDLP_ID_FILENAME_BYTES = 48


@dataclass(frozen=True)
class SkippedSourceFile:
    path: str
    reason: str


@dataclass(frozen=True)
class DownloadFfmpegRequirement:
    requires_ffmpeg: bool
    media_kind: str
    target_ext: str

    def __iter__(self):
        yield self.requires_ffmpeg
        yield self.media_kind
        yield self.target_ext


@dataclass(frozen=True)
class DownloadedMediaFfmpegRequirement:
    requires_ffmpeg: bool
    target_ext: str

    def __iter__(self):
        yield self.requires_ffmpeg
        yield self.target_ext


def _youtube_dl_class():
    module_name = os.getenv("GETOFFLINE_YTDLP_MODULE", "yt_dlp")
    return importlib.import_module(module_name).YoutubeDL


def _touch_active_job(job: Job, *, stage: str, title: str = "") -> None:
    """Refresh a running job heartbeat used by the library active marquee."""
    now = timezone.now()
    if job.updated_at and (now - job.updated_at).total_seconds() < 10:
        return
    payload = dict(job.payload) if isinstance(job.payload, dict) else {}
    payload["active_stage"] = stage
    if title:
        payload["active_title"] = title
    job.payload = payload
    job.updated_at = now
    job.save(update_fields=["payload", "updated_at"])


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
        log.info(
            "yt-dlp download finished filename=%s total_mb=%s",
            filename,
            mb(total or downloaded),
        )
    elif status == "error":
        log.error("yt-dlp download error filename=%s event=%s", filename, event)
    else:
        log.info(
            "yt-dlp progress status=%s filename=%s event=%s", status, filename, event
        )


def _yt_dlp_verbose_enabled() -> bool:
    return str(os.getenv("GETOFFLINE_YTDLP_VERBOSE", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


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


def _yt_dlp_download_outtmpl(output_dir: Path) -> str:
    """Return a yt-dlp output template that keeps one filename component short.

    Some podcast CDNs expose the entire signed media URL query string as the
    extractor id.  Including that id unbounded in the output template can push
    the temporary ``.part`` filename over the common 255-byte filesystem
    component limit before yt-dlp can finish the download.
    """
    return str(
        output_dir
        / (
            f"%(title).{_YTDLP_TITLE_FILENAME_BYTES}B "
            f"[%(id).{_YTDLP_ID_FILENAME_BYTES}B].%(ext)s"
        )
    )


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
    root = _profile_setting(profile_id, "output_root", f"./downloads/{profile_id}")
    return Path(root).expanduser().absolute()


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
    if parse_str_enum(SourceType, download.source_type) is SourceType.PODCAST:
        return "audio"
    return (
        "video"
        if (download.file_ext or "").lower() in {"mp4", "mkv", "webm", "mov"}
        else "audio"
    )


def _source_config_type(source: SourceConfig) -> SourceType | None:
    return parse_str_enum(SourceType, source.source_type)


def _delete_ffmpeg_source_files(
    source_paths: Iterable[Path], target_path: Path
) -> list[Path]:
    """Delete original FFmpeg input files once the converted output is ready."""
    resolved_target = Path(target_path).expanduser().resolve()
    source_path_list = list(source_paths)
    log.info(
        "FFmpeg source cleanup starting target=%s source_count=%s sources=%s",
        resolved_target,
        len(source_path_list),
        [str(path) for path in source_path_list],
    )
    deleted: list[Path] = []
    skipped: list[SkippedSourceFile] = []
    for source_path in source_path_list:
        candidate = Path(source_path).expanduser().resolve()
        exists = candidate.exists()
        is_file = candidate.is_file() if exists else False
        same_as_target = candidate == resolved_target
        size_bytes = candidate.stat().st_size if exists and is_file else None
        log.info(
            "FFmpeg source cleanup inspecting source=%s resolved=%s target=%s exists=%s is_file=%s same_as_target=%s size_bytes=%s",
            source_path,
            candidate,
            resolved_target,
            exists,
            is_file,
            same_as_target,
            size_bytes,
        )
        if same_as_target:
            skipped.append(SkippedSourceFile(str(candidate), "matches-target"))
            log.info(
                "FFmpeg source cleanup skipped source=%s reason=matches-target",
                candidate,
            )
            continue
        if not exists:
            skipped.append(SkippedSourceFile(str(candidate), DownloadStatus.MISSING))
            log.warning(
                "FFmpeg source cleanup skipped source=%s reason=missing",
                candidate,
            )
            continue
        if not is_file:
            skipped.append(SkippedSourceFile(str(candidate), "not-file"))
            log.warning(
                "FFmpeg source cleanup skipped source=%s reason=not-file",
                candidate,
            )
            continue
        try:
            log.info(
                "FFmpeg source cleanup deleting source=%s size_bytes=%s target=%s",
                candidate,
                size_bytes,
                resolved_target,
            )
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            log.warning(
                "FFmpeg source cleanup failed source=%s target=%s error=%s",
                candidate,
                resolved_target,
                exc,
                exc_info=True,
            )
            continue
        exists_after = candidate.exists()
        if exists_after:
            log.warning(
                "FFmpeg source cleanup delete returned but source still exists source=%s target=%s",
                candidate,
                resolved_target,
            )
            continue
        deleted.append(candidate)
        log.info(
            "FFmpeg source cleanup deleted source=%s target=%s",
            candidate,
            resolved_target,
        )
    log.info(
        "FFmpeg source cleanup finished target=%s deleted=%s skipped=%s",
        resolved_target,
        [str(path) for path in deleted],
        [asdict(item) for item in skipped],
    )
    return deleted


def _target_path(source_path: Path, target_ext: str) -> Path:
    clean_ext = target_ext.lower().lstrip(".") or source_path.suffix.lstrip(".")
    candidate = source_path.with_name(f"{source_path.stem}.converted.{clean_ext}")
    counter = 1
    while candidate.exists() and candidate != source_path:
        candidate = source_path.with_name(
            f"{source_path.stem}.converted-{counter}.{clean_ext}"
        )
        counter += 1
    return candidate


def _preferred_target_ext(profile_id: str, media_kind: str) -> str:
    return (
        _profile_setting(
            profile_id,
            "audio_format" if media_kind == "audio" else "video_format",
            "mp3" if media_kind == "audio" else "mp4",
        )
        .strip()
        .lower()
    )


def _download_requires_ffmpeg(
    download: Download, payload: dict
) -> DownloadFfmpegRequirement:
    media_kind = _preferred_media_kind(download, payload)
    target_ext = _preferred_target_ext(download.profile_id, media_kind)
    current_ext = (
        (download.file_ext or Path(str(download.file_path or "")).suffix.lstrip("."))
        .strip()
        .lower()
    )
    if media_kind == "video":
        codec = (
            _profile_setting(download.profile_id, "video_codec", "h264").strip().lower()
        )
        requires_ffmpeg = current_ext != target_ext or codec not in {"copy", ""}
    else:
        requires_ffmpeg = current_ext != target_ext
    return DownloadFfmpegRequirement(requires_ffmpeg, media_kind, target_ext)


def _downloaded_media_requires_ffmpeg(
    *, profile_id: str, media_kind: str, current_ext: str, input_count: int
) -> DownloadedMediaFfmpegRequirement:
    target_ext = (
        "mp3"
        if media_kind == "audio"
        else _preferred_target_ext(profile_id, media_kind)
    )
    normalized_ext = str(current_ext or "").strip().lower().lstrip(".")
    if media_kind == "video":
        codec = _profile_setting(profile_id, "video_codec", "h264").strip().lower()
        return DownloadedMediaFfmpegRequirement(
            input_count > 1
            or normalized_ext != target_ext
            or codec
            not in {
                "copy",
                "",
            },
            target_ext,
        )
    return DownloadedMediaFfmpegRequirement(normalized_ext != target_ext, target_ext)


def _ffmpeg_thread_count(profile_id: str) -> str:
    raw_value = _profile_setting(profile_id, "ffmpeg_threads", "1").strip() or "1"
    try:
        count = max(1, int(raw_value))
    except ValueError:
        log.warning("Ignoring invalid ffmpeg_threads=%r; using 1", raw_value)
        count = 1
    return str(count)


def _ffmpeg_audio_args(profile_id: str, target_ext: str) -> list[str]:
    quality = _profile_setting(profile_id, "audio_quality", "0").strip() or "0"
    audio_filter = _profile_setting(profile_id, "ffmpeg_audio_filter", "").strip()
    args = ["-vn", "-threads", _ffmpeg_thread_count(profile_id)]
    if target_ext == "mp3":
        args.extend(["-codec:a", "libmp3lame", "-q:a", quality])
    elif target_ext == "opus":
        args.extend(["-codec:a", "libopus", "-b:a", "96k"])
    else:
        args.extend(["-codec:a", "aac", "-b:a", "192k"])
    if audio_filter:
        args.extend(["-af", audio_filter])
    return args


def _ffmpeg_video_args(
    profile_id: str, target_ext: str, *, input_count: int = 1
) -> list[str]:
    codec = _profile_setting(profile_id, "video_codec", "h264").strip().lower()
    if input_count > 1:
        args = [
            "-map",
            "0:v:0?",
            "-map",
            "1:a:0?",
            "-map",
            "0:s?",
            "-c:s",
            "mov_text" if target_ext == "mp4" else "copy",
        ]
    else:
        args = [
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-map",
            "0:s?",
            "-c:s",
            "mov_text" if target_ext == "mp4" else "copy",
        ]
    if codec == "copy":
        args.extend(["-c:v", "copy"])
    elif codec in {"h264", "avc"}:
        args.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "25",
                "-pix_fmt",
                "yuv420p",
            ]
        )
    else:
        args.extend(
            ["-c:v", "libx265", "-tag:v", "hvc1", "-crf", "28", "-preset", "medium"]
        )
    args.extend(
        [
            "-threads",
            _ffmpeg_thread_count(profile_id),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-ar",
            "48000",
        ]
    )
    if target_ext == "mp4":
        args.extend(["-movflags", "+faststart"])
    return args


def _tail_text(value: str | None, *, limit: int = 4000) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"...{text[-limit:]}"


def _transcode_lock_key(profile_id: str, payload: dict) -> str:
    digest = hashlib.sha1(
        _transcode_idempotency_key(profile_id, payload).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()
    return f"transcode_lock:{digest}"


def _transcode_lock_expires_at(lease_seconds: float):
    return timezone.now() + timezone.timedelta(seconds=float(lease_seconds))


def _ensure_app_config_row(key: str) -> None:
    try:
        AppConfigValue.objects.get_or_create(
            key=key, defaults={"value": "", "updated_at": timezone.now()}
        )
    except IntegrityError:
        log.debug("App config row already exists for key=%s", key)


@contextmanager
def _transcode_execution_lock(
    *, profile_id: str, payload: dict, job_id: int | str, lease_seconds: float = 300.0
):
    """Lease a per-target FFmpeg lock so duplicate jobs cannot run together."""
    key = _transcode_lock_key(profile_id, payload)
    owner = str(job_id)
    while True:
        _ensure_app_config_row(key)
        with transaction.atomic():
            row = AppConfigValue.objects.select_for_update().get(key=key)
            current_owner, _, expires_at_text = str(row.value or "").partition("|")
            try:
                expires_at = (
                    datetime.fromisoformat(expires_at_text)
                    if expires_at_text
                    else timezone.now() - timezone.timedelta(seconds=1)
                )
                if timezone.is_naive(expires_at):
                    expires_at = timezone.make_aware(
                        expires_at, timezone.get_current_timezone()
                    )
            except ValueError:
                expires_at = timezone.now() - timezone.timedelta(seconds=1)
            if (
                not current_owner
                or current_owner == owner
                or expires_at <= timezone.now()
            ):
                row.value = (
                    f"{owner}|{_transcode_lock_expires_at(lease_seconds).isoformat()}"
                )
                row.updated_at = timezone.now()
                row.save(update_fields=["value", "updated_at"])
                log.info(
                    "FFmpeg target lock acquired job_id=%s lock_key=%s previous_owner=%s",
                    owner,
                    key,
                    current_owner or "none",
                )
                break
            log.info(
                "FFmpeg target lock waiting job_id=%s lock_key=%s owner=%s expires_at=%s",
                owner,
                key,
                current_owner,
                expires_at,
            )
        time.sleep(1.0)
    stop = False

    def heartbeat() -> None:
        while not stop:
            time.sleep(max(1.0, lease_seconds / 3.0))
            if stop:
                return
            with transaction.atomic():
                row = AppConfigValue.objects.select_for_update().get(key=key)
                current_owner, _, _expires_at_text = str(row.value or "").partition("|")
                if current_owner == owner:
                    row.value = f"{owner}|{_transcode_lock_expires_at(lease_seconds).isoformat()}"
                    row.updated_at = timezone.now()
                    row.save(update_fields=["value", "updated_at"])

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop = True
        with transaction.atomic():
            row = AppConfigValue.objects.select_for_update().get(key=key)
            current_owner, _, _expires_at_text = str(row.value or "").partition("|")
            if current_owner == owner:
                row.value = ""
                row.updated_at = timezone.now()
                row.save(update_fields=["value", "updated_at"])
                log.info(
                    "FFmpeg target lock released job_id=%s lock_key=%s", owner, key
                )


def _postprocess_download_with_ffmpeg(
    *, profile_id: str, payload: dict, parent_job_id: int | str = "inline"
) -> Download | None:
    """Run FFmpeg post-processing inside the FFmpeg worker and update/create the Download row."""
    payload = payload if isinstance(payload, dict) else {}
    log.info(
        "FFmpeg worker post-processing received parent_job_id=%s profile_id=%s payload=%s",
        parent_job_id,
        profile_id,
        payload,
    )
    download_id = payload.get("download_id")
    download = (
        Download.objects.filter(pk=download_id, profile_id=profile_id).first()
        if download_id
        else None
    )
    deferred_lookup = (
        payload.get("download_lookup")
        if isinstance(payload.get("download_lookup"), dict)
        else None
    )
    deferred_defaults = (
        payload.get("download_defaults")
        if isinstance(payload.get("download_defaults"), dict)
        else None
    )
    if download is None and not (
        deferred_lookup and deferred_defaults and payload.get("source_file_path")
    ):
        log.warning(
            "FFmpeg worker post-processing skipped missing download parent_job_id=%s download_id=%s",
            parent_job_id,
            download_id,
        )
        return
    source_paths_payload = (
        payload.get("source_file_paths")
        if isinstance(payload.get("source_file_paths"), list)
        else None
    )
    if source_paths_payload:
        source_paths = [
            Path(str(path)).expanduser().absolute() for path in source_paths_payload
        ]
        source_path = source_paths[0]
    else:
        source_path = (
            Path(
                str(
                    download.file_path
                    if download is not None
                    else payload.get("source_file_path")
                )
            )
            .expanduser()
            .absolute()
        )
        source_paths = [source_path]
    log.info(
        "FFmpeg worker post-processing loaded download parent_job_id=%s download_id=%s title=%s source_type=%s source_name=%s file_path=%s file_ext=%s db_size_bytes=%s status=%s",
        parent_job_id,
        download.id if download is not None else "deferred",
        download.title if download is not None else deferred_defaults.get("title"),
        (
            download.source_type
            if download is not None
            else deferred_lookup.get("source_type")
        ),
        (
            download.source_name
            if download is not None
            else deferred_lookup.get("source_name")
        ),
        download.file_path if download is not None else payload.get("source_file_path"),
        (
            download.file_ext
            if download is not None
            else Path(str(payload.get("source_file_path"))).suffix.lstrip(".")
        ),
        (
            download.file_size_bytes
            if download is not None
            else deferred_defaults.get("file_size_bytes")
        ),
        download.download_status if download is not None else "deferred_insert",
    )
    media_kind = (
        _preferred_media_kind(download, payload)
        if download is not None
        else str(payload.get("media_type") or "video")
    )
    target_ext = (
        "mp3"
        if media_kind == "audio"
        else _preferred_target_ext(profile_id, media_kind)
    )
    target_path = (
        (
            Path(str(payload.get("target_file_path"))).expanduser().resolve()
            if download is not None
            else Path(str(payload.get("target_file_path"))).expanduser().absolute()
        )
        if payload.get("target_file_path")
        else _target_path(source_path, target_ext)
    )
    if download is not None:
        target_path = target_path.resolve()
    with _transcode_execution_lock(
        profile_id=profile_id, payload=payload, job_id=parent_job_id
    ):
        if download_id:
            refreshed_download = Download.objects.filter(
                pk=download_id, profile_id=profile_id
            ).first()
            if (
                refreshed_download is not None
                and Path(str(refreshed_download.file_path or "")).expanduser().resolve()
                == target_path
                and target_path.exists()
            ):
                log.info(
                    "FFmpeg duplicate skipped because target is already current job_id=%s download_id=%s target=%s",
                    parent_job_id,
                    download_id,
                    target_path,
                )
                return refreshed_download
        missing_paths = [path for path in source_paths if not path.exists()]
        if missing_paths and target_path.exists() and download is not None:
            log.info(
                "FFmpeg duplicate skipped because target exists and source is gone job_id=%s download_id=%s target=%s missing_sources=%s",
                parent_job_id,
                download.id,
                target_path,
                missing_paths,
            )
            return download
        if missing_paths:
            if download is not None:
                source_paths = [
                    ensure_local_media(download, path) if not path.is_file() else path
                    for path in source_paths
                ]
                source_path = source_paths[0]
                if not payload.get("target_file_path"):
                    target_path = _target_path(source_path, target_ext)
                missing_paths = [path for path in source_paths if not path.is_file()]
            if not missing_paths:
                log.info(
                    "FFmpeg worker fetched missing input through API job_id=%s download_id=%s paths=%s",
                    parent_job_id,
                    download.id if download is not None else "deferred",
                    source_paths,
                )
        if missing_paths:
            log.error(
                "FFmpeg worker post-processing input file is missing parent_job_id=%s download_id=%s paths=%s",
                parent_job_id,
                download.id if download is not None else "deferred",
                missing_paths,
            )
            raise FileNotFoundError(f"Downloaded file is missing: {missing_paths[0]}")
        input_size = sum(path.stat().st_size for path in source_paths)
        ffmpeg_path = _profile_setting(profile_id, "ffmpeg_path", "ffmpeg")
        codec_args = (
            _ffmpeg_audio_args(profile_id, target_ext)
            if media_kind == "audio"
            else _ffmpeg_video_args(
                profile_id, target_ext, input_count=len(source_paths)
            )
        )
        ffmpeg_threads = _ffmpeg_thread_count(profile_id)
        input_args = [
            arg
            for path in source_paths
            for arg in ("-threads", ffmpeg_threads, "-i", str(path))
        ]
        command = [
            ffmpeg_path,
            "-y",
            "-filter_threads",
            ffmpeg_threads,
            "-filter_complex_threads",
            ffmpeg_threads,
            *input_args,
            *codec_args,
            str(target_path),
        ]
        log.info(
            "FFmpeg conversion prepared job_id=%s download_id=%s media_kind=%s input=%s input_size_bytes=%s target=%s target_ext=%s ffmpeg_path=%s codec_args=%s",
            parent_job_id,
            download.id if download is not None else "deferred",
            media_kind,
            source_paths if len(source_paths) > 1 else source_path,
            input_size,
            target_path,
            target_ext,
            ffmpeg_path,
            codec_args,
        )
        log.info(
            "Downloader FFmpeg conversion starting parent_job_id=%s download_id=%s command=%s",
            parent_job_id,
            download.id if download is not None else "deferred",
            command,
        )
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            log.error(
                "FFmpeg conversion failed job_id=%s download_id=%s returncode=%s stdout_tail=%s stderr_tail=%s",
                parent_job_id,
                download.id if download is not None else "deferred",
                exc.returncode,
                _tail_text(exc.stdout),
                _tail_text(exc.stderr),
            )
            raise
        log.info(
            "FFmpeg conversion subprocess finished job_id=%s download_id=%s returncode=%s stdout_tail=%s stderr_tail=%s",
            parent_job_id,
            download.id if download is not None else "deferred",
            result.returncode,
            _tail_text(result.stdout, limit=1000),
            _tail_text(result.stderr, limit=1000),
        )
        if not target_path.exists():
            log.error(
                "Downloader FFmpeg conversion output missing parent_job_id=%s download_id=%s target=%s",
                parent_job_id,
                download.id if download is not None else "deferred",
                target_path,
            )
            raise FileNotFoundError(
                f"FFmpeg output file was not created: {target_path}"
            )
        old_path = source_path
        output_size = target_path.stat().st_size
        output_root = (
            Path(str(payload.get("output_root") or _download_output_root(profile_id)))
            .expanduser()
            .absolute()
        )
        log.info(
            "FFmpeg conversion updating database job_id=%s download_id=%s old_path=%s new_path=%s old_size_bytes=%s new_size_bytes=%s",
            parent_job_id,
            download.id if download is not None else "deferred",
            old_path,
            target_path,
            input_size,
            output_size,
        )
        if (
            download is None
            and media_kind == "video"
            and bool(payload.get("delete_explicit_content", False))
        ):
            child = create_job(
                profile_id=profile_id,
                job_type="generate_transcript",
                payload={
                    "deferred_download_lookup": deferred_lookup,
                    "deferred_download_defaults": {
                        **deferred_defaults,
                        "file_path": str(target_path),
                        "file_path_relative": (
                            str(target_path.relative_to(output_root))
                            if target_path.is_relative_to(output_root)
                            else None
                        ),
                        "file_ext": target_path.suffix.lstrip("."),
                        "file_size_bytes": output_size,
                        "download_status": DownloadStatus.DOWNLOADED,
                        "completed_at": timezone.now().isoformat(),
                        "last_seen_at": timezone.now().isoformat(),
                    },
                    "deferred_media_path": str(target_path),
                    "ffmpeg_source_file_paths": [str(path) for path in source_paths],
                    "subtitles": payload.get("subtitles", True),
                    "subtitle_offset_seconds": payload.get("subtitle_offset_seconds"),
                    "source_type": deferred_lookup.get("source_type"),
                    "media_type": media_kind,
                    "recent_download": True,
                    "delete_explicit_content": True,
                },
                idempotency_key=(
                    f"generate_transcript:{profile_id}:deferred:"
                    f"{deferred_lookup.get('source_type')}:"
                    f"{deferred_lookup.get('source_name')}:"
                    f"{deferred_lookup.get('item_uid')}"
                ),
            )
            _publish_created_job(child)
            log.info(
                "FFmpeg worker queued deferred transcript screening job parent_job_id=%s child_job_id=%s target=%s",
                parent_job_id,
                child.id,
                target_path,
            )
            return None
        if download is None:
            final_defaults = dict(deferred_defaults)
            final_defaults.update(
                {
                    "file_path": str(target_path),
                    "file_path_relative": (
                        str(target_path.relative_to(output_root))
                        if target_path.is_relative_to(output_root)
                        else None
                    ),
                    "file_ext": target_path.suffix.lstrip("."),
                    "file_size_bytes": output_size,
                    "download_status": DownloadStatus.DOWNLOADED,
                    "completed_at": timezone.now(),
                    "last_seen_at": timezone.now(),
                }
            )
            download, _created = Download.objects.update_or_create(
                **deferred_lookup, defaults=final_defaults
            )
        else:
            download.file_path = str(target_path)
            download.file_path_relative = (
                str(target_path.relative_to(output_root))
                if target_path.is_relative_to(output_root)
                else None
            )
            download.file_ext = target_path.suffix.lstrip(".")
            download.file_size_bytes = output_size
            download.download_status = DownloadStatus.DOWNLOADED
            download.completed_at = timezone.now()
            download.last_seen_at = timezone.now()
            download.save(
                update_fields=[
                    "file_path",
                    "file_path_relative",
                    "file_ext",
                    "file_size_bytes",
                    "download_status",
                    "completed_at",
                    "last_seen_at",
                ]
            )
        deleted_sources = _delete_ffmpeg_source_files(source_paths, target_path)
        log.info(
            "FFmpeg conversion finished job_id=%s download_id=%s output=%s output_size_bytes=%s deleted_original_files=%s",
            parent_job_id,
            download.id,
            target_path,
            output_size,
            [str(path) for path in deleted_sources],
        )
        download._ffmpeg_original_file_path = ""
        return download


def transcode_media(job: Job) -> None:
    """Legacy compatibility: convert a queued FFmpeg job, then enqueue transcript work."""
    payload = job.payload if isinstance(job.payload, dict) else {}
    _touch_active_job(job, stage="ffmpeg_conversion")
    log.info(
        "Legacy FFmpeg job received job_id=%s profile_id=%s payload=%s",
        job.id,
        job.profile_id,
        payload,
    )
    download = _postprocess_download_with_ffmpeg(
        profile_id=job.profile_id, payload=payload, parent_job_id=job.id
    )
    if download is None:
        log.info(
            "Legacy FFmpeg job stopped before transcript queue parent_job_id=%s reason=postprocess-returned-none",
            job.id,
        )
        return
    media_kind = (
        str(payload.get("media_type") or _preferred_media_kind(download, payload))
        .strip()
        .lower()
    )
    child = create_job(
        profile_id=job.profile_id,
        job_type="generate_transcript",
        payload={
            "download_id": download.id,
            "subtitles": payload.get("subtitles", True),
            "subtitle_offset_seconds": payload.get("subtitle_offset_seconds"),
            "source_type": download.source_type,
            "media_type": media_kind,
            "recent_download": True,
            "delete_explicit_content": bool(
                payload.get("delete_explicit_content", False)
            ),
        },
        idempotency_key=f"generate_transcript:{job.profile_id}:{download.id}",
    )
    _publish_created_job(child)
    log.info(
        "Legacy FFmpeg job queued transcript job parent_job_id=%s download_id=%s child_job_id=%s",
        job.id,
        download.id,
        child.id,
    )


def _find_downloaded_files(info: dict, ydl) -> list[Path]:
    files: list[Path] = []
    candidate_groups = (
        [info.get("requested_downloads"), info.get("requested_formats")]
        if isinstance(info, dict)
        else []
    )
    for requested in candidate_groups:
        if isinstance(requested, list):
            for item in requested:
                if isinstance(item, dict):
                    candidate = item.get("filepath") or item.get("filename")
                    if candidate and Path(candidate).exists():
                        path = Path(candidate).expanduser().resolve()
                        if path not in files:
                            files.append(path)
    merge_files = info.get("__files_to_merge") if isinstance(info, dict) else None
    if isinstance(merge_files, list):
        for candidate in merge_files:
            if candidate and Path(candidate).exists():
                path = Path(candidate).expanduser().resolve()
                if path not in files:
                    files.append(path)
    for key in ("filepath", "_filename", "filename"):
        candidate = info.get(key) if isinstance(info, dict) else None
        if candidate and Path(candidate).exists():
            path = Path(candidate).expanduser().resolve()
            if path not in files:
                files.append(path)
    prepared = ydl.prepare_filename(info) if isinstance(info, dict) else ""
    if prepared and Path(prepared).exists():
        path = Path(prepared).expanduser().resolve()
        if path not in files:
            files.append(path)
    return files


def _find_downloaded_file(info: dict, ydl) -> Path | None:
    files = _find_downloaded_files(info, ydl)
    return files[0] if files else None


def _is_expected_ytdlp_download_error(exc: Exception) -> bool:
    """Return True for yt-dlp failures caused by unavailable remote media."""
    exc_type = type(exc).__name__
    if exc_type not in {"DownloadError", "ExtractorError"}:
        return False
    message = str(exc).lower()
    expected_fragments = (
        "video unavailable",
        "has been removed",
        "removed by the uploader",
        "private video",
        "this video is unavailable",
        "this video is private",
        "this video has been deleted",
        "account associated with this video has been terminated",
    )
    return any(fragment in message for fragment in expected_fragments)


def _is_youtube_video_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return (
        "youtube.com/watch" in lowered
        or "youtu.be/" in lowered
        or "youtube.com/shorts/" in lowered
    )


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


def _download_request_from_payload(
    job: Job, payload: dict
) -> tuple[str, SourceType, str] | None:
    download_url = str(
        payload.get("media_url") or payload.get("item_url") or payload.get("url") or ""
    ).strip()
    if not download_url:
        log.warning(
            "Download worker skipped job with no URL job_id=%s payload=%s",
            job.id,
            payload,
        )
        return None
    source_type = (
        parse_str_enum(SourceType, payload.get("source_type")) or SourceType.YOUTUBE
    )
    source_name = str(payload.get("source_name") or "").strip()
    if not source_name and source_type is SourceType.YOUTUBE:
        try:
            source_name = resolve_youtube_source_name(download_url)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Could not resolve YouTube channel name for direct download job_id=%s url=%s: %s",
                job.id,
                download_url,
                exc,
            )
    source_name = source_name or str(payload.get("source_type") or "GetOffline").strip()
    if source_type is SourceType.YOUTUBE and not _is_youtube_video_url(download_url):
        fallback_uid = str(payload.get("item_uid") or "").strip()
        if len(fallback_uid) != 11:
            log.warning(
                "Download worker skipped non-video YouTube URL job_id=%s url=%s payload=%s",
                job.id,
                download_url,
                payload,
            )
            return None
        download_url = f"https://www.youtube.com/watch?v={fallback_uid}"
        log.info(
            "Downloader converted YouTube item uid to video URL job_id=%s item_uid=%s url=%s",
            job.id,
            fallback_uid,
            download_url,
        )
    return download_url, source_type, source_name


def _yt_dlp_options_for_download(
    *,
    job: Job,
    payload: dict,
    source_type: SourceType,
    outtmpl: str,
    progress_hook,
) -> dict:
    ydl_opts = _yt_dlp_base_options(
        outtmpl=outtmpl,
        continuedl=True,
        retries=3,
        fragment_retries=3,
        noplaylist=True,
        playlist_items="1",
        playlistend=1,
        progress_hooks=[progress_hook],
    )
    max_height = _profile_setting(
        job.profile_id, "ytdlp_video_max_height", "720"
    ).strip()
    requested_media_type = (
        str(
            payload.get("media_type")
            or ("audio" if source_type is SourceType.PODCAST else "video")
        )
        .strip()
        .lower()
    )
    if (
        source_type is SourceType.YOUTUBE
        and requested_media_type != "audio"
        and max_height.isdigit()
    ):
        ydl_opts["format"] = (
            f"bv*[height<={max_height}]+ba/b[height<={max_height}]/best[height<={max_height}]/best"
        )
        # Download selected elementary streams only. Point yt-dlp at a deliberately
        # absent ffmpeg binary so it downloads separate files and leaves merge/transcode
        # work to the downloader's FFmpeg post-processing without enabling
        # yt-dlp's unplayable-format mode.
        ydl_opts["ffmpeg_location"] = "/usr/bin/ffmpeg"
        ydl_opts["ignoreerrors"] = True
    if source_type is SourceType.YOUTUBE:
        _configure_youtube_download_filters(ydl_opts, job, payload)
    return ydl_opts


def _configure_youtube_download_filters(
    ydl_opts: dict, job: Job, payload: dict
) -> None:
    include_shorts = bool(payload.get("include_shorts", False))
    include_livestreams = bool(payload.get("include_livestreams", False))

    def skip_unwanted_youtube_entries(info_dict, *, incomplete=False):
        _ = incomplete
        if not include_livestreams and _is_youtube_livestream_entry(info_dict):
            return "Skipping YouTube livestream entry from source."
        if not include_shorts and _is_youtube_short_entry(info_dict):
            return "Skipping YouTube Shorts entry from source."
        return None

    ydl_opts["match_filter"] = skip_unwanted_youtube_entries
    if not include_shorts:
        ydl_opts.setdefault("extractor_args", {}).setdefault("youtube", {})["skip"] = [
            "shorts"
        ]
    enable_youtube_quickjs_remote_component(
        ydl_opts,
        f"download job {job.id}",
        _profile_setting(job.profile_id, "js_runtime_path", "qjs"),
    )
    apply_ytdlp_player_js_variant_workaround(ydl_opts)


def _download_with_yt_dlp(job: Job, payload: dict) -> Download | dict | None:
    download_request = _download_request_from_payload(job, payload)
    if download_request is None:
        return None
    download_url, source_type, source_name = download_request
    output_root = _download_output_root(job.profile_id)
    output_dir = output_root / sanitize_channel_name(source_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = _yt_dlp_download_outtmpl(output_dir)
    downloaded_files_from_hooks: list[Path] = []

    def remember_finished_download(event: dict) -> None:
        _yt_dlp_progress_hook(event)
        info_dict = (
            event.get("info_dict") if isinstance(event.get("info_dict"), dict) else {}
        )
        status = event.get("status")
        if status == "downloading":
            _touch_active_job(
                job,
                stage="downloading",
                title=str(
                    info_dict.get("title") or payload.get("title") or download_url
                ).strip(),
            )
        if status != "finished":
            return
        candidate = event.get("filename") or event.get("tmpfilename")
        if candidate and Path(candidate).exists():
            path = Path(candidate).expanduser().resolve()
            if path not in downloaded_files_from_hooks:
                downloaded_files_from_hooks.append(path)

    ydl_opts = _yt_dlp_options_for_download(
        job=job,
        payload=payload,
        source_type=source_type,
        outtmpl=outtmpl,
        progress_hook=remember_finished_download,
    )

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
    with _youtube_dl_class()(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(download_url, download=True) or {}
        except Exception as exc:
            if _is_expected_ytdlp_download_error(exc):
                log.warning(
                    "yt-dlp skipped unavailable media job_id=%s url=%s error=%s",
                    job.id,
                    download_url,
                    exc,
                )
                return None
            raise
        if isinstance(info, dict):
            _log_youtube_response("yt-dlp download response", info)
            downloaded_files = _find_downloaded_files(info, ydl)
            for path in downloaded_files_from_hooks:
                if path not in downloaded_files:
                    downloaded_files.append(path)
            downloaded_file = downloaded_files[0] if downloaded_files else None
        else:
            downloaded_files = []
            downloaded_file = None
    if downloaded_file is None:
        log.warning(
            "yt-dlp download finished but file path was not found job_id=%s url=%s",
            job.id,
            download_url,
        )
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
        "file_path_relative": (
            str(downloaded_file.relative_to(output_root))
            if downloaded_file.is_relative_to(output_root)
            else None
        ),
        "file_ext": downloaded_file.suffix.lstrip("."),
        "file_size_bytes": (
            downloaded_file.stat().st_size if downloaded_file.exists() else None
        ),
        "download_status": DownloadStatus.DOWNLOADED,
        "last_seen_at": now,
        "completed_at": now,
    }
    media_kind = (
        str(
            payload.get("media_type")
            or ("audio" if source_type is SourceType.PODCAST else "video")
        )
        .strip()
        .lower()
    )
    # yt-dlp can report both the final merged/downloaded file and the temporary
    # elementary stream files that were used to create it. After yt-dlp finishes,
    # those temporary .fXXX files may already be removed, so do not pass them to
    # our FFmpeg step unless they still exist and we are intentionally
    # doing a video stream merge. Audio extraction/conversion should always use
    # the final downloaded media file as the single input.
    if media_kind == "audio":
        ffmpeg_input_files = [downloaded_file]
    else:
        existing_downloaded_files = [path for path in downloaded_files if path.exists()]
        ffmpeg_input_files = (
            existing_downloaded_files
            if len(existing_downloaded_files) > 1
            else [downloaded_file]
        )

    requires_ffmpeg, final_ext = _downloaded_media_requires_ffmpeg(
        profile_id=job.profile_id,
        media_kind=media_kind,
        current_ext=downloaded_file.suffix.lstrip("."),
        input_count=len(ffmpeg_input_files),
    )

    if requires_ffmpeg:
        target_file_path = (
            str(output_dir / f"{downloaded_file.stem.split('.f')[0]}.{final_ext}")
            if media_kind == "video"
            and (
                len(ffmpeg_input_files) > 1
                or downloaded_file.suffix.lstrip(".").lower() != final_ext
            )
            else ""
        )
        transcode_payload = {
            "source_file_path": str(downloaded_file),
            "source_file_paths": [str(path) for path in ffmpeg_input_files],
            "target_file_path": target_file_path,
            "output_root": str(output_root),
            "media_type": media_kind,
            "subtitles": payload.get("subtitles", True),
            "subtitle_offset_seconds": payload.get("subtitle_offset_seconds"),
            "source_type": source_type,
            "recent_download": True,
            "delete_explicit_content": bool(
                payload.get("delete_explicit_content", False)
            ),
            "item_uid": item_uid,
        }
        if media_kind == "video" and not transcode_payload["delete_explicit_content"]:
            download, _created = Download.objects.update_or_create(
                **download_lookup,
                defaults=download_defaults,
            )
            transcode_payload["download_id"] = download.id
            log.info(
                "Download worker saved video row before FFmpeg post-processing job_id=%s download_id=%s source_files=%s current_ext=%s target_ext=%s",
                job.id,
                download.id,
                [str(path) for path in ffmpeg_input_files],
                downloaded_file.suffix.lstrip("."),
                final_ext,
            )
        else:
            transcode_payload.update(
                {
                    "download_lookup": download_lookup,
                    "download_defaults": {
                        key: value
                        for key, value in download_defaults.items()
                        if key not in {"last_seen_at", "completed_at"}
                    },
                }
            )
            log.info(
                "Download worker deferred database insert to FFmpeg service post-processing job_id=%s source_files=%s current_ext=%s target_ext=%s media_kind=%s delete_explicit_content=%s",
                job.id,
                [str(path) for path in ffmpeg_input_files],
                downloaded_file.suffix.lstrip("."),
                final_ext,
                media_kind,
                transcode_payload["delete_explicit_content"],
            )
        return transcode_payload
    if media_kind == "video" and bool(payload.get("delete_explicit_content", False)):
        return _screen_deferred_video_before_insert(
            profile_id=job.profile_id,
            media_path=downloaded_file,
            download_lookup=download_lookup,
            download_defaults=download_defaults,
            payload=payload,
            job_id=job.id,
        )
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
        download_status=DownloadStatus.DOWNLOADED,
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


def _transcode_idempotency_key(profile_id: str, payload: dict) -> str:
    """Return a stable key for one logical FFmpeg conversion target.

    Download workers can be restarted or the same media can be discovered by more
    than one active parent job.  Including the parent job id in this key allows
    duplicate FFmpeg jobs for the same input/output pair to run concurrently, so
    key the operation by the durable download id when available and otherwise by
    the canonical source/target paths.
    """
    download_id = payload.get("download_id")
    if download_id:
        return f"transcode_media:{profile_id}:download:{download_id}"
    source_paths = payload.get("source_file_paths")
    if not isinstance(source_paths, list) or not source_paths:
        source_paths = [payload.get("source_file_path")]
    key_parts = [
        str(Path(str(path)).expanduser().resolve()) for path in source_paths if path
    ]
    key_parts.append(str(payload.get("target_file_path") or ""))
    key_parts.append(str(payload.get("item_uid") or ""))
    digest = hashlib.sha1(
        "|".join(key_parts).encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    return f"transcode_media:{profile_id}:file:{digest}"


def _enqueue_transcode_job(
    *, profile_id: str, payload: dict, parent_job_id: int
) -> Job:
    child = create_job(
        profile_id=profile_id,
        job_type="transcode_media",
        payload=payload,
        idempotency_key=_transcode_idempotency_key(profile_id, payload),
    )
    _publish_created_job(child)
    log.info(
        "Download worker queued FFmpeg job parent_job_id=%s child_job_id=%s payload=%s",
        parent_job_id,
        child.id,
        payload,
    )
    return child


def _publish_created_job(job: Job) -> None:
    log.info(
        "Publishing child job job_id=%s job_type=%s profile_id=%s",
        job.id,
        job.job_type,
        job.profile_id,
    )
    publish_job(
        {
            "job_id": job.id,
            "job_type": job.job_type,
            "profile_id": job.profile_id,
            "attempt": 1,
        }
    )
    log.info(
        "Published child job job_id=%s job_type=%s profile_id=%s",
        job.id,
        job.job_type,
        job.profile_id,
    )


def _fallback_uid(*parts: object) -> str:
    text = "|".join(str(part or "") for part in parts).strip() or "unknown"
    digest = hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"generated:{digest}"


def _idempotency_key(*parts: object) -> str:
    text = "|".join(str(part or "") for part in parts).strip() or "unknown"
    digest = hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()
    prefix = ":".join(str(part or "") for part in parts[:3])[:160]
    return f"{prefix}:{digest}"[:255]


def _episode_was_downloaded(
    *, profile_id: str, source: SourceConfig, item_uid: str, item_url: str, title: str
) -> bool:
    rows = Download.objects.filter(
        profile_id=profile_id, source_type=source.source_type, source_name=source.name
    )
    if item_uid and rows.filter(item_uid=item_uid).exists():
        return True
    if item_url and rows.filter(item_url=item_url).exists():
        return True
    return bool(title and rows.filter(title=title).exists())


def _source_limit(source: SourceConfig) -> int:
    if source.max_downloads:
        limit = max(1, int(source.max_downloads))
        log.info(
            "Using source max downloads source_id=%s source_name=%s limit=%s",
            source.id,
            source.name,
            limit,
        )
        return limit
    profile_default = (
        ProfileConfigValue.objects.filter(
            profile_id=source.profile_id, key="max_downloads"
        )
        .values_list("value", flat=True)
        .first()
    )
    if str(profile_default or "").strip().isdigit():
        return max(1, int(str(profile_default).strip()))
    limit = 10
    log.info(
        "Using fallback max downloads source_id=%s source_name=%s limit=%s",
        source.id,
        source.name,
        limit,
    )
    return limit


def _active_download_job(idempotency_key: str) -> Job | None:
    return (
        Job.objects.filter(
            idempotency_key=idempotency_key,
            status__in=[JobStatus.QUEUED, JobStatus.RUNNING],
        )
        .order_by("created_at", "id")
        .first()
    )


def _stale_running_job_cutoff() -> datetime | None:
    raw_timeout = str(
        os.getenv("GETOFFLINE_STALE_RUNNING_JOB_SECONDS", "21600")
    ).strip()
    if not raw_timeout.isdigit():
        return None
    timeout_seconds = int(raw_timeout)
    if timeout_seconds <= 0:
        return None
    return timezone.now() - timedelta(seconds=timeout_seconds)


def _make_stale_job_queued(job: Job) -> bool:
    if parse_str_enum(JobStatus, job.status) is not JobStatus.RUNNING:
        return False
    cutoff = _stale_running_job_cutoff()
    if cutoff is None:
        return False
    started_at = job.started_at or job.updated_at or job.created_at
    if started_at and started_at > cutoff:
        return False
    job.status = JobStatus.QUEUED
    job.error_message = "Reset stale running job so it can be published again."
    job.started_at = None
    job.finished_at = None
    job.updated_at = timezone.now()
    job.save(
        update_fields=[
            "status",
            "error_message",
            "started_at",
            "finished_at",
            "updated_at",
        ]
    )
    return True


def _podcast_candidates(source: SourceConfig) -> Iterable[dict]:
    log.info(
        "Checking podcast feed source_id=%s source_name=%s url=%s",
        source.id,
        source.name,
        source.url,
    )
    feedparser = importlib.import_module("feedparser")
    feed = feedparser.parse(source.url)
    if getattr(feed, "bozo", False):
        log.warning(
            "Podcast feed parse warning source_id=%s source_name=%s error=%s",
            source.id,
            source.name,
            getattr(feed, "bozo_exception", "unknown"),
        )
    entries = list(getattr(feed, "entries", []) or [])[: _source_limit(source)]
    feed_meta = getattr(feed, "feed", {}) or {}
    feed_title = str(
        getattr(feed_meta, "title", "")
        or getattr(feed_meta, "get", lambda _key, _default="": _default)("title", "")
        or ""
    )
    log.info(
        "Podcast feed parsed source_id=%s source_name=%s feed_title=%s entries_considered=%s limit=%s",
        source.id,
        source.name,
        feed_title,
        len(entries),
        _source_limit(source),
    )
    for entry in entries:
        enclosure_url = ""
        for enclosure in getattr(entry, "enclosures", []) or []:
            enclosure_url = str(
                getattr(enclosure, "href", "") or enclosure.get("href", "")
            ).strip()
            if enclosure_url:
                break
        item_url = enclosure_url or str(getattr(entry, "link", "") or "").strip()
        title = str(
            getattr(entry, "title", "") or item_url or "Untitled podcast episode"
        ).strip()
        published = str(
            getattr(entry, "published", "") or getattr(entry, "updated", "") or ""
        ).strip()
        item_uid = str(
            getattr(entry, "id", "") or getattr(entry, "guid", "") or item_url or ""
        ).strip()
        item_uid = item_uid or _fallback_uid(source.url, title, published)
        log.info(
            "Podcast episode candidate source_id=%s source_name=%s item_uid=%s title=%s media_url=%s published=%s",
            source.id,
            source.name,
            item_uid[:255],
            title,
            enclosure_url or item_url,
            published,
        )
        yield {
            "item_uid": item_uid[:255],
            "item_url": item_url,
            "media_url": enclosure_url or item_url,
            "title": title,
            "published": published,
        }


def _is_youtube_short_entry(entry: dict) -> bool:
    urls = [
        str(entry.get(key) or "")
        for key in ("webpage_url", "original_url", "url", "ie_key")
    ]
    return any("/shorts/" in value for value in urls)


_YOUTUBE_LIVE_TITLE_MARKERS = (
    "🔴",
    " live stream",
    " livestream",
    "| live",
    "- live",
    "[live]",
    "(live)",
)


def _youtube_title_looks_live(title: object) -> bool:
    normalized = f" {str(title or '').strip().lower()} "
    if not normalized.strip():
        return False
    if any(marker in normalized for marker in _YOUTUBE_LIVE_TITLE_MARKERS):
        return True
    return normalized.strip() == "live" or normalized.startswith("live ")


def _is_youtube_livestream_entry(entry: dict) -> bool:
    live_status = str(entry.get("live_status") or "").strip().lower()
    if bool(entry.get("is_live")) or live_status in {
        "is_live",
        "is_upcoming",
        "was_live",
        "post_live",
    }:
        return True
    return _youtube_title_looks_live(entry.get("title"))


def _youtube_source_skip_reason(source: SourceConfig, entry: dict) -> str | None:
    if not getattr(
        source, "include_livestreams", False
    ) and _is_youtube_livestream_entry(entry):
        return "Skipping YouTube livestream entry from source."
    if not getattr(source, "include_shorts", False) and _is_youtube_short_entry(entry):
        return "Skipping YouTube Shorts entry from source."
    return None


def _youtube_entries_from_url(
    url: str, limit: int, *, source: SourceConfig, reason: str
) -> list[dict]:
    ydl_opts = _yt_dlp_base_options(
        extract_flat=True,
        skip_download=True,
        playlistend=limit,
        playlist_items=f"1-{limit}",
    )
    enable_youtube_quickjs_remote_component(
        ydl_opts,
        f"update source {source.name}",
        _profile_setting(source.profile_id, "js_runtime_path", "qjs"),
    )
    apply_ytdlp_player_js_variant_workaround(ydl_opts)
    log.info(
        "yt-dlp extract starting source_id=%s source_name=%s reason=%s url=%s options=%s",
        source.id,
        source.name,
        reason,
        url,
        {k: v for k, v in ydl_opts.items() if k not in {"logger", "progress_hooks"}},
    )
    with _youtube_dl_class()(ydl_opts) as ydl:
        payload = ydl.extract_info(url, download=False) or {}
    if isinstance(payload, dict):
        _log_youtube_response(f"yt-dlp extract response ({reason})", payload)
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not entries:
        entries = [payload]
    return [entry for entry in list(entries or []) if isinstance(entry, dict)]


def _youtube_candidate_from_entry(source: SourceConfig, entry: dict) -> dict | None:
    skip_reason = _youtube_source_skip_reason(source, entry)
    if skip_reason:
        log.info(
            "%s source_id=%s source_name=%s entry_id=%s title=%s",
            skip_reason,
            source.id,
            source.name,
            entry.get("id"),
            entry.get("title"),
        )
        return None
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
    item_uid = (
        item_id if len(item_id) == 11 else item_url or _fallback_uid(source.url, title)
    )
    return {
        "item_uid": item_uid[:255],
        "item_url": item_url,
        "media_url": item_url,
        "title": title,
        "published": str(entry.get("upload_date") or entry.get("timestamp") or ""),
    }


def _youtube_candidates(source: SourceConfig) -> Iterable[dict]:
    log.info(
        "Checking YouTube source source_id=%s source_name=%s url=%s",
        source.id,
        source.name,
        source.url,
    )
    limit = _source_limit(source)
    entries = _youtube_entries_from_url(
        source.url, limit, source=source, reason="source"
    )
    log.info(
        "YouTube source parsed source_id=%s source_name=%s entries=%s limit=%s",
        source.id,
        source.name,
        len(entries),
        limit,
    )
    yielded = 0
    for entry in entries:
        if yielded >= limit:
            break
        if _youtube_source_skip_reason(source, entry):
            log.info(
                "Skipping unwanted YouTube entry source_id=%s source_name=%s entry_id=%s title=%s",
                source.id,
                source.name,
                entry.get("id"),
                entry.get("title"),
            )
            continue
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
            for nested_entry in _youtube_entries_from_url(
                nested_url, remaining, source=source, reason="nested-entry"
            ):
                if yielded >= limit:
                    break
                nested_candidate = _youtube_candidate_from_entry(source, nested_entry)
                if nested_candidate is None:
                    continue
                yielded += 1
                yield nested_candidate


def _candidates_for_source(source: SourceConfig) -> Iterable[dict]:
    parsed_url = urlparse(str(source.url or "").strip())
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        log.warning(
            "Skipping source with invalid URL source_id=%s source_name=%s source_type=%s url=%s",
            source.id,
            source.name,
            source.source_type,
            source.url,
        )
        return []
    source_type = _source_config_type(source)
    if source_type is SourceType.PODCAST:
        return _podcast_candidates(source)
    if source_type is SourceType.YOUTUBE:
        return _youtube_candidates(source)
    log.warning(
        "Unsupported source type for episode check source_id=%s source_type=%s",
        source.id,
        source.source_type,
    )
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
        sources = list(
            SourceConfig.objects.filter(profile_id=profile_id, enabled=True).order_by(
                "position", "id"
            )
        )
        log.info(
            "Episode check profile started job_id=%s profile_id=%s sources=%s",
            job.id,
            profile_id,
            len(sources),
        )
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
                source_type = _source_config_type(source)
                if source_type is SourceType.PODCAST:
                    log.info(
                        "New podcast episode found profile_id=%s source_id=%s source_name=%s item_uid=%s title=%s media_url=%s",
                        profile_id,
                        source.id,
                        source.name,
                        item_uid,
                        title,
                        candidate.get("media_url") or item_url,
                    )
                elif source_type is SourceType.YOUTUBE:
                    log.info(
                        "New YouTube episode found profile_id=%s source_id=%s source_name=%s item_uid=%s title=%s item_url=%s",
                        profile_id,
                        source.id,
                        source.name,
                        item_uid,
                        title,
                        item_url,
                    )
                idempotency_key = _idempotency_key(
                    "download_episode",
                    profile_id,
                    source.id,
                    item_uid or item_url or title,
                )
                existing_job = _active_download_job(idempotency_key)
                if existing_job is not None:
                    was_stale = _make_stale_job_queued(existing_job)
                    if (
                        parse_str_enum(JobStatus, existing_job.status)
                        is JobStatus.QUEUED
                    ):
                        _publish_created_job(existing_job)
                        total_enqueued += 1
                    source_enqueued += 1
                    log.info(
                        "Download episode job already active profile_id=%s source_id=%s job_id=%s job_status=%s republished=%s reset_stale=%s item_uid=%s title=%s reserved_for_source=%s limit=%s",
                        profile_id,
                        source.id,
                        existing_job.id,
                        existing_job.status,
                        parse_str_enum(JobStatus, existing_job.status)
                        is JobStatus.QUEUED,
                        was_stale,
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
                        "media_type": source.media_type
                        or ("audio" if source_type is SourceType.PODCAST else "video"),
                        "source_max_downloads": limit,
                        "item_uid": item_uid,
                        "item_url": item_url,
                        "media_url": candidate.get("media_url") or item_url,
                        "title": title,
                        "published": candidate.get("published") or "",
                        "subtitles": bool(source.subtitles),
                        "subtitle_offset_seconds": source.subtitle_offset_seconds,
                        "delete_explicit_content": bool(source.delete_explicit_content),
                        "include_shorts": bool(
                            getattr(source, "include_shorts", False)
                        ),
                        "include_livestreams": bool(
                            getattr(source, "include_livestreams", False)
                        ),
                    },
                    idempotency_key=idempotency_key,
                )
                _publish_created_job(child)
                source_enqueued += 1
                total_enqueued += 1
                log.info(
                    "Download episode job enqueued profile_id=%s source_id=%s child_job_id=%s item_uid=%s title=%s enqueued_for_source=%s limit=%s",
                    profile_id,
                    source.id,
                    child.id,
                    item_uid,
                    title,
                    source_enqueued,
                    limit,
                )
            log.info(
                "Episode check source finished profile_id=%s source_id=%s source_type=%s seen=%s enqueued=%s",
                profile_id,
                source.id,
                source.source_type,
                source_seen,
                source_enqueued,
            )
        log.info(
            "Episode check profile finished job_id=%s profile_id=%s", job.id, profile_id
        )
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
    log.info(
        "Download worker started job_id=%s profile_id=%s payload=%s",
        job.id,
        job.profile_id,
        job.payload,
    )
    payload = job.payload if isinstance(job.payload, dict) else {}
    download_id = payload.get("download_id")
    if not download_id:
        downloaded_result = _download_with_yt_dlp(job, payload)
        if downloaded_result is None:
            log.warning(
                "Download worker did not create a download row job_id=%s", job.id
            )
            return
        if isinstance(downloaded_result, dict):
            log.info(
                "Download worker queued FFmpeg post-processing parent_job_id=%s source_file=%s",
                job.id,
                downloaded_result.get("source_file_path"),
            )
            _enqueue_transcode_job(
                profile_id=job.profile_id,
                payload=downloaded_result,
                parent_job_id=job.id,
            )
            return
        download_id = downloaded_result.id
    download = Download.objects.filter(
        pk=download_id, profile_id=job.profile_id
    ).first()
    if download is None:
        log.warning(
            "Download worker could not find downloaded row for next stage job_id=%s download_id=%s",
            job.id,
            download_id,
        )
        return
    requires_ffmpeg, media_kind, target_ext = _download_requires_ffmpeg(
        download, payload
    )
    if requires_ffmpeg:
        log.info(
            "Download worker queued FFmpeg post-processing for existing download parent_job_id=%s download_id=%s",
            job.id,
            download_id,
        )
        _enqueue_transcode_job(
            profile_id=job.profile_id,
            payload={
                "download_id": download_id,
                "media_type": media_kind,
                "subtitles": payload.get("subtitles", True),
                "subtitle_offset_seconds": payload.get("subtitle_offset_seconds"),
                "source_type": download.source_type,
                "recent_download": True,
                "delete_explicit_content": bool(
                    payload.get("delete_explicit_content", False)
                ),
            },
            parent_job_id=job.id,
        )
        return
    next_job_type = "generate_transcript"
    next_payload = {
        "download_id": download_id,
        "subtitles": payload.get("subtitles", True),
        "subtitle_offset_seconds": payload.get("subtitle_offset_seconds"),
        "source_type": download.source_type,
        "media_type": media_kind,
        "recent_download": True,
        "delete_explicit_content": bool(payload.get("delete_explicit_content", False)),
    }
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
    log.info(
        "Download worker queued next stage parent_job_id=%s download_id=%s child_job_id=%s child_job_type=%s",
        job.id,
        download_id,
        child.id,
        child.job_type,
    )


def download_single(job: Job) -> None:
    log.info("download_single routed to downloader job_id=%s", job.id)
    download_episode(job)


def _subtitles_enabled_for_download(download: Download, payload: dict) -> bool:
    if "subtitles" in payload:
        return str(payload.get("subtitles")).strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
    value = (
        SourceConfig.objects.filter(
            profile_id=download.profile_id,
            source_type=download.source_type,
            name=download.source_name,
        )
        .values_list("subtitles", flat=True)
        .first()
    )
    return True if value is None else bool(value)


def _subtitle_offset_for_download(download: Download, payload: dict) -> float | None:
    if payload.get("subtitle_offset_seconds") not in {None, ""}:
        return float(payload.get("subtitle_offset_seconds"))
    return (
        SourceConfig.objects.filter(
            profile_id=download.profile_id,
            source_type=download.source_type,
            name=download.source_name,
        )
        .values_list("subtitle_offset_seconds", flat=True)
        .first()
    )


def _load_segments_from_subtitle(path: Path) -> list[str]:
    if not path.exists() or path.suffix.lower() != ".srt":
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    segments: list[str] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        ts_index = 1 if len(lines) > 1 and "-->" in lines[1] else 0
        if "-->" not in lines[ts_index]:
            continue
        body = " ".join(lines[ts_index + 1 :]).strip()
        if body:
            segments.append(body)
    return segments


def _screen_deferred_video_before_insert(
    *,
    profile_id: str,
    media_path: Path,
    download_lookup: dict,
    download_defaults: dict,
    payload: dict,
    job_id: int | str,
) -> Download | None:
    """Generate and screen a video transcript before exposing the Download row."""
    subtitle_offset = (
        float(payload.get("subtitle_offset_seconds"))
        if payload.get("subtitle_offset_seconds") not in {None, ""}
        else None
    )
    transcription_mode = _profile_setting(
        profile_id, "subtitle_transcription_mode", "in_process"
    )
    log.info(
        "Download worker profanity check generating transcript before database insert job_id=%s media_path=%s",
        job_id,
        media_path,
    )
    subtitle_path = create_subtitles(
        media_path,
        subtitle_offset,
        True,
        log,
        str(download_defaults.get("title") or media_path.name),
        "download",
        transcription_mode,
    )
    if subtitle_path is None:
        deleted_paths = delete_media_artifacts(media_path)
        log.warning(
            "Deleted video because transcript generation failed before profanity screening job_id=%s media_path=%s deleted_artifacts=%s",
            job_id,
            media_path,
            ", ".join(str(path) for path in deleted_paths) or "none",
        )
        return None
    try:
        explicit_match = screen_transcript(Path(subtitle_path))
    except Exception as screening_exc:  # noqa: BLE001
        deleted_paths = delete_media_artifacts(media_path)
        log.warning(
            "Deleted video because profanity screening failed before database insert job_id=%s media_path=%s error=%s deleted_artifacts=%s",
            job_id,
            media_path,
            screening_exc,
            ", ".join(str(path) for path in deleted_paths) or "none",
        )
        return None
    if explicit_match is not None:
        deleted_paths = delete_media_artifacts(media_path)
        filtered_defaults = dict(download_defaults)
        filtered_defaults.update(
            {
                "file_path": None,
                "file_path_relative": None,
                "file_size_bytes": None,
                "subtitle_path": None,
                "subtitle_path_relative": None,
                "download_status": DownloadStatus.FILTERED,
                "completed_at": timezone.now(),
                "last_seen_at": timezone.now(),
            }
        )
        Download.objects.update_or_create(
            **download_lookup,
            defaults=filtered_defaults,
        )
        log_filtered_deletion(
            source_type=download_lookup.get("source_type"),
            source_name=download_lookup.get("source_name"),
            title=str(download_defaults.get("title") or media_path.stem),
            media_path=media_path,
            match=explicit_match,
            deleted_paths=deleted_paths,
        )
        log.warning(
            "Deleted video before database insert after profanity screening job_id=%s media_path=%s category=%s",
            job_id,
            media_path,
            explicit_match.category,
        )
        return None
    output_root = _download_output_root(profile_id)
    final_defaults = dict(download_defaults)
    final_defaults["subtitle_path"] = str(subtitle_path)
    final_defaults["subtitle_path_relative"] = (
        str(Path(subtitle_path).relative_to(output_root))
        if Path(subtitle_path).is_relative_to(output_root)
        else None
    )
    download, _created = Download.objects.update_or_create(
        **download_lookup,
        defaults=final_defaults,
    )
    segments = _load_segments_from_subtitle(Path(subtitle_path))
    TranscriptSegment.objects.bulk_create(
        [
            TranscriptSegment(
                download=download,
                subtitle_path=str(subtitle_path),
                start_seconds=0.0,
                end_seconds=None,
                text=text,
            )
            for text in segments
        ]
    )
    log.info(
        "Download worker added video to database after profanity check passed job_id=%s download_id=%s subtitle_path=%s",
        job_id,
        download.id,
        subtitle_path,
    )
    return download


def _generate_deferred_transcript_screening(job: Job, payload: dict) -> None:
    """Generate/screen subtitles for a converted video before inserting its Download row."""
    download_lookup = (
        payload.get("deferred_download_lookup")
        if isinstance(payload.get("deferred_download_lookup"), dict)
        else None
    )
    download_defaults = (
        payload.get("deferred_download_defaults")
        if isinstance(payload.get("deferred_download_defaults"), dict)
        else None
    )
    media_path_value = payload.get("deferred_media_path")
    if not (download_lookup and download_defaults and media_path_value):
        log.warning(
            "Transcript worker skipped deferred screening job with missing payload job_id=%s",
            job.id,
        )
        return
    media_path = Path(str(media_path_value)).expanduser().resolve()
    source_paths_payload = (
        payload.get("ffmpeg_source_file_paths")
        if isinstance(payload.get("ffmpeg_source_file_paths"), list)
        else []
    )
    source_paths = [
        Path(str(path)).expanduser().resolve() for path in source_paths_payload
    ]
    try:
        _touch_active_job(
            job,
            stage="deferred_transcript_screening",
            title=str(download_defaults.get("title") or media_path.name),
        )
        _screen_deferred_video_before_insert(
            profile_id=job.profile_id,
            media_path=media_path,
            download_lookup=download_lookup,
            download_defaults=download_defaults,
            payload=payload,
            job_id=job.id,
        )
    finally:
        if source_paths:
            _delete_ffmpeg_source_files(source_paths, media_path)


def generate_transcript(job: Job) -> None:
    """Generate Whisper subtitles/transcript segments."""
    log.info(
        "Transcript worker started job_id=%s profile_id=%s payload=%s",
        job.id,
        job.profile_id,
        job.payload,
    )
    payload = job.payload if isinstance(job.payload, dict) else {}
    download_id = payload.get("download_id")
    if not download_id:
        if payload.get("deferred_download_lookup") and payload.get(
            "deferred_media_path"
        ):
            _generate_deferred_transcript_screening(job, payload)
            return
        log.warning(
            "Transcript worker skipped job with no download_id job_id=%s", job.id
        )
        return
    lookup_started_at = time.monotonic()
    download = Download.objects.filter(
        pk=download_id, profile_id=job.profile_id
    ).first()
    log.info(
        "Transcript worker download lookup finished job_id=%s download_id=%s found=%s elapsed_seconds=%.3f",
        job.id,
        download_id,
        download is not None,
        time.monotonic() - lookup_started_at,
    )
    if download is None:
        log.warning(
            "Transcript worker skipped missing download job_id=%s download_id=%s profile_id=%s",
            job.id,
            download_id,
            job.profile_id,
        )
        return
    _touch_active_job(
        job,
        stage="transcript_generation",
        title=str(download.title or "").strip(),
    )
    media_path = Path(str(download.file_path or "")).expanduser().resolve()
    if not media_path.is_file():
        media_path = ensure_local_media(download, media_path)
    log.info(
        "Transcript worker loaded download job_id=%s download_id=%s title=%s source_type=%s source_name=%s file_path=%s file_ext=%s subtitle_path=%s status=%s size_bytes=%s last_seen_at=%s",
        job.id,
        download_id,
        download.title,
        download.source_type,
        download.source_name,
        download.file_path,
        download.file_ext,
        download.subtitle_path,
        download.download_status,
        download.file_size_bytes,
        download.last_seen_at,
    )
    if not media_path.exists():
        log.warning(
            "Transcript worker skipped missing media file job_id=%s download_id=%s path=%s",
            job.id,
            download_id,
            media_path,
        )
    else:
        enabled = _subtitles_enabled_for_download(download, payload)
        subtitle_offset = _subtitle_offset_for_download(download, payload)
        transcription_mode = _profile_setting(
            job.profile_id, "subtitle_transcription_mode", "in_process"
        )
        log.info(
            "Transcript worker starting subtitle generation job_id=%s download_id=%s enabled=%s media_path=%s size_bytes=%s offset=%s mode=%s suffix=%s exists=%s",
            job.id,
            download_id,
            enabled,
            media_path,
            media_path.stat().st_size,
            subtitle_offset,
            transcription_mode,
            media_path.suffix,
            media_path.exists(),
        )
        subtitle_started_at = time.monotonic()
        subtitle_path = create_subtitles(
            media_path,
            subtitle_offset,
            enabled,
            log,
            download.title or media_path.name,
            "download",
            transcription_mode,
        )
        log.info(
            "Transcript worker subtitle generation finished job_id=%s download_id=%s subtitle_path=%s elapsed_seconds=%.2f",
            job.id,
            download_id,
            subtitle_path,
            time.monotonic() - subtitle_started_at,
        )
        if subtitle_path is not None:
            output_root = _download_output_root(job.profile_id)
            download.subtitle_path = str(subtitle_path)
            download.subtitle_path_relative = (
                str(subtitle_path.relative_to(output_root))
                if subtitle_path.is_relative_to(output_root)
                else None
            )
            download.save(update_fields=["subtitle_path", "subtitle_path_relative"])
            segments = _load_segments_from_subtitle(Path(subtitle_path))
            deleted_count, _ = TranscriptSegment.objects.filter(
                download=download
            ).delete()
            created_segments = [
                TranscriptSegment(
                    download=download,
                    subtitle_path=str(subtitle_path),
                    start_seconds=0.0,
                    end_seconds=None,
                    text=text,
                )
                for text in segments
            ]
            TranscriptSegment.objects.bulk_create(created_segments)
            log.info(
                "Transcript worker saved subtitles job_id=%s download_id=%s subtitle_path=%s loaded_segments=%s deleted_segments=%s inserted_segments=%s subtitle_size_bytes=%s",
                job.id,
                download_id,
                subtitle_path,
                len(segments),
                deleted_count,
                len(created_segments),
                (
                    Path(subtitle_path).stat().st_size
                    if Path(subtitle_path).exists()
                    else None
                ),
            )
            if bool(payload.get("delete_explicit_content", False)):
                log.info(
                    "Transcript worker profanity check started job_id=%s download_id=%s subtitle_path=%s media_path=%s",
                    job.id,
                    download_id,
                    subtitle_path,
                    media_path,
                )
                try:
                    explicit_match = screen_transcript(Path(subtitle_path))
                except Exception as screening_exc:  # noqa: BLE001
                    log.error(
                        "Transcript worker profanity check failed without deleting media job_id=%s download_id=%s error=%s",
                        job.id,
                        download_id,
                        screening_exc,
                    )
                    return
                if explicit_match is not None:
                    deleted_paths = delete_media_artifacts(media_path)
                    TranscriptSegment.objects.filter(download=download).delete()
                    download.download_status = DownloadStatus.FILTERED
                    download.last_seen_at = timezone.now()
                    download.save(update_fields=["download_status", "last_seen_at"])
                    log_filtered_deletion(
                        source_type=download.source_type,
                        source_name=download.source_name,
                        title=str(download.title or media_path.stem),
                        media_path=media_path,
                        match=explicit_match,
                        deleted_paths=deleted_paths,
                    )
                    log.warning(
                        "Deleted download after transcript profanity screening job_id=%s download_id=%s category=%s",
                        job.id,
                        download_id,
                        explicit_match.category,
                    )
                    return
            log.info(
                "Transcript worker profanity check finished job_id=%s download_id=%s result=clean",
                    job.id,
                    download_id,
                )

        else:
            log.warning(
                "Transcript worker completed without subtitle output job_id=%s download_id=%s enabled=%s media_path=%s",
                job.id,
                download_id,
                enabled,
                media_path,
            )
            if bool(payload.get("delete_explicit_content", False)):
                deleted_paths = delete_media_artifacts(media_path)
                download.download_status = DownloadStatus.FILTERED
                download.last_seen_at = timezone.now()
                download.save(update_fields=["download_status", "last_seen_at"])
                log.warning(
                    "Deleted download because transcript generation failed before profanity screening job_id=%s download_id=%s deleted_artifacts=%s",
                    job.id,
                    download_id,
                    ", ".join(str(path) for path in deleted_paths) or "none",
                )


def ocr_pdf(job: Job) -> None:
    """Extract searchable text from a PDF, OCRing pages without native text."""
    payload = job.payload if isinstance(job.payload, dict) else {}
    download_id = payload.get("download_id")
    if not download_id:
        log.warning("PDF OCR worker skipped job with no download_id job_id=%s", job.id)
        return
    download = Download.objects.filter(
        pk=download_id, profile_id=job.profile_id, file_ext__iexact="pdf"
    ).first()
    if download is None:
        log.warning(
            "PDF OCR worker skipped missing or non-PDF download job_id=%s download_id=%s profile_id=%s",
            job.id,
            download_id,
            job.profile_id,
        )
        return
    pdf_path = Path(str(download.file_path or "")).expanduser().resolve()
    if not pdf_path.is_file():
        try:
            pdf_path = ensure_local_media(download, pdf_path)
        except (FileNotFoundError, OSError) as exc:
            log.warning(
                "PDF OCR worker could not fetch missing file through API job_id=%s download_id=%s error=%s",
                job.id,
                download_id,
                exc,
            )
            return
    if not pdf_path.is_file():
        log.warning(
            "PDF OCR worker skipped missing file job_id=%s download_id=%s path=%s",
            job.id,
            download_id,
            pdf_path,
        )
        return
    _touch_active_job(job, stage="pdf_ocr", title=str(download.title or pdf_path.name))
    pages: list[PdfOcrPage] = extract_pdf_pages(pdf_path)
    TranscriptSegment.objects.filter(download=download).delete()
    created_segments: list[TranscriptSegment] = []
    for page in pages:
        sentences = split_sentences(page.text)
        for sentence_index, sentence in enumerate(sentences):
            # PDF segments use the page number as a virtual timestamp. The
            # tiny offset preserves sentence order without changing the page
            # label or the destination page in the player.
            virtual_position = float(page.page_number - 1) + (
                sentence_index / 1_000_000
            )
            created_segments.append(
                TranscriptSegment(
                    download=download,
                    subtitle_path=f"ocr://download/{download.id}/page/{page.page_number}",
                    start_seconds=virtual_position,
                    end_seconds=float(page.page_number),
                    text=sentence,
                )
            )
    TranscriptSegment.objects.bulk_create(created_segments)
    log.info(
        "PDF OCR worker saved searchable text job_id=%s download_id=%s pages=%s segments=%s ocr_pages=%s",
        job.id,
        download.id,
        len(pages),
        len(created_segments),
        sum(1 for page in pages if page.used_ocr),
    )


def retention_cleanup(job: Job) -> None:
    payload = job.payload if isinstance(job.payload, dict) else {}
    configured_days = (
        ProfileConfigValue.objects.filter(
            profile_id=job.profile_id, key="auto_delete_content_days"
        )
        .values_list("value", flat=True)
        .first()
    )
    try:
        retention_days = int(payload.get("retention_days") or configured_days or 0)
    except (TypeError, ValueError):
        retention_days = 0
    if retention_days <= 0:
        log.info(
            "Retention cleanup skipped because retention is disabled job_id=%s profile_id=%s",
            job.id,
            job.profile_id,
        )
        return
    cutoff = timezone.now() - timedelta(days=retention_days)
    rows = list(
        Download.objects.filter(
            profile_id=job.profile_id, download_status=DownloadStatus.DOWNLOADED
        )
        .exclude(source_type="manual")
        .order_by("completed_at", "first_seen_at", "id")
    )
    deleted = 0
    marked_missing = 0
    skipped_favorites = 0
    now = timezone.now()
    for download in rows:
        media_path = (
            Path(str(download.file_path or "")).expanduser()
            if download.file_path
            else None
        )
        if not media_path or not media_path.is_file():
            download.download_status = DownloadStatus.MISSING
            download.last_seen_at = now
            download.save(update_fields=["download_status", "last_seen_at"])
            marked_missing += 1
            continue
        if download.favorite:
            skipped_favorites += 1
            continue
        content_date = download.completed_at or download.first_seen_at
        if content_date and content_date > cutoff:
            continue
        try:
            media_path.unlink()
        except FileNotFoundError:
            log.debug(
                "Retention cleanup file already absent job_id=%s download_id=%s path=%s",
                job.id,
                download.id,
                media_path,
            )
        except OSError as exc:
            log.warning(
                "Retention cleanup could not delete file job_id=%s download_id=%s path=%s error=%s",
                job.id,
                download.id,
                media_path,
                exc,
            )
            continue
        download.download_status = DownloadStatus.RETENTION_DELETED
        download.last_seen_at = now
        download.save(update_fields=["download_status", "last_seen_at"])
        deleted += 1
    log.info(
        "Retention cleanup finished job_id=%s profile_id=%s retention_days=%s deleted=%s marked_missing=%s skipped_favorites=%s",
        job.id,
        job.profile_id,
        retention_days,
        deleted,
        marked_missing,
        skipped_favorites,
    )


HANDLERS = {
    "check_for_episodes": check_for_episodes,
    "update_downloads": update_downloads,
    "download_episode": download_episode,
    "download_single": download_single,
    "transcode_media": transcode_media,
    "generate_transcript": generate_transcript,
    "generate_ocr": ocr_pdf,
    "retention_cleanup": retention_cleanup,
}
