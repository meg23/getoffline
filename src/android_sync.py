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
            result.skipped += 1
            log.info("Android sync item skipped: row_id=%s remote file already exists", item.row_id)
            continue

        try:
            pushed = _run_adb_command(
                [adb_executable, "-s", device_serial, "push", str(local_path), remote_media_path],
                description=f"pushing media row {item.row_id}",
                timeout=300,
                runner=runner,
            )
        except RuntimeError as exc:
            result.failed += 1
            _append_error(result, f"push failed for row {item.row_id}: {exc}")
            continue
        if int(getattr(pushed, "returncode", 1) or 0) != 0:
            result.failed += 1
            _append_error(result, _combined_output(pushed) or f"push failed for row {item.row_id}: {local_path}")
            continue

        result.copied += 1
        result.copied_files.append(remote_media_path)
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
