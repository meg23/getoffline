import http.cookiejar
import os

import browser_cookie3
import yaml

from database import init_database, load_runtime_config, resolve_database_path, store_runtime_config
from logger import get_logger


log = get_logger("config")


def load_config():
    with open("config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    defaults = config["defaults"]
    defaults["output_root"] = os.path.expanduser(defaults["output_root"])
    defaults["cookie_path"] = os.path.expanduser(defaults["cookie_path"])
    defaults["database_path"] = resolve_database_path(defaults)
    init_database(defaults["database_path"])

    try:
        cj = browser_cookie3.chrome(domain_name="youtube.com")
        cookie_jar = http.cookiejar.MozillaCookieJar(defaults["cookie_path"])
        for cookie in cj:
            cookie_jar.set_cookie(cookie)
        cookie_jar.save(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        log.warning(f"Could not export Chrome cookies: {exc}")

    store_runtime_config(defaults["database_path"], config)
    db_config = load_runtime_config(defaults["database_path"])

    return db_config
