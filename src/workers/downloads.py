"""Backward-compatible entrypoint for download runs.

This module intentionally avoids direct file-based configuration loading.
"""

from workers.main import run_downloads


if __name__ == "__main__":
    run_downloads()
