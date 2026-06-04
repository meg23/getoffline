import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from android_sync import AndroidSyncItem, build_remote_media_path, build_vlc_xspf
from logger import get_logger

log = get_logger("syncthing_sync")

MEDIA_SUBTITLE_EXTENSIONS = {".srt", ".vtt"}


@dataclass
class SyncthingAndroidSyncConfig:
    enabled: bool = False
    local_sync_folder: str = "./syncthing-android"
    android_destination: str = "/sdcard/Movies/GetOffline"
    max_items: int = 10
    include_subtitles: bool = True
    include_unplayed: bool = True
    include_started: bool = True
    include_played: bool = False
    exclude_regex: str = ""
    prune: bool = True


@dataclass
class SyncthingAndroidSyncResult:
    attempted: int = 0
    copied: int = 0
    skipped: int = 0
    failed: int = 0
    deleted: int = 0
    message: str = "idle"
    copied_files: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    playlist_path: Optional[str] = None


def _coerce_bool(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _coerce_int(value: object, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def config_from_defaults(defaults: dict) -> SyncthingAndroidSyncConfig:
    max_items = _coerce_int(defaults.get("syncthing_android_sync_max_items"), 10)
    return SyncthingAndroidSyncConfig(
        enabled=_coerce_bool(defaults.get("syncthing_android_sync_enabled")),
        local_sync_folder=str(defaults.get("syncthing_android_sync_local_folder") or "./syncthing-android").strip()
        or "./syncthing-android",
        android_destination=str(defaults.get("syncthing_android_sync_android_destination") or "/sdcard/Movies/GetOffline").strip()
        or "/sdcard/Movies/GetOffline",
        max_items=max(1, max_items),
        include_subtitles=_coerce_bool(defaults.get("syncthing_android_sync_include_subtitles", "1")),
        include_unplayed=_coerce_bool(defaults.get("syncthing_android_sync_include_unplayed", "1")),
        include_started=_coerce_bool(defaults.get("syncthing_android_sync_include_started", "1")),
        include_played=_coerce_bool(defaults.get("syncthing_android_sync_include_played", "0")),
        exclude_regex=str(defaults.get("syncthing_android_sync_exclude_regex") or "").strip(),
        prune=_coerce_bool(defaults.get("syncthing_android_sync_prune", "1")),
    )


def _append_error(result: SyncthingAndroidSyncResult, message: str) -> None:
    result.errors.append(message)
    log.warning("Syncthing Android sync: %s", message)


def _relative_name_for_android_path(android_destination: str, android_path: str) -> str:
    destination = android_destination.rstrip("/")
    path = str(android_path)
    if path == destination:
        return Path(path).name
    prefix = destination + "/"
    if path.startswith(prefix):
        return path[len(prefix) :]
    return Path(path).name


def _copy_if_needed(source_path: Path, target_path: Path) -> bool:
    if target_path.exists() and target_path.is_file():
        source_stat = source_path.stat()
        target_stat = target_path.stat()
        if target_stat.st_size == source_stat.st_size and target_stat.st_mtime >= source_stat.st_mtime:
            return False
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)
    return True


def _manifest_path(local_folder: Path) -> Path:
    return local_folder / "syncdb.txt"


def _read_manifest(local_folder: Path) -> set[str]:
    path = _manifest_path(local_folder)
    try:
        return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    except FileNotFoundError:
        return set()
    except OSError as exc:
        log.warning("Syncthing Android sync manifest read failed: %s", exc)
        return set()


def _write_manifest(local_folder: Path, synced_files: Iterable[str]) -> None:
    path = _manifest_path(local_folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(f"{name}\n" for name in sorted({str(name).strip() for name in synced_files if str(name).strip()}))
    path.write_text(payload, encoding="utf-8")


def _prune_stale_files(local_folder: Path, previous_files: Iterable[str], current_files: set[str], result: SyncthingAndroidSyncResult) -> None:
    for relative_name in sorted(set(previous_files) - current_files):
        candidate = (local_folder / relative_name).resolve(strict=False)
        try:
            candidate.relative_to(local_folder.resolve())
        except ValueError:
            _append_error(result, f"refusing to prune path outside Syncthing folder: {relative_name}")
            continue
        try:
            if candidate.exists() and candidate.is_file():
                candidate.unlink()
                result.deleted += 1
                log.info("Syncthing Android sync pruned stale file: %s", candidate)
        except OSError as exc:
            _append_error(result, f"unable to prune stale file {relative_name}: {exc}")


def _copy_subtitle(
    item: AndroidSyncItem,
    local_folder: Path,
    remote_media_path: str,
    android_destination: str,
    result: SyncthingAndroidSyncResult,
) -> Optional[str]:
    if not item.subtitle_path:
        return None
    subtitle_path = item.subtitle_path.expanduser().resolve()
    if not subtitle_path.exists() or not subtitle_path.is_file():
        log.info("Syncthing Android sync subtitle skipped: row_id=%s subtitle missing path=%s", item.row_id, subtitle_path)
        return None
    if subtitle_path.suffix.lower() not in MEDIA_SUBTITLE_EXTENSIONS:
        log.info("Syncthing Android sync subtitle skipped: row_id=%s unsupported extension path=%s", item.row_id, subtitle_path)
        return None

    remote_subtitle_path = str(Path(remote_media_path).with_suffix(subtitle_path.suffix)).replace("\\", "/")
    relative_name = _relative_name_for_android_path(android_destination, remote_subtitle_path)
    try:
        copied = _copy_if_needed(subtitle_path, local_folder / relative_name)
    except OSError as exc:
        _append_error(result, f"subtitle copy failed for row {item.row_id}: {exc}")
        return None
    if copied:
        log.info("Syncthing Android sync subtitle copied: row_id=%s target=%s", item.row_id, local_folder / relative_name)
    return relative_name


def sync_items_to_syncthing_android(items: Iterable[AndroidSyncItem], config: SyncthingAndroidSyncConfig) -> SyncthingAndroidSyncResult:
    result = SyncthingAndroidSyncResult(message="disabled")
    if not config.enabled:
        log.info("Syncthing Android sync skipped: disabled")
        return result

    sync_items = list(items)[: config.max_items]
    if not sync_items:
        result.message = "no media to sync"
        log.info("Syncthing Android sync skipped: no media items selected")
        return result

    local_folder = Path(config.local_sync_folder).expanduser().resolve()
    android_destination = config.android_destination.rstrip("/") or "/sdcard/Movies/GetOffline"
    previous_manifest = _read_manifest(local_folder)
    current_manifest: set[str] = set()
    playlist_entries: List[Tuple[AndroidSyncItem, str]] = []

    try:
        local_folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.failed += 1
        result.message = f"unable to prepare Syncthing folder: {exc}"
        _append_error(result, result.message)
        return result

    log.info(
        "Syncthing Android sync starting: items=%s local_folder=%s android_destination=%s include_subtitles=%s",
        len(sync_items),
        local_folder,
        android_destination,
        "yes" if config.include_subtitles else "no",
    )

    for item in sync_items:
        result.attempted += 1
        source_path = item.file_path.expanduser().resolve()
        if not source_path.exists() or not source_path.is_file():
            result.failed += 1
            _append_error(result, f"missing local file for row {item.row_id}: {source_path}")
            continue

        remote_media_path = build_remote_media_path(android_destination, item)
        relative_name = _relative_name_for_android_path(android_destination, remote_media_path)
        target_path = local_folder / relative_name
        try:
            copied = _copy_if_needed(source_path, target_path)
        except OSError as exc:
            result.failed += 1
            _append_error(result, f"copy failed for row {item.row_id}: {exc}")
            continue

        if copied:
            result.copied += 1
            result.copied_files.append(str(target_path))
            log.info("Syncthing Android sync item copied: row_id=%s target=%s", item.row_id, target_path)
        else:
            result.skipped += 1
            log.info("Syncthing Android sync item skipped unchanged: row_id=%s target=%s", item.row_id, target_path)

        current_manifest.add(relative_name)
        playlist_entries.append((item, remote_media_path))

        if config.include_subtitles:
            subtitle_relative_name = _copy_subtitle(item, local_folder, remote_media_path, android_destination, result)
            if subtitle_relative_name:
                current_manifest.add(subtitle_relative_name)

    if config.prune:
        _prune_stale_files(local_folder, previous_manifest, current_manifest, result)

    try:
        _write_manifest(local_folder, current_manifest)
    except OSError as exc:
        result.failed += 1
        _append_error(result, f"Syncthing manifest write failed: {exc}")

    if playlist_entries:
        playlist_path = local_folder / "GetOffline.xspf"
        try:
            playlist_path.write_text(build_vlc_xspf(playlist_entries), encoding="utf-8")
            result.playlist_path = str(playlist_path)
        except OSError as exc:
            _append_error(result, f"VLC playlist write failed: {exc}")

    if result.failed:
        result.message = f"copied {result.copied}, skipped {result.skipped}, deleted {result.deleted}, failed {result.failed}"
    else:
        result.message = f"copied {result.copied}, skipped {result.skipped}, deleted {result.deleted}"
    log.info(
        "Syncthing Android sync finished: attempted=%s copied=%s skipped=%s deleted=%s failed=%s",
        result.attempted,
        result.copied,
        result.skipped,
        result.deleted,
        result.failed,
    )
    return result
