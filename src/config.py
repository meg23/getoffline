import os
from pathlib import Path
from typing import Any, Dict, Optional

from database import (
    DEFAULT_APP_CONFIG,
    ensure_config_seeded,
    get_stored_config,
    init_database,
    materialize_youtube_cookie_file,
    resolve_database_path,
)


CONFIG_FILE_NAME = "config.yml"


def _resolve_path(value: Any, *, base_dir: Path) -> str:
    raw = str(value or "").strip()
    expanded = os.path.expandvars(os.path.expanduser(raw))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return str(candidate.resolve())


def _load_yaml_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    path = config_path or (Path.cwd() / CONFIG_FILE_NAME)
    if not path.is_file():
        return {}

    defaults: Dict[str, Any] = {}
    in_defaults = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped == "defaults:":
            in_defaults = True
            continue
        if indent == 0:
            in_defaults = False
        if ":" not in stripped:
            raise ValueError(f"{path} contains an unsupported YAML line: {raw_line}")
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key not in {"output_root", "database_path"}:
            continue
        if in_defaults or indent == 0:
            defaults[key] = value
    return {"path": path, "defaults": defaults or {}}


def _build_bootstrap_defaults(config_path: Optional[Path] = None):
    defaults = dict(DEFAULT_APP_CONFIG)
    file_config = _load_yaml_config(config_path)
    config_dir = file_config.get("path", Path.cwd()).parent if file_config else Path.cwd()

    for key in ("output_root", "database_path"):
        configured_value = file_config.get("defaults", {}).get(key)
        if configured_value is not None:
            defaults[key] = configured_value

    env_output_root = os.getenv("GETOFFLINE_OUTPUT_ROOT")
    if env_output_root:
        defaults["output_root"] = env_output_root

    env_database_path = os.getenv("GETOFFLINE_DATABASE_PATH")
    if env_database_path:
        defaults["database_path"] = env_database_path

    defaults["output_root"] = _resolve_path(defaults["output_root"], base_dir=config_dir)
    defaults["database_path"] = resolve_database_path(defaults, base_dir=str(config_dir))
    return defaults


def load_config(config_path: Optional[Path] = None):
    defaults = _build_bootstrap_defaults(config_path)
    init_database(defaults["database_path"])
    ensure_config_seeded(defaults["database_path"], defaults)

    materialize_youtube_cookie_file(defaults["database_path"])
    persisted = get_stored_config(defaults["database_path"])
    return {
        "defaults": persisted["defaults"],
        "download_settings": persisted["download_settings"],
        "youtube": persisted["youtube"],
        "podcasts": persisted["podcasts"],
    }
