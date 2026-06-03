import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from logger import get_logger

log = get_logger("android_sync")

MEDIA_SUBTITLE_EXTENSIONS = {".srt", ".vtt"}


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


def build_remote_media_path(destination: str, item: AndroidSyncItem) -> str:
    source = _safe_remote_component(item.source_name)
    title = _safe_remote_component(item.title or item.file_path.stem)
    suffix = item.file_path.suffix or ""
    return f"{destination.rstrip('/')}/{source} - {title}{suffix}"


def find_connected_device(adb_path: str, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> Optional[str]:
    adb_executable = shutil.which(adb_path) if not Path(adb_path).is_absolute() else adb_path
    if not adb_executable:
        return None
    completed = runner(
        [adb_executable, "devices"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    for line in completed.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            return parts[0]
    return None


def sync_items_to_android(
    items: Iterable[AndroidSyncItem],
    config: AndroidSyncConfig,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> AndroidSyncResult:
    result = AndroidSyncResult(message="disabled")
    if not config.enabled:
        return result

    adb_executable = shutil.which(config.adb_path) if not Path(config.adb_path).is_absolute() else config.adb_path
    if not adb_executable:
        result.message = f"adb not found: {config.adb_path}"
        result.failed += 1
        return result

    try:
        device_serial = find_connected_device(adb_executable, runner=runner)
    except (OSError, subprocess.SubprocessError) as exc:
        result.message = f"adb device check failed: {exc}"
        result.failed += 1
        result.errors.append(str(exc))
        return result

    if not device_serial:
        result.message = "no authorized Android device connected"
        return result

    result.device_serial = device_serial
    destination = config.destination.rstrip("/") or "/sdcard/Movies/GetOffline"
    try:
        runner(
            [adb_executable, "-s", device_serial, "shell", "mkdir", "-p", destination],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result.message = f"unable to prepare Android folder: {exc}"
        result.failed += 1
        result.errors.append(str(exc))
        return result

    for item in list(items)[: config.max_items]:
        result.attempted += 1
        local_path = item.file_path.expanduser().resolve()
        if not local_path.exists() or not local_path.is_file():
            result.failed += 1
            result.errors.append(f"missing local file: {local_path}")
            continue

        remote_media_path = build_remote_media_path(destination, item)
        exists = runner(
            [adb_executable, "-s", device_serial, "shell", "test", "-f", remote_media_path],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        if exists.returncode == 0:
            result.skipped += 1
            continue

        pushed = runner(
            [adb_executable, "-s", device_serial, "push", str(local_path), remote_media_path],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
        if pushed.returncode != 0:
            result.failed += 1
            result.errors.append(pushed.stderr.strip() or pushed.stdout.strip() or f"push failed: {local_path}")
            continue

        result.copied += 1
        result.copied_files.append(remote_media_path)

        if config.include_subtitles and item.subtitle_path:
            subtitle_path = item.subtitle_path.expanduser().resolve()
            if subtitle_path.exists() and subtitle_path.suffix.lower() in MEDIA_SUBTITLE_EXTENSIONS:
                remote_subtitle_path = str(Path(remote_media_path).with_suffix(subtitle_path.suffix)).replace("\\", "/")
                subtitle_push = runner(
                    [adb_executable, "-s", device_serial, "push", str(subtitle_path), remote_subtitle_path],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=120,
                    check=False,
                )
                if subtitle_push.returncode != 0:
                    result.errors.append(
                        subtitle_push.stderr.strip() or subtitle_push.stdout.strip() or f"subtitle push failed: {subtitle_path}"
                    )

    if result.failed:
        result.message = f"copied {result.copied}, skipped {result.skipped}, failed {result.failed}"
    else:
        result.message = f"copied {result.copied}, skipped {result.skipped}"
    return result
