import importlib.util
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = REPOSITORY_ROOT / "scripts/deploy.py"
SPEC = importlib.util.spec_from_file_location("getoffline_deploy", DEPLOY_SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load scripts/deploy.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class DeployScriptTests(unittest.TestCase):
    def test_playbook_connects_as_project_user(self):
        playbook = deploy.PLAYBOOK.read_text(encoding="utf-8")

        self.assertIn(f'host: "{deploy.DEPLOY_USER}@localhost"', playbook)


if __name__ == "__main__":
    unittest.main()
