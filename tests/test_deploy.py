import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = REPOSITORY_ROOT / "scripts/deploy.py"
SPEC = importlib.util.spec_from_file_location("getoffline_deploy", DEPLOY_SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load scripts/deploy.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class DeployScriptTests(unittest.TestCase):
    def test_deployment_environment_selects_project_user_for_ssh(self):
        with patch.dict(os.environ, {"LOGNAME": "github-actions", "USER": "github-actions"}):
            environment = deploy.deployment_environment()

        self.assertEqual(environment["LOGNAME"], "jellyfin")
        self.assertEqual(environment["USER"], "jellyfin")

    def test_deployment_environment_preserves_existing_values(self):
        with patch.dict(os.environ, {"GETOFFLINE_DEPLOY_REVISION": "abc123"}):
            environment = deploy.deployment_environment()

        self.assertEqual(environment["GETOFFLINE_DEPLOY_REVISION"], "abc123")


if __name__ == "__main__":
    unittest.main()
