import json
import sys

from workers.podcasts import _download_podcasts_in_process


def _download_podcast_entry_in_subprocess(payload: dict) -> dict:
    config = payload["config"]
    downloaded_items = []
    _download_podcasts_in_process(config, downloaded_items)
    return {"downloaded_items": downloaded_items}


if __name__ == '__main__':
    payload = json.loads(sys.stdin.read())
    result = _download_podcast_entry_in_subprocess(payload)
    sys.stdout.write(json.dumps(result))
