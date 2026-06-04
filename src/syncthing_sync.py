from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from android_sync import AndroidSyncItem, build_vlc_xspf
from logger import get_logger

log = get_logger("syncthing_sync")

MEDIA_SUBTITLE_EXTENSIONS = {".srt", ".vtt"}
MANAGED_IGNORE_FILE = ".stignore-getoffline"
SYNCTHING_IGNORE_FILE = ".stignore"
MANAGED_BLOCK_BEGIN = "// BEGIN GetOffline managed Syncthing Android sync"
MANAGED_BLOCK_END = "// END GetOffline managed Syncthing Android sync"


@dataclass
class SyncthingAndroidSyncConfig:
    enabled: bool = False
    android_destination: str = "/sdcard/Movies/GetOffline"
    max_items: int = 10
    include_subtitles: bool = True
    include_unplayed: bool = True
    include_started: bool = True
    include_played: bool = False
    exclude_regex: str = ""


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
    ignore_path: Optional[str] = None


@dataclass(frozen=True)
class _SelectedSyncthingFile:
    item: AndroidSyncItem
    local_path: Path
    relative_path: str
    android_path: str


def _coerce_bool(value: object, fallback: bool = False) -> bool:
    if value is None:
        return fallback
    text = str(value).strip().lower()
    if not text:
        return fallback
    return text in {"1", "true", "yes", "on"}


def _coerce_int(value: object, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def config_from_defaults(defaults: dict) -> SyncthingAndroidSyncConfig:
    max_items = _coerce_int(defaults.get("syncthing_android_sync_max_items"), 10)
    return SyncthingAndroidSyncConfig(
        enabled=_coerce_bool(defaults.get("syncthing_android_sync_enabled")),
        android_destination=str(defaults.get("syncthing_android_sync_android_destination") or "/sdcard/Movies/GetOffline").strip()
        or "/sdcard/Movies/GetOffline",
        max_items=max(1, max_items),
        include_subtitles=_coerce_bool(defaults.get("syncthing_android_sync_include_subtitles"), True),
        include_unplayed=_coerce_bool(defaults.get("syncthing_android_sync_include_unplayed"), True),
        include_started=_coerce_bool(defaults.get("syncthing_android_sync_include_started"), True),
        include_played=_coerce_bool(defaults.get("syncthing_android_sync_include_played"), False),
        exclude_regex=str(defaults.get("syncthing_android_sync_exclude_regex") or "").strip(),
    )


def _append_error(result: SyncthingAndroidSyncResult, message: str) -> None:
    result.errors.append(message)
    log.warning("Syncthing Android sync: %s", message)


def _sync_root_from_config(output_root: Path) -> Path:
    return output_root.expanduser().resolve()


def _relative_posix_path(sync_root: Path, local_path: Path) -> Optional[str]:
    try:
        return local_path.expanduser().resolve().relative_to(sync_root.expanduser().resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _android_path_for_relative(android_destination: str, relative_path: str) -> str:
    destination = android_destination.rstrip("/") or "/sdcard/Movies/GetOffline"
    return f"{destination}/{relative_path.strip('/')}"


def _escape_syncthing_pattern_path(relative_path: str) -> str:
    escaped = []
    for char in relative_path.replace("\\", "/"):
        if char in {"\\", "*", "?", "[", "]", "{", "}"}:
            escaped.append("\\" + char)
        else:
            escaped.append(char)
    return "".join(escaped)


def _ignore_pattern_for_relative_path(relative_path: str) -> str:
    return "!/" + _escape_syncthing_pattern_path(relative_path)


def _manifest_path(sync_root: Path) -> Path:
    return sync_root / "syncdb.txt"


def _managed_ignore_path(sync_root: Path) -> Path:
    return sync_root / MANAGED_IGNORE_FILE


def _syncthing_ignore_path(sync_root: Path) -> Path:
    return sync_root / SYNCTHING_IGNORE_FILE


def _write_manifest(sync_root: Path, synced_files: Iterable[str]) -> None:
    path = _manifest_path(sync_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(f"{name}\n" for name in sorted({str(name).strip() for name in synced_files if str(name).strip()}))
    path.write_text(payload, encoding="utf-8")


def _write_playlist(sync_root: Path, playlist_entries: List[Tuple[AndroidSyncItem, str]]) -> Optional[Path]:
    if not playlist_entries:
        playlist_path = sync_root / "GetOffline.xspf"
        playlist_path.unlink(missing_ok=True)
        return None
    playlist_path = sync_root / "GetOffline.xspf"
    playlist_path.write_text(build_vlc_xspf(playlist_entries), encoding="utf-8")
    return playlist_path


def _managed_ignore_content(included_relative_paths: Iterable[str]) -> str:
    patterns = [_ignore_pattern_for_relative_path(path) for path in sorted({str(path).strip("/") for path in included_relative_paths if str(path).strip("/")})]
    lines = [
        "// Generated by GetOffline. Edit Syncthing Android settings instead of this file.",
        "// This include-list syncs only the selected downloads from the Syncthing folder root.",
        "#escape=\\",
        *patterns,
        "*",
        "",
    ]
    return "\n".join(lines)


def _install_managed_include(sync_root: Path) -> Path:
    stignore_path = _syncthing_ignore_path(sync_root)
    include_block = f"{MANAGED_BLOCK_BEGIN}\n#include {MANAGED_IGNORE_FILE}\n{MANAGED_BLOCK_END}"
    try:
        existing = stignore_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        stignore_path.write_text(include_block + "\n", encoding="utf-8")
        return stignore_path

    start = existing.find(MANAGED_BLOCK_BEGIN)
    end = existing.find(MANAGED_BLOCK_END)
    if start >= 0 and end >= start:
        end += len(MANAGED_BLOCK_END)
        updated = existing[:start] + include_block + existing[end:]
    else:
        separator = "" if existing.startswith("\n") or not existing else "\n\n"
        updated = include_block + separator + existing
    stignore_path.write_text(updated, encoding="utf-8")
    return stignore_path


def _write_managed_ignore(sync_root: Path, included_relative_paths: Iterable[str]) -> Path:
    managed_path = _managed_ignore_path(sync_root)
    managed_path.write_text(_managed_ignore_content(included_relative_paths), encoding="utf-8")
    _install_managed_include(sync_root)
    return managed_path


def _selected_media_file(item: AndroidSyncItem, sync_root: Path, android_destination: str, result: SyncthingAndroidSyncResult) -> Optional[_SelectedSyncthingFile]:
    source_path = item.file_path.expanduser().resolve()
    if not source_path.exists() or not source_path.is_file():
        result.failed += 1
        _append_error(result, f"missing local file for row {item.row_id}: {source_path}")
        return None
    relative_path = _relative_posix_path(sync_root, source_path)
    if relative_path is None:
        result.failed += 1
        _append_error(result, f"file for row {item.row_id} is outside the configured Syncthing folder root: {source_path}")
        return None
    return _SelectedSyncthingFile(
        item=item,
        local_path=source_path,
        relative_path=relative_path,
        android_path=_android_path_for_relative(android_destination, relative_path),
    )


def _selected_subtitle_file(item: AndroidSyncItem, sync_root: Path, android_destination: str, result: SyncthingAndroidSyncResult) -> Optional[_SelectedSyncthingFile]:
    if not item.subtitle_path:
        return None
    subtitle_path = item.subtitle_path.expanduser().resolve()
    if not subtitle_path.exists() or not subtitle_path.is_file():
        log.info("Syncthing Android sync subtitle skipped: row_id=%s subtitle missing path=%s", item.row_id, subtitle_path)
        return None
    if subtitle_path.suffix.lower() not in MEDIA_SUBTITLE_EXTENSIONS:
        log.info("Syncthing Android sync subtitle skipped: row_id=%s unsupported extension path=%s", item.row_id, subtitle_path)
        return None
    relative_path = _relative_posix_path(sync_root, subtitle_path)
    if relative_path is None:
        _append_error(result, f"subtitle for row {item.row_id} is outside the configured Syncthing folder root: {subtitle_path}")
        return None
    return _SelectedSyncthingFile(
        item=item,
        local_path=subtitle_path,
        relative_path=relative_path,
        android_path=_android_path_for_relative(android_destination, relative_path),
    )


def sync_items_to_syncthing_android(
    items: Iterable[AndroidSyncItem],
    config: SyncthingAndroidSyncConfig,
    *,
    output_root: Path,
) -> SyncthingAndroidSyncResult:
    result = SyncthingAndroidSyncResult(message="disabled")
    if not config.enabled:
        log.info("Syncthing Android sync skipped: disabled")
        return result

    sync_items = list(items)[: config.max_items]
    sync_root = _sync_root_from_config(output_root)
    android_destination = config.android_destination.rstrip("/") or "/sdcard/Movies/GetOffline"

    try:
        sync_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.failed += 1
        result.message = f"unable to prepare Syncthing folder root: {exc}"
        _append_error(result, result.message)
        return result

    log.info(
        "Syncthing Android sync starting: items=%s sync_root=%s android_destination=%s include_subtitles=%s",
        len(sync_items),
        sync_root,
        android_destination,
        "yes" if config.include_subtitles else "no",
    )

    included_paths: set[str] = set()
    playlist_entries: List[Tuple[AndroidSyncItem, str]] = []
    selected_files: List[_SelectedSyncthingFile] = []

    for item in sync_items:
        result.attempted += 1
        selected_media = _selected_media_file(item, sync_root, android_destination, result)
        if selected_media is None:
            continue
        selected_files.append(selected_media)
        included_paths.add(selected_media.relative_path)
        playlist_entries.append((item, selected_media.android_path))
        result.copied += 1
        result.copied_files.append(str(selected_media.local_path))

        if config.include_subtitles:
            selected_subtitle = _selected_subtitle_file(item, sync_root, android_destination, result)
            if selected_subtitle is not None:
                selected_files.append(selected_subtitle)
                included_paths.add(selected_subtitle.relative_path)

    try:
        playlist_path = _write_playlist(sync_root, playlist_entries)
        if playlist_path is not None:
            result.playlist_path = str(playlist_path)
            included_paths.add(playlist_path.relative_to(sync_root).as_posix())
        _write_manifest(sync_root, included_paths)
        included_paths.add(_manifest_path(sync_root).relative_to(sync_root).as_posix())
        result.ignore_path = str(_write_managed_ignore(sync_root, included_paths))
    except OSError as exc:
        result.failed += 1
        _append_error(result, f"Syncthing include-list write failed: {exc}")

    if result.failed:
        result.message = f"selected {len(selected_files)}, failed {result.failed}"
    else:
        result.message = f"selected {len(selected_files)}"
    log.info(
        "Syncthing Android sync finished: attempted=%s selected=%s failed=%s sync_root=%s",
        result.attempted,
        len(selected_files),
        result.failed,
        sync_root,
    )
    return result
