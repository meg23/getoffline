import subprocess
import sys
import textwrap
import unittest


class LazyImportTests(unittest.TestCase):
    def test_subtitles_import_does_not_load_transcription_module(self):
        script = textwrap.dedent(
            """
            import os
            import sys

            sys.path.insert(0, os.path.join(os.getcwd(), "src"))
            import workers.subtitles  # noqa: F401
            print("workers.transcription" in sys.modules)
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stdout.strip(), "False")

    def test_worker_runner_import_does_not_load_transcription_module(self):
        script = textwrap.dedent(
            """
            import os
            import sys

            sys.path.insert(0, os.path.join(os.getcwd(), "src"))
            os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")
            import workers.runner  # noqa: F401
            print("workers.transcription" in sys.modules)
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
