import http.cookiejar
import os

import browser_cookie3
import yaml

from logger import get_logger


log = get_logger("config")

DEFAULT_AD_SCRUBBER = {
    "enabled": True,
    "model": "base",
    "min_ad_seconds": 8.0,
    "pre_roll": 2.0,
    "post_roll": 2.0,
    "min_hits": 1,
    "subtitle_time_offset_seconds": -0.2,
}


def load_config():
    with open("config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    defaults = config["defaults"]
    defaults["output_root"] = os.path.expanduser(defaults["output_root"])
    defaults["cookie_path"] = os.path.expanduser(defaults["cookie_path"])

    scrubber_cfg = dict(DEFAULT_AD_SCRUBBER)
    scrubber_cfg.update(defaults.get("ad_scrubber", {}))
    defaults["ad_scrubber"] = scrubber_cfg

    try:
        cj = browser_cookie3.chrome(domain_name="youtube.com")
        cookie_jar = http.cookiejar.MozillaCookieJar(defaults["cookie_path"])
        for cookie in cj:
            cookie_jar.set_cookie(cookie)
        cookie_jar.save(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        log.warning(f"Could not export Chrome cookies: {exc}")

    return config
