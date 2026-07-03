import os
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from workers.download_store import DEFAULT_APP_CONFIG
from workers.download_store import ensure_config_seeded
from workers.download_store import get_stored_config
from workers.download_store import init_database
from workers.download_store import materialize_youtube_cookie_file
from workers.download_store import resolve_database_path

CONFIG_FILE_NAME = "config.yml"


@dataclass(frozen=True)
class YamlConfig:
    path: Path
    defaults: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BootstrapConfig:
    defaults: dict[str, Any]
    download_settings: dict[str, Any]
    youtube: list[dict[str, Any]]
    podcasts: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "defaults": self.defaults,
            "download_settings": self.download_settings,
            "youtube": self.youtube,
            "podcasts": self.podcasts,
        }


def _resolve_path(value: Any, *, base_dir: Path) -> str:
    raw = str(value or "").strip()
    expanded = os.path.expandvars(os.path.expanduser(raw))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return str(candidate.resolve())


def _load_yaml_config(config_path: Path | None = None) -> YamlConfig | None:
    path = config_path or (Path.cwd() / CONFIG_FILE_NAME)
    if not path.is_file():
        return None

    defaults: dict[str, Any] = {}
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
    return YamlConfig(path=path, defaults=defaults or {})


def _build_bootstrap_defaults(config_path: Path | None = None):
    defaults = dict(DEFAULT_APP_CONFIG)
    file_config = _load_yaml_config(config_path)
    config_dir = (
        file_config.path.parent if file_config else Path.cwd()
    )

    for key in ("output_root", "database_path"):
        configured_value = file_config.defaults.get(key) if file_config else None
        if configured_value is not None:
            defaults[key] = configured_value

    defaults["output_root"] = _resolve_path(
        defaults["output_root"], base_dir=config_dir
    )
    defaults["database_path"] = resolve_database_path(
        defaults, base_dir=str(config_dir)
    )
    return defaults


def load_bootstrap_config(config_path: Path | None = None):
    return BootstrapConfig(
        defaults=_build_bootstrap_defaults(config_path),
        download_settings={"youtube_cookie_text": None},
        youtube=[],
        podcasts=[],
    ).as_dict()


def load_config(config_path: Path | None = None):
    defaults = _build_bootstrap_defaults(config_path)
    init_database(defaults["database_path"])
    ensure_config_seeded(defaults["database_path"], defaults)

    materialize_youtube_cookie_file(defaults["database_path"])
    persisted = get_stored_config(defaults["database_path"])
    return BootstrapConfig(
        defaults=persisted["defaults"],
        download_settings=persisted["download_settings"],
        youtube=persisted["youtube"],
        podcasts=persisted["podcasts"],
    ).as_dict()
