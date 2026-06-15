#!/usr/bin/env python3
"""Render GetOffline's Pystrano playbook and deploy the requested revision."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK = REPOSITORY_ROOT / "deploy/getoffline/production/deployment.yml"
DEPLOY_USER = "jellyfin"
DEFAULT_DEPLOY_TIMEOUT_SECONDS = 20 * 60


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} must be set")
    return value


def deployment_timeout() -> int:
    raw_value = os.environ.get(
        "GETOFFLINE_DEPLOY_TIMEOUT_SECONDS",
        str(DEFAULT_DEPLOY_TIMEOUT_SECONDS),
    )
    try:
        timeout = int(raw_value)
    except ValueError as error:
        raise SystemExit("GETOFFLINE_DEPLOY_TIMEOUT_SECONDS must be an integer") from error
    if timeout <= 0:
        raise SystemExit("GETOFFLINE_DEPLOY_TIMEOUT_SECONDS must be greater than zero")
    return timeout


def run_command(
    command: list[str],
    *,
    description: str,
    timeout: int,
    cwd: Path = REPOSITORY_ROOT,
) -> None:
    print(f"[deploy] {description} (timeout: {timeout}s)", flush=True)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        start_new_session=True,
    )
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        rendered_command = " ".join(command)
        raise SystemExit(
            f"{description} timed out after {timeout}s.\n"
            f"Command: {rendered_command}"
        ) from error
    if return_code:
        raise subprocess.CalledProcessError(return_code, command)


def main() -> None:
    source_code_url = required_environment("GETOFFLINE_SOURCE_CODE_URL")
    revision = required_environment("GETOFFLINE_DEPLOY_REVISION")
    timeout = deployment_timeout()
    pystrano = Path(sys.executable).with_name("pystrano")
    if not pystrano.is_file():
        raise SystemExit(f"Pystrano executable was not found at {pystrano}")

    ssh = shutil.which("ssh")
    if not ssh:
        raise SystemExit("ssh must be installed to deploy")
    run_command(
        [
            ssh,
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ConnectionAttempts=1",
            f"{DEPLOY_USER}@localhost",
            "true",
        ],
        description=f"Checking non-interactive SSH access to {DEPLOY_USER}@localhost",
        timeout=20,
    )

    git = shutil.which("git")
    if not git:
        raise SystemExit("git must be installed to deploy")
    run_command(
        [git, "ls-remote", "--exit-code", source_code_url, "HEAD"],
        description="Checking non-interactive repository access",
        timeout=60,
    )

    rendered = PLAYBOOK.read_text(encoding="utf-8")
    rendered = rendered.replace("__SOURCE_CODE_URL__", source_code_url)
    rendered = rendered.replace("__REVISION__", revision)

    with tempfile.TemporaryDirectory(prefix="getoffline-pystrano-") as config_root:
        deployment_dir = Path(config_root) / "getoffline/production"
        deployment_dir.mkdir(parents=True)
        (deployment_dir / "deployment.yml").write_text(rendered, encoding="utf-8")
        run_command(
            [
                str(pystrano),
                "deploy",
                "production",
                "getoffline",
                "--deploy-config-dir",
                config_root,
            ],
            description="Running Pystrano deployment",
            timeout=timeout,
        )


if __name__ == "__main__":
    main()
