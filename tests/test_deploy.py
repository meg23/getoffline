import importlib.util
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = REPOSITORY_ROOT / "scripts/deploy.py"
CI_WORKFLOW = REPOSITORY_ROOT / ".github/workflows/ci.yml"
SPEC = importlib.util.spec_from_file_location("getoffline_deploy", DEPLOY_SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load scripts/deploy.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class DeployScriptTests(unittest.TestCase):
    def test_playbook_connects_as_project_user(self):
        playbook = deploy.PLAYBOOK.read_text(encoding="utf-8")

        self.assertIn(f'host: "{deploy.DEPLOY_USER}@localhost"', playbook)

    def test_workflow_runs_deployment_as_project_user(self):
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("--set-home --user=jellyfin", workflow)
        self.assertIn(
            "--preserve-env=GETOFFLINE_DEPLOY_REVISION,GETOFFLINE_SOURCE_CODE_URL",
            workflow,
        )

    def test_deployment_timeout_uses_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                deploy.deployment_timeout(),
                deploy.DEFAULT_DEPLOY_TIMEOUT_SECONDS,
            )

    def test_deployment_timeout_rejects_invalid_value(self):
        with mock.patch.dict(
            os.environ,
            {"GETOFFLINE_DEPLOY_TIMEOUT_SECONDS": "never"},
            clear=True,
        ):
            with self.assertRaisesRegex(SystemExit, "must be an integer"):
                deploy.deployment_timeout()

    @mock.patch.object(deploy.os, "killpg")
    @mock.patch.object(deploy.subprocess, "Popen")
    def test_run_command_terminates_a_timed_out_process(self, popen, killpg):
        process = popen.return_value
        process.pid = 123
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd=["pystrano"], timeout=1),
            0,
        ]

        with self.assertRaisesRegex(SystemExit, "timed out after 1s"):
            deploy.run_command(
                ["pystrano"],
                description="Running deployment",
                timeout=1,
            )

        killpg.assert_called_once_with(123, deploy.signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
