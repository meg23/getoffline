import json
import sys

from workers.youtube import _download_youtube_items_in_process


def _download_youtube_entry_in_subprocess(payload: dict) -> dict:
    config = payload["config"]
    downloaded_items: list[str] = []
    _download_youtube_items_in_process(config, downloaded_items)
    return {"downloaded_items": downloaded_items}


if __name__ == "__main__":
    payload = json.loads(sys.stdin.read())
    result = _download_youtube_entry_in_subprocess(payload)
    sys.stdout.write(json.dumps(result))
