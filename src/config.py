import os

from database import (
    DEFAULT_APP_CONFIG,
    ensure_config_seeded,
    get_stored_config,
    materialize_youtube_cookie_file,
    resolve_database_path,
)


def _build_bootstrap_defaults():
    defaults = dict(DEFAULT_APP_CONFIG)
    env_output_root = os.getenv("GETOFFLINE_OUTPUT_ROOT")
    if env_output_root:
        defaults["output_root"] = env_output_root

    env_database_path = os.getenv("GETOFFLINE_DATABASE_PATH")
    if env_database_path:
        defaults["database_path"] = env_database_path

    defaults["output_root"] = os.path.expanduser(defaults["output_root"])
    defaults["database_path"] = resolve_database_path(defaults)
    return defaults


def load_config():
    defaults = _build_bootstrap_defaults()
    ensure_config_seeded(defaults["database_path"], defaults)

    materialize_youtube_cookie_file(defaults["database_path"])
    persisted = get_stored_config(defaults["database_path"])
    return {
        "defaults": persisted["defaults"],
        "download_settings": persisted["download_settings"],
        "youtube": persisted["youtube"],
        "podcasts": persisted["podcasts"],
    }
