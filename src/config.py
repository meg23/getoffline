import os

import yaml

from database import (
    ensure_config_seeded,
    get_stored_config,
    materialize_youtube_cookie_file,
    resolve_database_path,
)


def load_config():
    with open("config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    defaults = config["defaults"]
    defaults["output_root"] = os.path.expanduser(defaults["output_root"])
    defaults["database_path"] = resolve_database_path(defaults)
    ensure_config_seeded(defaults["database_path"], defaults)

    materialize_youtube_cookie_file(defaults["database_path"])
    persisted = get_stored_config(defaults["database_path"])
    return {
        "defaults": persisted["defaults"],
        "download_settings": persisted["download_settings"],
        "youtube": persisted["youtube"],
        "podcasts": persisted["podcasts"],
    }
