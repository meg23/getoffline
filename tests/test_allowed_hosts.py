import os
import subprocess
import sys


def _load_allowed_hosts(
    env_value: str | None, *, strict_allowed_hosts: bool = False
) -> list[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    env["DJANGO_SETTINGS_MODULE"] = "app.settings"
    env["GETOFFLINE_TEST_IN_MEMORY_DB"] = "1"
    env["GETOFFLINE_DJANGO_STRICT_ALLOWED_HOSTS"] = (
        "1" if strict_allowed_hosts else "0"
    )
    if env_value is None:
        env.pop("GETOFFLINE_DJANGO_ALLOWED_HOSTS", None)
    else:
        env["GETOFFLINE_DJANGO_ALLOWED_HOSTS"] = env_value
    script = "import app.settings; print(','.join(app.settings.ALLOWED_HOSTS))"
    output = subprocess.check_output([sys.executable, "-c", script], env=env, text=True)
    return output.strip().split(",")


def test_default_allowed_hosts_accept_lan_addresses() -> None:
    assert "*" in _load_allowed_hosts(None)


def test_compose_default_allowed_hosts_accept_lan_addresses() -> None:
    assert "*" in _load_allowed_hosts("localhost,127.0.0.1,frontend,api")


def test_custom_allowed_hosts_accept_lan_addresses_by_default() -> None:
    assert "*" in _load_allowed_hosts("localhost,127.0.0.1")


def test_custom_allowed_hosts_can_opt_into_strict_validation() -> None:
    assert _load_allowed_hosts("localhost,127.0.0.1", strict_allowed_hosts=True) == [
        "localhost",
        "127.0.0.1",
    ]
