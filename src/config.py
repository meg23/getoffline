import os
import yaml
import browser_cookie3
import http.cookiejar

def load_config():
    with open("config.yaml") as f:
        config = yaml.safe_load(f)

    defaults = config["defaults"]
    config["defaults"]["output_root"] = os.path.expanduser(defaults["output_root"])
    config["defaults"]["cookie_path"] = os.path.expanduser(defaults["cookie_path"])

    # Save browser cookies to file
    cj = browser_cookie3.chrome(domain_name="youtube.com")
    cookie_jar = http.cookiejar.MozillaCookieJar(config["defaults"]["cookie_path"])
    for cookie in cj:
        cookie_jar.set_cookie(cookie)
    cookie_jar.save(ignore_discard=True, ignore_expires=True)

    return config

