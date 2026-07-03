import re
import shutil
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

from workers.logger import get_logger

log = get_logger("media_sync")

MEDIA_SUBTITLE_EXTENSIONS = {".srt", ".vtt"}
MEDIA_METADATA_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".wav",
    ".webm",
}
AUDIO_ARTWORK_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".ogg"}
MOV_METADATA_EXTENSIONS = {".m4a", ".mov", ".mp4"}


@dataclass
class AndroidSyncConfig:
    enabled: bool = False
    target: str = "android"
    directory: str = "./offline-sync"
    adb_path: str = "adb"
    connection_mode: str = "usb"
    wifi_address: str = ""
    destination: str = "/sdcard/Movies/GetOffline"
    max_items: int = 10
    include_subtitles: bool = True
    include_unplayed: bool = True
    include_started: bool = True
    include_played: bool = False
    exclude_regex: str = ""


@dataclass
class AndroidSyncItem:
    row_id: int
    title: str
    source_name: str
    file_path: Path
    subtitle_path: Path | None = None
    position_seconds: float = 0.0
    artwork_url: str | None = None
    artwork_path: Path | None = None


@dataclass
class AndroidSyncResult:
    attempted: int = 0
    copied: int = 0
    skipped: int = 0
    failed: int = 0
    device_serial: str | None = None
    message: str = "idle"
    copied_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    vlc_playlist_path: str | None = None


def _coerce_bool(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def config_from_defaults(defaults: dict) -> AndroidSyncConfig:
    try:
        max_items = int(defaults.get("android_sync_max_items") or 10)
    except (TypeError, ValueError):
        max_items = 10
    return AndroidSyncConfig(
        enabled=_coerce_bool(defaults.get("android_sync_enabled")),
        target="android",
        directory="",
        adb_path=str(defaults.get("android_sync_adb_path") or "adb").strip() or "adb",
        connection_mode=str(defaults.get("android_sync_connection_mode") or "usb")
        .strip()
        .lower()
        or "usb",
        wifi_address=str(defaults.get("android_sync_wifi_address") or "").strip(),
        destination=str(
            defaults.get("android_sync_destination") or "/sdcard/Movies/GetOffline"
        ).strip()
        or "/sdcard/Movies/GetOffline",
        max_items=max(1, max_items),
        include_subtitles=_coerce_bool(
            defaults.get("android_sync_include_subtitles", "1")
        ),
        include_unplayed=_coerce_bool(
            defaults.get("android_sync_include_unplayed", "1")
        ),
        include_started=_coerce_bool(defaults.get("android_sync_include_started", "1")),
        include_played=_coerce_bool(defaults.get("android_sync_include_played", "0")),
        exclude_regex=str(defaults.get("android_sync_exclude_regex") or "").strip(),
    )


def _safe_remote_component(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", str(value or "")).strip(" .-")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:90] or "item"


def _remote_quote(path: str) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def _command_for_log(args: list[str]) -> str:
    return " ".join(
        _remote_quote(arg) if re.search(r"\s", str(arg)) else str(arg) for arg in args
    )


def _combined_output(completed: object) -> str:
    stdout = str(getattr(completed, "stdout", "") or "").strip()
    stderr = str(getattr(completed, "stderr", "") or "").strip()
    if stdout and stderr:
        return f"{stdout}; {stderr}"
    return stdout or stderr


def _append_error(
    result: AndroidSyncResult, message: str, sync_name: str = "Android transfer"
) -> None:
    result.errors.append(message)
    log.warning("%s: %s", sync_name, message)


def _run_adb_command(
    args: list[str],
    *,
    description: str,
    timeout: int,
    runner: Callable[..., subprocess.CompletedProcess],
    log_context: str = "Android transfer adb command",
) -> subprocess.CompletedProcess:
    log.debug("%s starting: %s (%s)", log_context, description, _command_for_log(args))
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
        log.warning(
            "%s timed out during %s after %ss", log_context, description, timeout
        )
        raise RuntimeError(f"{description} timed out after {timeout}s") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("%s failed during %s: %s", log_context, description, exc)
        raise RuntimeError(f"{description} failed: {exc}") from exc

    output = _combined_output(completed)
    log.debug(
        "%s finished: %s returncode=%s output=%s",
        log_context,
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


def _metadata_values(source_path: Path, item: AndroidSyncItem) -> list[tuple[str, str]]:
    title = str(item.title or source_path.stem)
    artist = str(item.source_name or "GetOffline")
    return [
        ("title", title),
        ("artist", artist),
        ("album_artist", artist),
        ("album", artist),
        ("genre", "Podcast"),
        ("comment", _metadata_comment(item)),
    ]


def _append_metadata_args(
    command: list[str], metadata_values: list[tuple[str, str]], prefix: str
) -> None:
    for key, value in metadata_values:
        command.extend([prefix, f"{key}={value}"])


def _download_artwork(artwork_url: str | None) -> Path | None:
    url = str(artwork_url or "").strip()
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            payload = response.read(20 * 1024 * 1024)
            content_type = str(response.headers.get("Content-Type") or "").lower()
    except Exception as exc:
        log.warning("Media transfer artwork download failed url=%s: %s", url, exc)
        return None
    if not payload:
        log.warning(
            "Media transfer artwork download returned empty payload url=%s", url
        )
        return None
    suffix = ".jpg"
    if "png" in content_type:
        suffix = ".png"
    with tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False) as handle:
        handle.write(payload)
        return Path(handle.name)


def _build_ffmpeg_metadata_command(
    ffmpeg_path: str,
    source_path: Path,
    output_path: Path,
    item: AndroidSyncItem,
    artwork_path: Path | None,
) -> list[str]:
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
    command.extend(
        [
            "-map",
            "0",
        ]
    )
    if artwork_path is not None:
        command.extend(["-map", "1", "-disposition:v:0", "attached_pic"])
        command.extend(["-c:a", "copy", "-c:v", "mjpeg"])
    else:
        command.extend(["-c", "copy"])
    metadata_values = _metadata_values(source_path, item)
    _append_metadata_args(command, metadata_values, "-metadata")
    _append_metadata_args(command, metadata_values, "-metadata:s:a:0")
    if source_path.suffix.lower() == ".mp3":
        command.extend(["-id3v2_version", "3"])
    if source_path.suffix.lower() in MOV_METADATA_EXTENSIONS:
        command.extend(["-movflags", "use_metadata_tags"])
    command.append(str(output_path))
    return command


def _metadata_temp_path(source_path: Path) -> Path:
    with tempfile.NamedTemporaryFile(
        "wb", suffix=source_path.suffix, delete=False
    ) as handle:
        return Path(handle.name)


def _copy_with_embedded_metadata(
    source_path: Path,
    item: AndroidSyncItem,
    runner: Callable[..., subprocess.CompletedProcess],
) -> Path:
    if source_path.suffix.lower() not in MEDIA_METADATA_EXTENSIONS:
        log.info(
            "Media transfer metadata skipped: unsupported extension path=%s",
            source_path,
        )
        return source_path

    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        log.info("Media transfer metadata skipped: ffmpeg not found")
        return source_path

    artwork_path = None
    downloaded_artwork = False
    if source_path.suffix.lower() in AUDIO_ARTWORK_EXTENSIONS:
        if item.artwork_path is not None:
            candidate_artwork_path = item.artwork_path.expanduser().resolve()
            if candidate_artwork_path.exists() and candidate_artwork_path.is_file():
                artwork_path = candidate_artwork_path
        if artwork_path is None:
            artwork_path = _download_artwork(item.artwork_url)
            downloaded_artwork = artwork_path is not None
    output_path = _metadata_temp_path(source_path)
    command = _build_ffmpeg_metadata_command(
        ffmpeg_path, source_path, output_path, item, artwork_path
    )
    try:
        completed = _run_adb_command(
            command,
            description=f"embedding VLC-visible metadata for row {item.row_id}",
            timeout=300,
            runner=runner,
            log_context="Media transfer command",
        )
    except RuntimeError as exc:
        log.warning(
            "Media transfer metadata embedding failed for row_id=%s: %s",
            item.row_id,
            exc,
        )
        output_path.unlink(missing_ok=True)
        if artwork_path is not None and downloaded_artwork:
            artwork_path.unlink(missing_ok=True)
        return source_path

    if artwork_path is not None and downloaded_artwork:
        artwork_path.unlink(missing_ok=True)

    if int(getattr(completed, "returncode", 1) or 0) != 0:
        log.warning(
            "Media transfer metadata embedding failed for row_id=%s: %s",
            item.row_id,
            _combined_output(completed) or "ffmpeg returned non-zero status",
        )
        output_path.unlink(missing_ok=True)
        return source_path

    if not output_path.exists() or output_path.stat().st_size <= 0:
        log.warning(
            "Media transfer metadata embedding produced no output for row_id=%s",
            item.row_id,
        )
        output_path.unlink(missing_ok=True)
        return source_path

    log.info(
        "Media transfer metadata embedded: row_id=%s temp=%s", item.row_id, output_path
    )
    return output_path


def build_vlc_xspf(entries: list[tuple[AndroidSyncItem, str]]) -> str:
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


def _normalize_wifi_address(address: str) -> str:
    value = str(address or "").strip()
    if not value:
        return ""
    if value.startswith("[") or ":" in value:
        return value
    return f"{value}:5555"


def _connect_adb_wifi(
    adb_executable: str,
    wifi_address: str,
    runner: Callable[..., subprocess.CompletedProcess],
) -> str | None:
    target = _normalize_wifi_address(wifi_address)
    if not target:
        log.warning(
            "Android transfer Wi-Fi requested but no device address is configured"
        )
        return None

    completed = _run_adb_command(
        [adb_executable, "connect", target],
        description=f"connecting to paired Android device over Wi-Fi at {target}",
        timeout=30,
        runner=runner,
    )
    output = _combined_output(completed)
    if int(getattr(completed, "returncode", 1) or 0) != 0:
        raise RuntimeError(f"adb connect {target} failed: {output or 'no output'}")
    if output and "failed" in output.lower():
        raise RuntimeError(f"adb connect {target} failed: {output}")
    log.info(
        "Android transfer: Wi-Fi adb connect completed for %s: %s",
        target,
        output or "no output",
    )
    return target


def find_connected_device(
    adb_path: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    *,
    connection_mode: str = "usb",
    wifi_address: str = "",
) -> str | None:
    adb_executable = (
        shutil.which(adb_path) if not Path(adb_path).is_absolute() else adb_path
    )
    if not adb_executable:
        log.warning("Android transfer: adb executable not found: %s", adb_path)
        return None

    expected_serial = None
    if str(connection_mode or "usb").strip().lower() == "wifi":
        expected_serial = _connect_adb_wifi(adb_executable, wifi_address, runner)
        if not expected_serial:
            return None

    completed = _run_adb_command(
        [adb_executable, "devices"],
        description="checking connected Android devices",
        timeout=15,
        runner=runner,
    )
    if int(getattr(completed, "returncode", 1) or 0) != 0:
        raise RuntimeError(
            f"adb devices failed: {_combined_output(completed) or 'no output'}"
        )

    authorized = []
    unauthorized = []
    offline = []
    for line in str(getattr(completed, "stdout", "") or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state == "device":
            authorized.append(serial)
            continue
        if state == "unauthorized":
            unauthorized.append(serial)
        elif state == "offline":
            offline.append(serial)

    if expected_serial:
        if expected_serial in authorized:
            log.info(
                "Android transfer: found authorized Wi-Fi device serial=%s",
                expected_serial,
            )
            return expected_serial
        log.warning(
            "Android transfer: Wi-Fi device %s is not authorized/online after adb connect",
            expected_serial,
        )
    elif authorized:
        log.info("Android transfer: found authorized device serial=%s", authorized[0])
        return authorized[0]

    if unauthorized:
        log.warning(
            "Android transfer: device(s) connected but unauthorized: %s",
            ", ".join(unauthorized),
        )
    if offline:
        log.warning(
            "Android transfer: device(s) connected but offline: %s", ", ".join(offline)
        )
    if not unauthorized and not offline:
        log.info("Android transfer: no Android devices reported by adb")
    return None


def _rescan_android_media(
    adb_executable: str,
    device_serial: str,
    remote_path: str,
    runner: Callable[..., subprocess.CompletedProcess],
) -> None:
    media_uri = _file_uri_for_android_path(remote_path)
    try:
        completed = _run_adb_command(
            [
                adb_executable,
                "-s",
                device_serial,
                "shell",
                f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d {_remote_quote(media_uri)}",
            ],
            description=f"requesting Android media rescan for {remote_path}",
            timeout=30,
            runner=runner,
        )
    except RuntimeError as exc:
        log.warning("Android transfer media rescan failed for %s: %s", remote_path, exc)
        return
    if int(getattr(completed, "returncode", 1) or 0) != 0:
        log.warning(
            "Android transfer media rescan returned non-zero for %s: %s",
            remote_path,
            _combined_output(completed) or "no output",
        )


def _remote_subtitle_paths_for_media(remote_media_path: str) -> list[str]:
    paths = []
    for suffix in sorted(MEDIA_SUBTITLE_EXTENSIONS):
        paths.append(
            str(Path(remote_media_path).with_suffix(suffix)).replace("\\", "/")
        )
    return paths


def _syncdb_remote_path(destination: str) -> str:
    return f"{destination.rstrip('/')}/transferdb.txt"


def _read_remote_syncdb(
    adb_executable: str,
    device_serial: str,
    destination: str,
    runner: Callable[..., subprocess.CompletedProcess],
) -> set[str]:
    remote_syncdb_path = _syncdb_remote_path(destination)
    try:
        completed = _run_adb_command(
            [
                adb_executable,
                "-s",
                device_serial,
                "shell",
                f"cat {_remote_quote(remote_syncdb_path)} 2>/dev/null || true",
            ],
            description=f"reading Android transfer history {remote_syncdb_path}",
            timeout=30,
            runner=runner,
        )
    except RuntimeError as exc:
        log.warning(
            "Android transfer history read failed; continuing with empty history: %s",
            exc,
        )
        return set()
    if int(getattr(completed, "returncode", 1) or 0) != 0:
        log.warning(
            "Android transfer history read returned non-zero; continuing with empty history: %s",
            _combined_output(completed) or "no output",
        )
        return set()
    return {
        line.strip()
        for line in str(getattr(completed, "stdout", "") or "").splitlines()
        if line.strip()
    }


def _write_remote_syncdb(
    adb_executable: str,
    device_serial: str,
    destination: str,
    synced_paths: Iterable[str],
    result: AndroidSyncResult,
    runner: Callable[..., subprocess.CompletedProcess],
) -> None:
    remote_syncdb_path = _syncdb_remote_path(destination)
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".txt", delete=False
        ) as handle:
            for path in sorted(
                {str(path).strip() for path in synced_paths if str(path).strip()}
            ):
                handle.write(path + "\n")
            local_syncdb_path = Path(handle.name)
        completed = _run_adb_command(
            [
                adb_executable,
                "-s",
                device_serial,
                "push",
                str(local_syncdb_path),
                remote_syncdb_path,
            ],
            description="pushing Android transfer history",
            timeout=120,
            runner=runner,
        )
    except RuntimeError as exc:
        _append_error(result, f"Android transfer history push failed: {exc}")
    except OSError as exc:
        _append_error(result, f"Android transfer history generation failed: {exc}")
    else:
        if int(getattr(completed, "returncode", 1) or 0) != 0:
            _append_error(
                result,
                _combined_output(completed) or "Android transfer history push failed",
            )
        else:
            log.info("Android transfer history copied: remote=%s", remote_syncdb_path)
    finally:
        try:
            local_syncdb_path.unlink(missing_ok=True)
        except (NameError, OSError):
            pass


def delete_items_from_android(
    items: Iterable[AndroidSyncItem],
    config: AndroidSyncConfig,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> AndroidSyncResult:
    result = AndroidSyncResult(message="disabled")
    if not config.enabled:
        log.info("Android delete skipped: disabled")
        return result

    delete_items = list(items)
    if not delete_items:
        result.message = "no Android files to delete"
        log.info("Android delete skipped: no items selected")
        return result

    adb_executable = (
        shutil.which(config.adb_path)
        if not Path(config.adb_path).is_absolute()
        else config.adb_path
    )
    if not adb_executable:
        result.message = f"adb not found: {config.adb_path}"
        result.failed += 1
        _append_error(result, result.message)
        return result

    destination = config.destination.rstrip("/") or "/sdcard/Movies/GetOffline"
    log.info(
        "Android delete starting: items=%s destination=%s adb=%s",
        len(delete_items),
        destination,
        adb_executable,
    )

    try:
        device_serial = find_connected_device(
            adb_executable,
            runner=runner,
            connection_mode=config.connection_mode,
            wifi_address=config.wifi_address,
        )
    except RuntimeError as exc:
        result.message = f"adb device check failed: {exc}"
        result.failed += 1
        _append_error(result, str(exc))
        return result

    if not device_serial:
        if str(config.connection_mode or "usb").strip().lower() == "wifi":
            result.message = "no authorized Android Wi-Fi device connected"
            _append_error(
                result,
                "no authorized Android Wi-Fi device connected; check the Wi-Fi address and adb pairing",
            )
        else:
            result.message = "no authorized Android device connected"
            _append_error(
                result,
                "no authorized Android device connected; check USB debugging authorization",
            )
        return result

    result.device_serial = device_serial
    for item in delete_items:
        result.attempted += 1
        remote_media_path = build_remote_media_path(destination, item)
        remote_paths = [remote_media_path]
        remote_paths.extend(_remote_subtitle_paths_for_media(remote_media_path))
        quoted_paths = " ".join(_remote_quote(path) for path in remote_paths)
        try:
            deleted = _run_adb_command(
                [adb_executable, "-s", device_serial, "shell", f"rm -f {quoted_paths}"],
                description=f"deleting Android media row {item.row_id}",
                timeout=60,
                runner=runner,
            )
        except RuntimeError as exc:
            result.failed += 1
            _append_error(result, f"delete failed for row {item.row_id}: {exc}")
            continue
        if int(getattr(deleted, "returncode", 1) or 0) != 0:
            result.failed += 1
            _append_error(
                result,
                _combined_output(deleted)
                or f"delete failed for row {item.row_id}: {remote_media_path}",
            )
            continue

        for remote_path in remote_paths:
            _rescan_android_media(adb_executable, device_serial, remote_path, runner)
        result.copied += 1
        result.copied_files.append(remote_media_path)
        log.info(
            "Android delete item completed: row_id=%s remote=%s",
            item.row_id,
            remote_media_path,
        )

    if result.failed:
        result.message = f"deleted {result.copied}, failed {result.failed}"
    else:
        result.message = f"deleted {result.copied}"
    log.info(
        "Android delete finished: attempted=%s deleted=%s failed=%s device=%s",
        result.attempted,
        result.copied,
        result.failed,
        result.device_serial or "none",
    )
    return result


def sync_items_to_android(
    items: Iterable[AndroidSyncItem],
    config: AndroidSyncConfig,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> AndroidSyncResult:
    result = AndroidSyncResult(message="disabled")
    if not config.enabled:
        log.info("Android transfer skipped: disabled")
        return result

    sync_items = list(items)[: config.max_items]
    if not sync_items:
        result.message = "no unplayed media to transfer"
        log.info("Android transfer skipped: no unplayed media items selected")
        return result

    adb_executable = (
        shutil.which(config.adb_path)
        if not Path(config.adb_path).is_absolute()
        else config.adb_path
    )
    if not adb_executable:
        result.message = f"adb not found: {config.adb_path}"
        result.failed += 1
        _append_error(result, result.message)
        return result

    destination = config.destination.rstrip("/") or "/sdcard/Movies/GetOffline"
    log.info(
        "Android transfer starting: items=%s destination=%s adb=%s include_subtitles=%s",
        len(sync_items),
        destination,
        adb_executable,
        "yes" if config.include_subtitles else "no",
    )

    try:
        device_serial = find_connected_device(
            adb_executable,
            runner=runner,
            connection_mode=config.connection_mode,
            wifi_address=config.wifi_address,
        )
    except RuntimeError as exc:
        result.message = f"adb device check failed: {exc}"
        result.failed += 1
        _append_error(result, str(exc))
        return result

    if not device_serial:
        if str(config.connection_mode or "usb").strip().lower() == "wifi":
            result.message = "no authorized Android Wi-Fi device connected"
            _append_error(
                result,
                "no authorized Android Wi-Fi device connected; check the Wi-Fi address and adb pairing",
            )
        else:
            result.message = "no authorized Android device connected"
            _append_error(
                result,
                "no authorized Android device connected; check USB debugging authorization",
            )
        return result

    result.device_serial = device_serial
    vlc_playlist_entries: list[tuple[AndroidSyncItem, str]] = []
    syncdb_paths: set[str] = set()
    syncdb_changed = False
    try:
        mkdir = _run_adb_command(
            [
                adb_executable,
                "-s",
                device_serial,
                "shell",
                f"mkdir -p {_remote_quote(destination)}",
            ],
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

    syncdb_paths = _read_remote_syncdb(
        adb_executable, device_serial, destination, runner
    )

    for item in sync_items:
        result.attempted += 1
        local_path = item.file_path.expanduser().resolve()
        if not local_path.exists() or not local_path.is_file():
            result.failed += 1
            _append_error(
                result, f"missing local file for row {item.row_id}: {local_path}"
            )
            continue

        remote_media_path = build_remote_media_path(destination, item)
        log.info(
            "Android transfer item starting: row_id=%s local=%s remote=%s size_bytes=%s",
            item.row_id,
            local_path,
            remote_media_path,
            local_path.stat().st_size,
        )
        if remote_media_path in syncdb_paths:
            result.skipped += 1
            vlc_playlist_entries.append((item, remote_media_path))
            log.info(
                "Android transfer item skipped: row_id=%s remote path already recorded in transferdb.txt",
                item.row_id,
            )
            continue

        remote_exists = False
        try:
            exists = _run_adb_command(
                [
                    adb_executable,
                    "-s",
                    device_serial,
                    "shell",
                    f"test -f {_remote_quote(remote_media_path)}",
                ],
                description=f"checking existing Android file for row {item.row_id}",
                timeout=15,
                runner=runner,
            )
        except RuntimeError as exc:
            result.failed += 1
            _append_error(
                result,
                f"unable to check existing Android file for row {item.row_id}: {exc}",
            )
            continue
        if int(getattr(exists, "returncode", 1) or 0) == 0:
            remote_exists = True

        push_source_path = _copy_with_embedded_metadata(local_path, item, runner)
        if remote_exists and push_source_path == local_path:
            result.skipped += 1
            syncdb_paths.add(remote_media_path)
            syncdb_changed = True
            vlc_playlist_entries.append((item, remote_media_path))
            log.info(
                "Android transfer item skipped: row_id=%s remote file already exists and metadata refresh is unavailable",
                item.row_id,
            )
            continue
        if remote_exists:
            log.info(
                "Android transfer item refreshing metadata on existing remote file: row_id=%s",
                item.row_id,
            )
        try:
            pushed = _run_adb_command(
                [
                    adb_executable,
                    "-s",
                    device_serial,
                    "push",
                    str(push_source_path),
                    remote_media_path,
                ],
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
            _append_error(
                result,
                _combined_output(pushed)
                or f"push failed for row {item.row_id}: {local_path}",
            )
            continue

        _rescan_android_media(adb_executable, device_serial, remote_media_path, runner)
        result.copied += 1
        result.copied_files.append(remote_media_path)
        syncdb_paths.add(remote_media_path)
        syncdb_changed = True
        vlc_playlist_entries.append((item, remote_media_path))
        log.info(
            "Android transfer item copied: row_id=%s remote=%s",
            item.row_id,
            remote_media_path,
        )

        if not config.include_subtitles or not item.subtitle_path:
            continue

        subtitle_path = item.subtitle_path.expanduser().resolve()
        if not subtitle_path.exists():
            log.info(
                "Android transfer subtitle skipped: row_id=%s subtitle missing path=%s",
                item.row_id,
                subtitle_path,
            )
            continue
        if subtitle_path.suffix.lower() not in MEDIA_SUBTITLE_EXTENSIONS:
            log.info(
                "Android transfer subtitle skipped: row_id=%s unsupported extension path=%s",
                item.row_id,
                subtitle_path,
            )
            continue

        remote_subtitle_path = str(
            Path(remote_media_path).with_suffix(subtitle_path.suffix)
        ).replace("\\", "/")
        try:
            subtitle_push = _run_adb_command(
                [
                    adb_executable,
                    "-s",
                    device_serial,
                    "push",
                    str(subtitle_path),
                    remote_subtitle_path,
                ],
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
                _combined_output(subtitle_push)
                or f"subtitle push failed for row {item.row_id}: {subtitle_path}",
            )
        else:
            log.info(
                "Android transfer subtitle copied: row_id=%s remote=%s",
                item.row_id,
                remote_subtitle_path,
            )

    if syncdb_changed or not syncdb_paths:
        _write_remote_syncdb(
            adb_executable, device_serial, destination, syncdb_paths, result, runner
        )

    if vlc_playlist_entries:
        remote_playlist_path = f"{destination}/GetOffline.xspf"
        playlist_content = build_vlc_xspf(vlc_playlist_entries)
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".xspf", delete=False
            ) as handle:
                handle.write(playlist_content)
                local_playlist_path = Path(handle.name)
            playlist_push = _run_adb_command(
                [
                    adb_executable,
                    "-s",
                    device_serial,
                    "push",
                    str(local_playlist_path),
                    remote_playlist_path,
                ],
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
                    _combined_output(playlist_push)
                    or "VLC playlist metadata push failed",
                )
            else:
                result.vlc_playlist_path = remote_playlist_path
                log.info(
                    "Android transfer VLC playlist metadata copied: remote=%s entries=%s",
                    remote_playlist_path,
                    len(vlc_playlist_entries),
                )
        finally:
            try:
                local_playlist_path.unlink(missing_ok=True)
            except (NameError, OSError):
                pass

    if result.failed:
        result.message = (
            f"copied {result.copied}, skipped {result.skipped}, failed {result.failed}"
        )
    else:
        result.message = f"copied {result.copied}, skipped {result.skipped}"
    log.info(
        "Android transfer finished: attempted=%s copied=%s skipped=%s failed=%s device=%s",
        result.attempted,
        result.copied,
        result.skipped,
        result.failed,
        result.device_serial or "none",
    )
    return result


def _copy_item_to_directory(
    item: AndroidSyncItem,
    destination_path: Path,
    result: AndroidSyncResult,
    runner: Callable[..., subprocess.CompletedProcess],
) -> bool:
    source_path = item.file_path.expanduser().resolve()
    if not source_path.is_file():
        result.failed += 1
        _append_error(
            result,
            f"missing local file for row {item.row_id}: {source_path}",
            "Directory transfer",
        )
        return False
    if (
        destination_path.is_file()
        and destination_path.stat().st_size == source_path.stat().st_size
    ):
        result.skipped += 1
        return True
    copy_source = _copy_with_embedded_metadata(source_path, item, runner)
    try:
        shutil.copy2(copy_source, destination_path)
        result.copied += 1
        result.copied_files.append(str(destination_path))
        return True
    except OSError as exc:
        result.failed += 1
        _append_error(
            result, f"copy failed for row {item.row_id}: {exc}", "Directory transfer"
        )
        return False
    finally:
        if copy_source != source_path:
            copy_source.unlink(missing_ok=True)


def _copy_subtitle_to_directory(
    item: AndroidSyncItem, destination_path: Path, result: AndroidSyncResult
) -> None:
    if not item.subtitle_path:
        return
    subtitle_source = item.subtitle_path.expanduser().resolve()
    if not subtitle_source.is_file():
        return
    subtitle_destination = destination_path.with_suffix(subtitle_source.suffix)
    try:
        if (
            not subtitle_destination.is_file()
            or subtitle_destination.stat().st_size != subtitle_source.stat().st_size
        ):
            shutil.copy2(subtitle_source, subtitle_destination)
    except OSError as exc:
        result.failed += 1
        _append_error(
            result,
            f"subtitle copy failed for row {item.row_id}: {exc}",
            "Directory transfer",
        )


def _write_directory_playlist(
    destination: Path,
    entries: list[tuple[AndroidSyncItem, str]],
    result: AndroidSyncResult,
) -> None:
    if not entries:
        return
    playlist_path = destination / "GetOffline.xspf"
    try:
        playlist_path.write_text(build_vlc_xspf(entries), encoding="utf-8")
        result.vlc_playlist_path = str(playlist_path)
    except OSError as exc:
        result.failed += 1
        _append_error(result, f"VLC playlist write failed: {exc}", "Directory transfer")


def sync_items(
    items: Iterable[AndroidSyncItem],
    config: AndroidSyncConfig,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> AndroidSyncResult:
    return sync_items_to_android(items, config, runner)
