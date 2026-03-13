import http.cookiejar
import os
from pathlib import Path

import browser_cookie3
import yaml

from database import (
    ensure_config_seeded,
    get_stored_config,
    materialize_youtube_cookie_file,
    resolve_database_path,
    update_download_settings,
)
from logger import get_logger


log = get_logger("config")


def load_config():
    with open("config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    defaults = config["defaults"]
    defaults["output_root"] = os.path.expanduser(defaults["output_root"])
    defaults["cookie_path"] = os.path.expanduser(defaults["cookie_path"])
    defaults["database_path"] = resolve_database_path(defaults)
    ensure_config_seeded(defaults["database_path"], defaults)

    try:
        cj = browser_cookie3.chrome(domain_name="youtube.com")
        cookie_jar = http.cookiejar.MozillaCookieJar(defaults["cookie_path"])
        for cookie in cj:
            cookie_jar.set_cookie(cookie)
        cookie_jar.save(ignore_discard=True, ignore_expires=True)
        cookie_text = Path(defaults["cookie_path"]).read_text(encoding="utf-8")
        update_download_settings(defaults["database_path"], cookie_text)
    except Exception as exc:
        log.warning(f"Could not export Chrome cookies: {exc}")

    materialize_youtube_cookie_file(defaults["database_path"], defaults["cookie_path"])
    persisted = get_stored_config(defaults["database_path"])
    config["defaults"] = persisted["defaults"]
    config["download_settings"] = persisted["download_settings"]
    return config
