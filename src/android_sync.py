import re
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape
from typing import Callable, Iterable, List, Optional, Tuple

from logger import get_logger

log = get_logger("android_sync")

MEDIA_SUBTITLE_EXTENSIONS = {".srt", ".vtt"}
MEDIA_METADATA_EXTENSIONS = {".aac", ".flac", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".wav", ".webm"}
AUDIO_ARTWORK_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".ogg"}
MOV_METADATA_EXTENSIONS = {".m4a", ".mov", ".mp4"}


@dataclass
class AndroidSyncConfig:
    enabled: bool = False
    adb_path: str = "adb"
    destination: str = "/sdcard/Movies/GetOffline"
    max_items: int = 10
    include_subtitles: bool = True


@dataclass
class AndroidSyncItem:
    row_id: int
    title: str
    source_name: str
    file_path: Path
    subtitle_path: Optional[Path] = None
    position_seconds: float = 0.0
    artwork_url: Optional[str] = None


@dataclass
class AndroidSyncResult:
    attempted: int = 0
    copied: int = 0
    skipped: int = 0
    failed: int = 0
    device_serial: Optional[str] = None
    message: str = "idle"
    copied_files: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    vlc_playlist_path: Optional[str] = None


def _coerce_bool(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def config_from_defaults(defaults: dict) -> AndroidSyncConfig:
    try:
        max_items = int(defaults.get("android_sync_max_items") or 10)
    except (TypeError, ValueError):
        max_items = 10
    return AndroidSyncConfig(
        enabled=_coerce_bool(defaults.get("android_sync_enabled")),
        adb_path=str(defaults.get("android_sync_adb_path") or "adb").strip() or "adb",
        destination=str(defaults.get("android_sync_destination") or "/sdcard/Movies/GetOffline").strip()
        or "/sdcard/Movies/GetOffline",
        max_items=max(1, max_items),
        include_subtitles=_coerce_bool(defaults.get("android_sync_include_subtitles", "1")),
    )


def _safe_remote_component(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", str(value or "")).strip(" .-")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:90] or "item"


def _remote_quote(path: str) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def _command_for_log(args: List[str]) -> str:
    return " ".join(_remote_quote(arg) if re.search(r"\s", str(arg)) else str(arg) for arg in args)


def _combined_output(completed: object) -> str:
    stdout = str(getattr(completed, "stdout", "") or "").strip()
    stderr = str(getattr(completed, "stderr", "") or "").strip()
    if stdout and stderr:
        return f"{stdout}; {stderr}"
    return stdout or stderr


def _append_error(result: AndroidSyncResult, message: str) -> None:
    result.errors.append(message)
    log.warning("Android sync: %s", message)


def _run_adb_command(
    args: List[str],
    *,
    description: str,
    timeout: int,
    runner: Callable[..., subprocess.CompletedProcess],
) -> subprocess.CompletedProcess:
    log.debug("Android sync adb command starting: %s (%s)", description, _command_for_log(args))
    try:
        completed = runner(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        log.warning("Android sync adb command timed out during %s after %ss", description, timeout)
        raise RuntimeError(f"{description} timed out after {timeout}s") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Android sync adb command failed during %s: %s", description, exc)
        raise RuntimeError(f"{description} failed: {exc}") from exc

    output = _combined_output(completed)
    log.debug(
        "Android sync adb command finished: %s returncode=%s output=%s",
        description,
        getattr(completed, "returncode", "unknown"),
        output or "none",
    )
    return completed


def build_remote_media_path(destination: str, item: AndroidSyncItem) -> str:
    source = _safe_remote_component(item.source_name)
    title = _safe_remote_component(item.title or item.file_path.stem)
    suffix = item.file_path.suffix or ""
    return f"{destination.rstrip('/')}/{source} - {title}{suffix}"


def _xml_text(value: object) -> str:
    return escape(str(value or ""), {"'": "&apos;", '"': "&quot;"})


def _file_uri_for_android_path(remote_path: str) -> str:
    return "file://" + quote(str(remote_path), safe="/:._-()[]@!$&'+,;=")



def _metadata_position_text(item: AndroidSyncItem) -> str:
    return f"{max(0.0, float(item.position_seconds or 0.0)):.3f}"


def _metadata_comment(item: AndroidSyncItem) -> str:
    return f"GetOffline row_id={item.row_id} position_seconds={_metadata_position_text(item)}"


def _download_artwork(artwork_url: Optional[str]) -> Optional[Path]:
    url = str(artwork_url or "").strip()
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            payload = response.read(5 * 1024 * 1024)
            content_type = str(response.headers.get("Content-Type") or "").lower()
    except Exception as exc:
        log.warning("Android sync artwork download failed url=%s: %s", url, exc)
        return None
    if not payload:
        log.warning("Android sync artwork download returned empty payload url=%s", url)
        return None
    suffix = ".jpg"
    if "png" in content_type:
        suffix = ".png"
    with tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False) as handle:
        handle.write(payload)
        return Path(handle.name)


def _build_ffmpeg_metadata_command(ffmpeg_path: str, source_path: Path, output_path: Path, item: AndroidSyncItem, artwork_path: Optional[Path]) -> List[str]:
    command = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source_path),
    ]
    if artwork_path is not None:
        command.extend(["-i", str(artwork_path)])
    command.extend([
        "-map",
        "0",
    ])
    if artwork_path is not None:
        command.extend(["-map", "1", "-disposition:v:0", "attached_pic"])
    command.extend([
        "-c",
        "copy",
        "-metadata",
        f"title={item.title or source_path.stem}",
        "-metadata",
        f"artist={item.source_name or 'GetOffline'}",
        "-metadata",
        f"album_artist={item.source_name or 'GetOffline'}",
        "-metadata",
        f"album={item.source_name or 'GetOffline'}",
        "-metadata",
        "genre=Podcast",
        "-metadata",
        f"comment={_metadata_comment(item)}",
    ])
    if source_path.suffix.lower() == ".mp3":
        command.extend(["-id3v2_version", "3"])
    if source_path.suffix.lower() in MOV_METADATA_EXTENSIONS:
        command.extend(["-movflags", "use_metadata_tags"])
    command.append(str(output_path))
    return command


def _metadata_temp_path(source_path: Path) -> Path:
    with tempfile.NamedTemporaryFile("wb", suffix=source_path.suffix, delete=False) as handle:
        return Path(handle.name)


def _copy_with_embedded_metadata(
    source_path: Path,
    item: AndroidSyncItem,
    runner: Callable[..., subprocess.CompletedProcess],
) -> Path:
    if source_path.suffix.lower() not in MEDIA_METADATA_EXTENSIONS:
        log.info("Android sync metadata skipped: unsupported extension path=%s", source_path)
        return source_path

    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        log.info("Android sync metadata skipped: ffmpeg not found")
        return source_path

    artwork_path = None
    if source_path.suffix.lower() in AUDIO_ARTWORK_EXTENSIONS:
        artwork_path = _download_artwork(item.artwork_url)
    output_path = _metadata_temp_path(source_path)
    command = _build_ffmpeg_metadata_command(ffmpeg_path, source_path, output_path, item, artwork_path)
    try:
        completed = _run_adb_command(
            command,
            description=f"embedding VLC-visible metadata for row {item.row_id}",
            timeout=300,
            runner=runner,
        )
    except RuntimeError as exc:
        log.warning("Android sync metadata embedding failed for row_id=%s: %s", item.row_id, exc)
        output_path.unlink(missing_ok=True)
        if artwork_path is not None:
            artwork_path.unlink(missing_ok=True)
        return source_path

    if artwork_path is not None:
        artwork_path.unlink(missing_ok=True)

    if int(getattr(completed, "returncode", 1) or 0) != 0:
        log.warning(
            "Android sync metadata embedding failed for row_id=%s: %s",
            item.row_id,
            _combined_output(completed) or "ffmpeg returned non-zero status",
        )
        output_path.unlink(missing_ok=True)
        return source_path

    if not output_path.exists() or output_path.stat().st_size <= 0:
        log.warning("Android sync metadata embedding produced no output for row_id=%s", item.row_id)
        output_path.unlink(missing_ok=True)
        return source_path

    log.info("Android sync metadata embedded: row_id=%s temp=%s", item.row_id, output_path)
    return output_path


def build_vlc_xspf(entries: List[Tuple[AndroidSyncItem, str]]) -> str:
    track_lines = []
    for idx, (item, remote_path) in enumerate(entries):
        raw_position_seconds = max(0.0, float(item.position_seconds or 0.0))
        position_seconds = int(raw_position_seconds)
        title = _xml_text(item.title or item.file_path.stem)
        creator = _xml_text(item.source_name or "GetOffline")
        location = _xml_text(_file_uri_for_android_path(remote_path))
        annotation = _xml_text(_metadata_comment(item))
        track_lines.append(
            f"""    <track>
      <location>{location}</location>
      <title>{title}</title>
      <creator>{creator}</creator>
      <annotation>{annotation}</annotation>
      <extension application="http://www.videolan.org/vlc/playlist/0">
        <vlc:id>{idx}</vlc:id>
        <vlc:option>start-time={position_seconds}</vlc:option>
      </extension>
    </track>"""
        )

    track_list = "\n".join(track_lines)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<playlist version="1" xmlns="http://xspf.org/ns/0/" xmlns:vlc="http://www.videolan.org/vlc/playlist/ns/0/">
  <title>GetOffline</title>
  <creator>GetOffline</creator>
  <trackList>
{track_list}
  </trackList>
</playlist>
"""


def find_connected_device(adb_path: str, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> Optional[str]:
    adb_executable = shutil.which(adb_path) if not Path(adb_path).is_absolute() else adb_path
    if not adb_executable:
        log.warning("Android sync: adb executable not found: %s", adb_path)
        return None
    completed = _run_adb_command(
        [adb_executable, "devices"],
        description="checking connected Android devices",
        timeout=15,
        runner=runner,
    )
    if int(getattr(completed, "returncode", 1) or 0) != 0:
        raise RuntimeError(f"adb devices failed: {_combined_output(completed) or 'no output'}")

    unauthorized = []
    offline = []
    for line in str(getattr(completed, "stdout", "") or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state == "device":
            log.info("Android sync: found authorized device serial=%s", serial)
            return serial
        if state == "unauthorized":
            unauthorized.append(serial)
        elif state == "offline":
            offline.append(serial)

    if unauthorized:
        log.warning("Android sync: device(s) connected but unauthorized: %s", ", ".join(unauthorized))
    if offline:
        log.warning("Android sync: device(s) connected but offline: %s", ", ".join(offline))
    if not unauthorized and not offline:
        log.info("Android sync: no Android devices reported by adb")
    return None


def sync_items_to_android(
    items: Iterable[AndroidSyncItem],
    config: AndroidSyncConfig,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> AndroidSyncResult:
    result = AndroidSyncResult(message="disabled")
    if not config.enabled:
        log.info("Android sync skipped: disabled")
        return result

    sync_items = list(items)[: config.max_items]
    if not sync_items:
        result.message = "no unplayed media to sync"
        log.info("Android sync skipped: no unplayed media items selected")
        return result

    adb_executable = shutil.which(config.adb_path) if not Path(config.adb_path).is_absolute() else config.adb_path
    if not adb_executable:
        result.message = f"adb not found: {config.adb_path}"
        result.failed += 1
        _append_error(result, result.message)
        return result

    destination = config.destination.rstrip("/") or "/sdcard/Movies/GetOffline"
    log.info(
        "Android sync starting: items=%s destination=%s adb=%s include_subtitles=%s",
        len(sync_items),
        destination,
        adb_executable,
        "yes" if config.include_subtitles else "no",
    )

    try:
        device_serial = find_connected_device(adb_executable, runner=runner)
    except RuntimeError as exc:
        result.message = f"adb device check failed: {exc}"
        result.failed += 1
        _append_error(result, str(exc))
        return result

    if not device_serial:
        result.message = "no authorized Android device connected"
        _append_error(result, "no authorized Android device connected; check USB debugging authorization")
        return result

    result.device_serial = device_serial
    vlc_playlist_entries: List[Tuple[AndroidSyncItem, str]] = []
    try:
        mkdir = _run_adb_command(
            [adb_executable, "-s", device_serial, "shell", f"mkdir -p {_remote_quote(destination)}"],
            description=f"creating Android destination folder {destination}",
            timeout=30,
            runner=runner,
        )
    except RuntimeError as exc:
        result.message = f"unable to prepare Android folder: {exc}"
        result.failed += 1
        _append_error(result, str(exc))
        return result
    if int(getattr(mkdir, "returncode", 1) or 0) != 0:
        output = _combined_output(mkdir) or "no output"
        result.message = f"unable to prepare Android folder: {output}"
        result.failed += 1
        _append_error(result, result.message)
        return result

    for item in sync_items:
        result.attempted += 1
        local_path = item.file_path.expanduser().resolve()
        if not local_path.exists() or not local_path.is_file():
            result.failed += 1
            _append_error(result, f"missing local file for row {item.row_id}: {local_path}")
            continue

        remote_media_path = build_remote_media_path(destination, item)
        log.info(
            "Android sync item starting: row_id=%s local=%s remote=%s size_bytes=%s",
            item.row_id,
            local_path,
            remote_media_path,
            local_path.stat().st_size,
        )
        remote_exists = False
        try:
            exists = _run_adb_command(
                [adb_executable, "-s", device_serial, "shell", f"test -f {_remote_quote(remote_media_path)}"],
                description=f"checking existing Android file for row {item.row_id}",
                timeout=15,
                runner=runner,
            )
        except RuntimeError as exc:
            result.failed += 1
            _append_error(result, f"unable to check existing Android file for row {item.row_id}: {exc}")
            continue
        if int(getattr(exists, "returncode", 1) or 0) == 0:
            remote_exists = True

        push_source_path = _copy_with_embedded_metadata(local_path, item, runner)
        if remote_exists and push_source_path == local_path:
            result.skipped += 1
            vlc_playlist_entries.append((item, remote_media_path))
            log.info("Android sync item skipped: row_id=%s remote file already exists and metadata refresh is unavailable", item.row_id)
            continue
        if remote_exists:
            log.info("Android sync item refreshing metadata on existing remote file: row_id=%s", item.row_id)
        try:
            pushed = _run_adb_command(
                [adb_executable, "-s", device_serial, "push", str(push_source_path), remote_media_path],
                description=f"pushing media row {item.row_id}",
                timeout=300,
                runner=runner,
            )
        except RuntimeError as exc:
            result.failed += 1
            _append_error(result, f"push failed for row {item.row_id}: {exc}")
            if push_source_path != local_path:
                push_source_path.unlink(missing_ok=True)
            continue
        if push_source_path != local_path:
            push_source_path.unlink(missing_ok=True)
        if int(getattr(pushed, "returncode", 1) or 0) != 0:
            result.failed += 1
            _append_error(result, _combined_output(pushed) or f"push failed for row {item.row_id}: {local_path}")
            continue

        result.copied += 1
        result.copied_files.append(remote_media_path)
        vlc_playlist_entries.append((item, remote_media_path))
        log.info("Android sync item copied: row_id=%s remote=%s", item.row_id, remote_media_path)

        if not config.include_subtitles or not item.subtitle_path:
            continue

        subtitle_path = item.subtitle_path.expanduser().resolve()
        if not subtitle_path.exists():
            log.info("Android sync subtitle skipped: row_id=%s subtitle missing path=%s", item.row_id, subtitle_path)
            continue
        if subtitle_path.suffix.lower() not in MEDIA_SUBTITLE_EXTENSIONS:
            log.info("Android sync subtitle skipped: row_id=%s unsupported extension path=%s", item.row_id, subtitle_path)
            continue

        remote_subtitle_path = str(Path(remote_media_path).with_suffix(subtitle_path.suffix)).replace("\\", "/")
        try:
            subtitle_push = _run_adb_command(
                [adb_executable, "-s", device_serial, "push", str(subtitle_path), remote_subtitle_path],
                description=f"pushing subtitle for row {item.row_id}",
                timeout=120,
                runner=runner,
            )
        except RuntimeError as exc:
            _append_error(result, f"subtitle push failed for row {item.row_id}: {exc}")
            continue
        if int(getattr(subtitle_push, "returncode", 1) or 0) != 0:
            _append_error(
                result,
                _combined_output(subtitle_push) or f"subtitle push failed for row {item.row_id}: {subtitle_path}",
            )
        else:
            log.info("Android sync subtitle copied: row_id=%s remote=%s", item.row_id, remote_subtitle_path)


    if vlc_playlist_entries:
        remote_playlist_path = f"{destination}/GetOffline.xspf"
        playlist_content = build_vlc_xspf(vlc_playlist_entries)
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".xspf", delete=False) as handle:
                handle.write(playlist_content)
                local_playlist_path = Path(handle.name)
            playlist_push = _run_adb_command(
                [adb_executable, "-s", device_serial, "push", str(local_playlist_path), remote_playlist_path],
                description="pushing VLC playlist metadata",
                timeout=120,
                runner=runner,
            )
        except RuntimeError as exc:
            _append_error(result, f"VLC playlist metadata push failed: {exc}")
        except OSError as exc:
            _append_error(result, f"VLC playlist metadata generation failed: {exc}")
        else:
            if int(getattr(playlist_push, "returncode", 1) or 0) != 0:
                _append_error(
                    result,
                    _combined_output(playlist_push) or "VLC playlist metadata push failed",
                )
            else:
                result.vlc_playlist_path = remote_playlist_path
                log.info(
                    "Android sync VLC playlist metadata copied: remote=%s entries=%s",
                    remote_playlist_path,
                    len(vlc_playlist_entries),
                )
        finally:
            try:
                local_playlist_path.unlink(missing_ok=True)
            except (NameError, OSError):
                pass

    if result.failed:
        result.message = f"copied {result.copied}, skipped {result.skipped}, failed {result.failed}"
    else:
        result.message = f"copied {result.copied}, skipped {result.skipped}"
    log.info(
        "Android sync finished: attempted=%s copied=%s skipped=%s failed=%s device=%s",
        result.attempted,
        result.copied,
        result.skipped,
        result.failed,
        result.device_serial or "none",
    )
    return result
