#!/usr/bin/env python3
"""Render GetOffline's Pystrano playbook and deploy the requested revision."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK = REPOSITORY_ROOT / "deploy/getoffline/production/deployment.yml"
DEPLOY_USER = "jellyfin"


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} must be set")
    return value


def deployment_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["LOGNAME"] = DEPLOY_USER
    environment["USER"] = DEPLOY_USER
    return environment


def main() -> None:
    source_code_url = required_environment("GETOFFLINE_SOURCE_CODE_URL")
    revision = required_environment("GETOFFLINE_DEPLOY_REVISION")
    rendered = PLAYBOOK.read_text(encoding="utf-8")
    rendered = rendered.replace("__SOURCE_CODE_URL__", source_code_url)
    rendered = rendered.replace("__REVISION__", revision)

    with tempfile.TemporaryDirectory(prefix="getoffline-pystrano-") as config_root:
        deployment_dir = Path(config_root) / "getoffline/production"
        deployment_dir.mkdir(parents=True)
        (deployment_dir / "deployment.yml").write_text(rendered, encoding="utf-8")
        subprocess.run(
            [
                str(Path(sys.executable).with_name("pystrano")),
                "deploy",
                "production",
                "getoffline",
                "--deploy-config-dir",
                config_root,
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
            env=deployment_environment(),
        )


if __name__ == "__main__":
    main()
